package com.idox.mobile;

import android.app.Activity;
import android.content.Intent;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.Color;
import android.graphics.pdf.PdfRenderer;
import android.net.Uri;
import android.os.Bundle;
import android.os.ParcelFileDescriptor;
import android.provider.OpenableColumns;
import android.database.Cursor;
import android.text.InputType;
import android.view.View;
import android.view.ViewGroup;
import android.widget.ArrayAdapter;
import android.widget.Button;
import android.widget.CheckBox;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.Spinner;
import android.widget.TextView;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Date;
import java.util.List;
import java.util.Locale;

/**
 * idox on the phone, milestone 1: a PDF or image -> the idox Page blocks, every page read
 * by an on-device model through the bundled llama-server. Shows and saves the blocks and
 * the time and memory per page.
 *
 * Models are read from this app's private folder, /data/data/com.idox.mobile/files/models/,
 * copied in on the phone with `adb shell run-as com.idox.mobile cp ...` so the app owns
 * them (it can read neither VLM Tester's folder nor files adb pushes into Android/data).
 *
 * Results go to /sdcard/Android/data/com.idox.mobile/files/results/<run>/:
 *   page_001.json ...   the model's blocks, as returned
 *   extracted.md        all pages as Markdown (same rendering as test_pdf.py --out)
 *   timings.csv         one row per model call (read, continue): tokens, times, finish
 *   summary.txt         model, settings, totals, peak memory
 *   <name>.docx         the Word document (DocxWriter), also copied to Download/idox
 */
public class MainActivity extends Activity {

    private static final int PICK = 1;
    private static final int RENDER_DPI = 125;      // the same as test_pdf.py's default
    // Last resort only: loops are stopped as they start (PageGuard), so this only cuts
    // pages that are genuinely long. The laptop uses the same 4,000.
    private static final int MAX_TOKENS = 4000;

    private Spinner modelSpin, mmprojSpin;
    private EditText threadsEdit, tokensEdit, pagesEdit;
    private CheckBox gpuBox, textLayerBox;
    private Button pickBtn, runBtn, stopBtn, openBtn;
    private volatile Uri lastDocx;
    private TextView fileText, logText;
    private ScrollView logScroll;

    private Uri picked;
    private String pickedName = "document", pickedMime = "";
    private LlamaServer server;
    private volatile boolean stopRequested;
    private Thread worker;
    // Loads the selected model in the background as soon as the app opens (or the model
    // is changed), so pressing Run does not wait for it. One task at a time.
    private final java.util.concurrent.ExecutorService loader =
            java.util.concurrent.Executors.newSingleThreadExecutor();

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        server = new LlamaServer(getApplicationInfo().nativeLibraryDir);
        buildUi();
        // The run notification needs this on Android 13+; without it the service still
        // keeps the app alive, the notification is just not shown.
        if (android.os.Build.VERSION.SDK_INT >= 33
                && checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS) != android.content.pm.PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{android.Manifest.permission.POST_NOTIFICATIONS}, 2);
        }
        refreshModels();
        log("Models folder: " + modelsDir());
    }

    @Override
    protected void onResume() {
        super.onResume();
        refreshModels();
    }

    @Override
    protected void onDestroy() {
        loader.shutdownNow();
        stopRequested = true;
        server.stop();
        super.onDestroy();
    }

    // ------------------------------------------------------------------ UI

    private void buildUi() {
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        int pad = dp(12);
        root.setPadding(pad, pad, pad, pad);

        // Android 15+ draws apps edge to edge: pad the content clear of the status and
        // navigation bars, or the first row ends up hidden underneath them.
        if (android.os.Build.VERSION.SDK_INT >= 30) root.setOnApplyWindowInsetsListener((v, insets) -> {
            android.graphics.Insets bars = insets.getInsets(android.view.WindowInsets.Type.systemBars());
            v.setPadding(pad + bars.left, pad + bars.top, pad + bars.right, pad + bars.bottom);
            return insets;
        });

        pickBtn = button("Pick PDF or image", v -> pick());
        fileText = new TextView(this);
        fileText.setText("No file picked");
        root.addView(pickBtn);
        root.addView(fileText);

        root.addView(button("Refresh models", v -> refreshModels()));
        root.addView(button("Text layer test (picked PDF)", v -> textLayerTest()));
        root.addView(label("Model"));
        modelSpin = new Spinner(this);
        root.addView(modelSpin);
        root.addView(label("Image projector (mmproj)"));
        mmprojSpin = new Spinner(this);
        root.addView(mmprojSpin);
        modelSpin.setOnItemSelectedListener(new android.widget.AdapterView.OnItemSelectedListener() {
            public void onItemSelected(android.widget.AdapterView<?> p, View v, int pos, long id) {
                matchMmproj();
                preload();
            }
            public void onNothingSelected(android.widget.AdapterView<?> p) {
            }
        });

        LinearLayout row = new LinearLayout(this);
        threadsEdit = number("4");
        tokensEdit = number("512");
        pagesEdit = new EditText(this);
        pagesEdit.setText("all");
        row.addView(labelled("Threads", threadsEdit));
        row.addView(labelled("Image tokens", tokensEdit));
        row.addView(labelled("Pages", pagesEdit));
        root.addView(row);
        gpuBox = new CheckBox(this);
        gpuBox.setText("Use GPU (Vulkan) for the language model");
        root.addView(gpuBox);
        textLayerBox = new CheckBox(this);
        textLayerBox.setText("Digital PDFs: give the model the text layer instead of the page image");
        textLayerBox.setChecked(true);
        root.addView(textLayerBox);

        LinearLayout buttons = new LinearLayout(this);
        runBtn = button("Run", v -> startRun());
        stopBtn = button("Stop", v -> stopRun());
        stopBtn.setEnabled(false);
        openBtn = button("Open Word file", v -> openDocx());
        openBtn.setEnabled(false);
        buttons.addView(runBtn);
        buttons.addView(stopBtn);
        buttons.addView(openBtn);
        root.addView(buttons);

        logText = new TextView(this);
        logText.setTextIsSelectable(true);
        logText.setTypeface(android.graphics.Typeface.MONOSPACE);
        logText.setTextSize(11);
        logScroll = new ScrollView(this);
        logScroll.addView(logText);
        root.addView(logScroll, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f));
        setContentView(root);
    }

    private Button button(String text, View.OnClickListener l) {
        Button b = new Button(this);
        b.setText(text);
        b.setOnClickListener(l);
        return b;
    }

    private TextView label(String text) {
        TextView t = new TextView(this);
        t.setText(text);
        t.setPadding(0, dp(8), 0, 0);
        return t;
    }

    private EditText number(String value) {
        EditText e = new EditText(this);
        e.setInputType(InputType.TYPE_CLASS_NUMBER);
        e.setText(value);
        return e;
    }

    private LinearLayout labelled(String name, View field) {
        LinearLayout l = new LinearLayout(this);
        l.setOrientation(LinearLayout.VERTICAL);
        l.addView(label(name));
        l.addView(field, new LinearLayout.LayoutParams(dp(100), ViewGroup.LayoutParams.WRAP_CONTENT));
        return l;
    }

    private int dp(int v) {
        return Math.round(v * getResources().getDisplayMetrics().density);
    }

    private void log(String line) {
        runOnUiThread(() -> {
            logText.append(line + "\n");
            logScroll.post(() -> logScroll.fullScroll(View.FOCUS_DOWN));
        });
    }

    // ------------------------------------------------------------------ models

    private File modelsDir() {
        // Private storage, owned by this app. Files pushed with adb into Android/data are
        // owned by the shell user and this app cannot read them; copy models in with
        //   adb shell run-as com.idox.mobile cp <source>.gguf files/models/
        File d = new File(getFilesDir(), "models");
        d.mkdirs();
        return d;
    }

    private void refreshModels() {
        List<String> models = new ArrayList<>(), projectors = new ArrayList<>();
        File[] files = modelsDir().listFiles();
        if (files != null) {
            Arrays.sort(files);
            for (File f : files) {
                String n = f.getName();
                if (!n.toLowerCase(Locale.ROOT).endsWith(".gguf")) continue;
                (n.toLowerCase(Locale.ROOT).startsWith("mmproj") ? projectors : models).add(n);
            }
        }
        String keep = (String) modelSpin.getSelectedItem();
        modelSpin.setAdapter(new ArrayAdapter<>(this, android.R.layout.simple_spinner_dropdown_item, models));
        mmprojSpin.setAdapter(new ArrayAdapter<>(this, android.R.layout.simple_spinner_dropdown_item, projectors));
        if (keep != null && models.contains(keep)) modelSpin.setSelection(models.indexOf(keep));
        matchMmproj();
        if (logText != null) {
            log("Found " + models.size() + " model(s) and " + projectors.size() + " projector(s) in " + modelsDir());
        }
    }

    /** The server's own log, kept across runs; each run gets a copy in its results folder. */
    private File serverLog() {
        return new File(getExternalFilesDir(null), "server.log");
    }

    /** Start loading the selected model now, unless a conversion is running. */
    private void preload() {
        if (worker != null && worker.isAlive()) return;   // never restart a server mid-run
        Object m = modelSpin.getSelectedItem(), p = mmprojSpin.getSelectedItem();
        if (m == null || p == null) return;
        File model = new File(modelsDir(), (String) m), mmproj = new File(modelsDir(), (String) p);
        int threads = intOf(threadsEdit, 4), tokens = intOf(tokensEdit, 512);
        boolean gpu = gpuBox.isChecked();
        loader.submit(() -> {
            if (worker != null && worker.isAlive()) return;
            try {
                long t0 = System.nanoTime();
                if (server.ensure(model, mmproj, threads, gpu, tokens, serverLog())) {
                    log(String.format(Locale.ROOT, "%s loaded in %.1f s -- ready, Run starts straight away",
                            model.getName(), (System.nanoTime() - t0) / 1e9));
                }
            } catch (Exception e) {
                log("could not load " + model.getName() + ": " + e.getMessage());
            }
        });
    }

    /** Select the projector whose name shares the most with the model's name. */
    private void matchMmproj() {
        String m = (String) modelSpin.getSelectedItem();
        if (m == null || mmprojSpin.getAdapter() == null) return;
        String key = m.toLowerCase(Locale.ROOT).replace(".gguf", "").replaceAll("[-_.]?q\\d.*$", "");
        for (int i = 0; i < mmprojSpin.getAdapter().getCount(); i++) {
            String p = ((String) mmprojSpin.getAdapter().getItem(i)).toLowerCase(Locale.ROOT);
            if (p.contains(key)) {
                mmprojSpin.setSelection(i);
                return;
            }
        }
    }

    // ------------------------------------------------------------------ picking

    private void pick() {
        Intent i = new Intent(Intent.ACTION_OPEN_DOCUMENT);
        i.addCategory(Intent.CATEGORY_OPENABLE);
        i.setType("*/*");
        i.putExtra(Intent.EXTRA_MIME_TYPES, new String[]{"application/pdf", "image/*"});
        startActivityForResult(i, PICK);
    }

    @Override
    protected void onActivityResult(int req, int res, Intent data) {
        super.onActivityResult(req, res, data);
        if (req != PICK || res != RESULT_OK || data == null || data.getData() == null) return;
        picked = data.getData();
        pickedMime = String.valueOf(getContentResolver().getType(picked));
        pickedName = displayName(picked);
        fileText.setText(pickedName + "  (" + pickedMime + ")");
    }

    private String displayName(Uri uri) {
        try (Cursor c = getContentResolver().query(uri, null, null, null, null)) {
            if (c != null && c.moveToFirst()) {
                int i = c.getColumnIndex(OpenableColumns.DISPLAY_NAME);
                if (i >= 0) return c.getString(i);
            }
        } catch (Exception ignored) {
        }
        return "document";
    }

    // ------------------------------------------------------------------ the run

    private void startRun() {
        if (picked == null) {
            log("Pick a PDF or image first.");
            return;
        }
        if (modelSpin.getSelectedItem() == null || mmprojSpin.getSelectedItem() == null) {
            log("No model in " + modelsDir() + " -- copy a model and its mmproj there.");
            return;
        }
        File model = new File(modelsDir(), (String) modelSpin.getSelectedItem());
        File mmproj = new File(modelsDir(), (String) mmprojSpin.getSelectedItem());
        int threads = intOf(threadsEdit, 4), tokens = intOf(tokensEdit, 512);
        boolean gpu = gpuBox.isChecked();
        String pages = pagesEdit.getText().toString().trim();

        stopRequested = false;
        runBtn.setEnabled(false);
        stopBtn.setEnabled(true);
        ConvertService.start(this, "Converting " + pickedName + " with " + model.getName());
        worker = new Thread(() -> {
            try {
                convert(model, mmproj, threads, gpu, tokens, pages, textLayerBox.isChecked());
            } catch (Throwable t) {
                log("FAILED: " + t);
            } finally {
                ConvertService.stop(this);
                runOnUiThread(() -> {
                    runBtn.setEnabled(true);
                    stopBtn.setEnabled(false);
                });
            }
        });
        worker.start();
    }

    private void stopRun() {
        stopRequested = true;
        server.stop();          // cuts the request that is in flight
        log("Stopping...");
    }

    private void convert(File model, File mmproj, int threads, boolean gpu, int tokens,
                         String pageSpec, boolean useTextLayer) throws Exception {
        String stamp = new SimpleDateFormat("yyyyMMdd-HHmmss", Locale.ROOT).format(new Date());
        String stem = pickedName.replaceAll("\\.[^.]+$", "").replaceAll("[^A-Za-z0-9._-]+", "_");
        File out = new File(getExternalFilesDir("results"),
                stem + "__" + model.getName().replace(".gguf", "") + "__" + stamp);
        out.mkdirs();

        // The phone's own prompt (assets/mobile_*.txt), written for small on-device models.
        // The laptop's prompt in blocks.py is separate and unchanged.
        String system = asset("mobile_system_prompt.txt"), user = asset("mobile_user_prompt.txt");
        String textSystem = asset("mobile_text_system_prompt.txt"), textUser = asset("mobile_text_user_prompt.txt");
        JSONObject schema = new JSONObject(asset("page_schema.json"));

        log("\n=== " + pickedName + "\nmodel " + model.getName() + " + " + mmproj.getName()
                + "\nthreads " + threads + ", " + (gpu ? "GPU" : "CPU only") + ", image tokens " + tokens);
        long loadStart = System.nanoTime();
        // Waits here if the background preload is still loading this model.
        boolean loadedNow = server.ensure(model, mmproj, threads, gpu, tokens, serverLog());
        double loadS = (System.nanoTime() - loadStart) / 1e9;
        log(loadedNow ? String.format(Locale.ROOT, "model loaded in %.1f s (cold)", loadS)
                : String.format(Locale.ROOT, "model already loaded (warm)%s",
                loadS > 0.5 ? String.format(Locale.ROOT, " -- waited %.1f s for the preload to finish", loadS) : ""));

        // PDFs: nothing is drawn up front. Digital pages are read from their text layer,
        // scanned pages from the image stored inside the PDF; a page is drawn only when it
        // has neither (renderPage). Pictures that are not PDFs (photos) are sent as they are.
        List<byte[]> images = isPdf() ? null : pageImages(pageSpec);
        List<Integer> pageIdx = new ArrayList<>();
        List<TextLayer.Page> textPages = null;
        if (isPdf()) {
            long t0 = System.nanoTime();
            try (InputStream in = getContentResolver().openInputStream(picked)) {
                textPages = TextLayer.extract(this, in);
            } catch (Exception e) {
                log("text layer could not be read (" + e.getMessage() + ") -- using page images");
            }
            if (textPages != null) {
                pageIdx = pageList(pageSpec, textPages.size());
                log(String.format(Locale.ROOT, "text layer read in %.2f s", (System.nanoTime() - t0) / 1e9));
            }
        }
        if (images == null && textPages == null) images = pageImages(pageSpec);   // PDF unreadable by PdfBox
        if (images != null) {                             // images only: pages are 1..n
            pageIdx = new ArrayList<>();
            for (int k = 0; k < images.size(); k++) pageIdx.add(k);
        }
        int nPages = pageIdx.size();
        StringBuilder md = new StringBuilder();
        StringBuilder csv = new StringBuilder("page,attempt,prompt_tokens,prefill_s,gen_tokens,gen_s,gen_tok_per_s,wall_s,finish\n");
        double totalWall = 0;
        StringBuilder perPage = new StringBuilder();     // "page 1: 154.6 s (read 116.9 + continue 37.7)" 
        int ok = 0;
        List<JSONObject> parsedPages = new ArrayList<>();
        for (int i = 0; i < nPages && !stopRequested; i++) {
            int pageNo = pageIdx.get(i) + 1;      // the real page number, also with --pages
            TextLayer.Page tp = textPages == null ? null : textPages.get(pageIdx.get(i));
            // A page whose text layer is (nearly) empty is a scan: it is read from its image.
            boolean fromText = useTextLayer && tp != null && tp.plain.length() >= 20;
            List<byte[]> pageImgs = null;
            String source = "";
            if (!fromText) {
                boolean blank = false;
                if (tp != null && !tp.scanImages.isEmpty()) {
                    for (byte[] im : tp.scanImages) blank |= TextLayer.looksBlank(im);
                    if (blank) log("page " + pageNo + ": the image taken out of the PDF came out blank -- drawing the page instead");
                }
                if (tp != null && !tp.scanImages.isEmpty() && !blank) {
                    pageImgs = tp.scanImages;
                    source = "the scan image(s) stored in the PDF (" + tp.scanInfo + ")";
                } else if (images != null) {
                    pageImgs = java.util.Collections.singletonList(images.get(i));
                    source = "the picture file";
                } else {
                    // last resort: no usable stored image
                    pageImgs = java.util.Collections.singletonList(renderPage(pageIdx.get(i)));
                    source = "a drawing of the page (it has no text layer and no stored image"
                            + (tp != null && !tp.scanInfo.isEmpty() ? ": " + tp.scanInfo : "") + ")";
                }
            }
            log("page " + pageNo + " (" + (i + 1) + " of " + nPages + "): "
                    + (fromText ? "reading its text layer (" + tp.lines + " lines, " + tp.expectedBlocks
                    + " blocks expected, limit " + (tp.expectedBlocks + 1) + ")" : "reading " + source)
                    + (tp != null && tp.images > 0 ? " -- has " + tp.images + " picture(s); text inside pictures is not in the text layer" : ""));
            // The exact image sent to the model, kept for checking and replaying a page.
            if (pageImgs != null) {
                for (int k = 0; k < pageImgs.size(); k++) {
                    byte[] im = pageImgs.get(k);
                    boolean jpg = im.length > 2 && (im[0] & 0xff) == 0xFF && (im[1] & 0xff) == 0xD8;
                    try (FileOutputStream f = new FileOutputStream(new File(out, String.format(Locale.ROOT,
                            "page_%03d_img%d.%s", pageNo, k + 1, jpg ? "jpg" : "png")))) {
                        f.write(im);
                    }
                }
            }
            // repeat_penalty: 1.0 when reading text -- the model must repeat what it is given
            // (words, table brackets), and 1.15 there made the 2B add repeated blocks and take
            // twice as long. Images keep the 1.15 their runs were tuned with.
            // Text mode: the page's layout says how many blocks it has, so the schema caps
            // the list at that + 2. The model then cannot start the page over after
            // finishing it (the 2B's habit) -- after the last allowed block, only the
            // closing bracket is possible.
            JSONObject pageSchema = schema;
            if (fromText) {
                pageSchema = new JSONObject(schema.toString());
                pageSchema.getJSONObject("properties").getJSONObject("blocks").put("maxItems", tp.expectedBlocks + 1);
            }
            LlamaServer.Result r = fromText
                    ? server.extract(null, textSystem, textUser.replace("{PAGE_TEXT}", tp.text), pageSchema, MAX_TOKENS, 0.0, 1.0,
                            PageGuard.words(tp.plain))
                    : server.extract(pageImgs, system, user, schema, MAX_TOKENS, 0.0, 1.15, null);
            if (fromText) write(new File(out, String.format(Locale.ROOT, "page_%03d.text.txt", pageNo)), tp.text);
            String readLabel = fromText ? "read-text" : "read-image";
            totalWall += r.wallMs / 1000;
            double pageS = r.wallMs / 1000, readS = pageS, contS = 0;
            csvRow(csv, pageNo, readLabel, r);
            logAttempt(pageNo, readLabel, r);
            JSONObject pageJson = readPage(r);

            // Safety net for closing the list too early (seen with the 2B: the heading,
            // then "]}"): if the model wrote at most half the blocks it itself estimated,
            // let it CONTINUE from its last block. The page image is still cached by the
            // server, so this costs only the extra writing, not another ~60 s read; a
            // grammar keeps the continuation to well-formed blocks.
            int estimate = pageJson == null || pageJson.optJSONObject("analysis") == null ? 0
                    : pageJson.optJSONObject("analysis").optInt("n_blocks");
            int written = pageJson == null ? 0 : pageJson.getJSONArray("blocks").length();
            String prefix = PageGuard.openPrefix(r.content);
            // When to continue. Image mode can only go by the model's own estimate. Text
            // mode KNOWS the page's words, so it continues only if words are really
            // missing -- the estimate (often 2x too high) made the 2B continue complete
            // pages, and with nothing left to write it looped.
            boolean shortPage;
            String why;
            if (fromText) {
                java.util.Set<String> truthW = PageGuard.words(tp.plain);
                java.util.Set<String> miss = pageJson == null ? truthW
                        : PageGuard.missingWords(tp.plain, pageJson.getJSONArray("blocks"));
                shortPage = miss.size() >= 3 && miss.size() >= 0.03 * truthW.size();
                why = miss.size() + " of the page's " + truthW.size() + " words are not written yet";
            } else {
                shortPage = estimate >= 3 && written * 2 <= estimate;
                why = "wrote " + written + " of an estimated " + estimate + " blocks";
            }
            // Text mode: the continuation may only use the blocks still allowed by the page's
            // limit (without it, a forced extra block was written as junk); none left -> the
            // text-layer repair below restores what is missing instead.
            String grammar = asset("continue_blocks.gbnf");
            if (fromText) {
                int left = tp.expectedBlocks + 1 - written;
                if (left <= 0) shortPage = false;
                else grammar = grammar.replaceFirst("(?m)^root ::= .*$", java.util.regex.Matcher.quoteReplacement(
                        "root ::= ws \",\" ws block ( ws \",\" ws block ){0," + (left - 1) + "} ws \"]\" ws \"}\""));
            }
            if (!stopRequested && prefix != null && "stop".equals(r.finishReason) && shortPage) {
                log(String.format(Locale.ROOT, "page %d: %s -- continuing from block %d", pageNo, why, written));
                LlamaServer.Result more = server.continueFrom(r, prefix, grammar, MAX_TOKENS);
                totalWall += more.wallMs / 1000;
                contS = more.wallMs / 1000;
                pageS += contS;
                csvRow(csv, pageNo, "continue", more);
                logAttempt(pageNo, "continue", more);
                JSONObject longer = readPage(more);
                if (longer != null && longer.getJSONArray("blocks").length() > written) {
                    r = more;
                    pageJson = longer;
                }
            }
            write(new File(out, String.format(Locale.ROOT, "page_%03d.json", pageNo)), r.content);

            int nBlocks = -1;
            String problem = null;
            if (pageJson == null) {
                problem = "no usable output (" + r.finishReason + ")";
            } else {
                if (fromText) PageGuard.cleanMarkdown(pageJson);
                int dropped = PageGuard.dedupe(pageJson);
                if (dropped > 0) log("page " + pageNo + ": dropped " + dropped + " repeated block(s)");
                if (fromText) {
                    String fixed = TextRepair.repair(pageJson, tp.text);
                    if (!fixed.isEmpty()) log("page " + pageNo + ": repaired from the text layer -- " + fixed);
                }
                if (r.finishReason.startsWith("done")) {
                    log("page " + pageNo + ": every word of the page was written and the model started over -- stopped it there");
                } else if (r.finishReason.startsWith("loop")) {
                    log("page " + pageNo + ": the model started repeating itself -- stopped it, kept the blocks before the loop");
                } else if (r.finishReason.startsWith("length")) {
                    log("page " + pageNo + ": hit the " + MAX_TOKENS + "-token limit -- kept the blocks finished before it");
                }
                JSONArray blocks = pageJson.getJSONArray("blocks");
                if (tp != null && tp.plain.length() >= 20) {
                    String cov = coverage(tp.plain, blocks);
                    log("page " + pageNo + ": " + cov);
                    perPage.append("page ").append(pageNo).append(" ").append(cov).append("\n");
                }
                parsedPages.add(pageJson);
                write(new File(out, String.format(Locale.ROOT, "page_%03d.clean.json", pageNo)), pageJson.toString(1));
                nBlocks = blocks.length();
                md.append("\n---\n\n*(page ").append(pageNo).append(")*\n\n");
                for (int k = 0; k < blocks.length(); k++) {
                    md.append(renderBlock(blocks.getJSONObject(k))).append("\n\n");
                }
                ok++;
            }
            // One line with the page's whole time, so the pages add up to the run's total.
            String parts = contS > 0 ? String.format(Locale.ROOT, " (read %.1f + continue %.1f)", readS, contS) : "";
            String line = String.format(Locale.ROOT, "page %d: %.1f s%s", pageNo, pageS, parts);
            perPage.append(line).append("\n");
            log(line + " -- " + (problem == null ? nBlocks + " blocks" : "FAILED (" + problem + ")"));
        }

        // --- the Word document (no model here: the blocks above, written as .docx) ---
        if (!parsedPages.isEmpty()) {
            long w0 = System.nanoTime();
            File docx = new File(out, stem + ".docx");
            try (FileOutputStream o = new FileOutputStream(docx)) {
                DocxWriter.write(parsedPages, o);
            }
            lastDocx = publishToDownloads(docx);
            log(String.format(Locale.ROOT, "Word file written in %.2f s: %s%s", (System.nanoTime() - w0) / 1e9,
                    docx.getName(), lastDocx != null ? "  (copy in Download/idox)" : ""));
            runOnUiThread(() -> openBtn.setEnabled(lastDocx != null));
        }

        long peak = server.peakRssMb();
        write(new File(out, "extracted.md"), md.toString());
        write(new File(out, "timings.csv"), csv.toString());
        String summary = String.format(Locale.ROOT,
                "file: %s\nmodel: %s\nmmproj: %s\nthreads: %d\ngpu: %s\nimage_tokens: %d\n"
                        + "prompt: mobile (assets/mobile_system_prompt.txt)\n"
                        + "input: " + (useTextLayer ? "text layer for digital PDF pages, image otherwise" : "page images") + "\n"
                        + "model_start: " + (loadedNow ? "cold (loaded for this run)" : "warm (already loaded)") + "\n"
                        + "pages: %d, read ok: %d%s\nmodel_load_s: %.1f\npages_total_s: %.1f\n"
                        + "server_peak_rss_mb: %d\ndevice: %s %s, Android %s\n",
                pickedName, model.getName(), mmproj.getName(), threads, gpu, tokens,
                nPages, ok, stopRequested ? " (stopped)" : "", loadS, totalWall, peak,
                android.os.Build.MANUFACTURER, android.os.Build.MODEL, android.os.Build.VERSION.RELEASE);
        summary += perPage;
        write(new File(out, "summary.txt"), summary);
        copyFile(serverLog(), new File(out, "server.log"));
        log("\n" + summary + "saved to " + out);
    }

    /** One PNG per page to read: rendered PDF pages, or the picked image itself. */
    /** Last resort for a PDF page with neither a text layer nor a stored image: draw it. */
    private byte[] renderPage(int index) throws Exception {
        try (ParcelFileDescriptor fd = getContentResolver().openFileDescriptor(picked, "r");
             PdfRenderer pdf = new PdfRenderer(fd);
             PdfRenderer.Page page = pdf.openPage(index)) {
            int w = Math.round(page.getWidth() * RENDER_DPI / 72f);
            int h = Math.round(page.getHeight() * RENDER_DPI / 72f);
            Bitmap bmp = Bitmap.createBitmap(w, h, Bitmap.Config.ARGB_8888);
            bmp.eraseColor(Color.WHITE);
            page.render(bmp, null, null, PdfRenderer.Page.RENDER_MODE_FOR_DISPLAY);
            byte[] b = png(bmp);
            bmp.recycle();
            return b;
        }
    }

    private List<byte[]> pageImages(String spec) throws Exception {
        List<byte[]> out = new ArrayList<>();
        if (pickedMime.contains("pdf") || pickedName.toLowerCase(Locale.ROOT).endsWith(".pdf")) {
            try (ParcelFileDescriptor fd = getContentResolver().openFileDescriptor(picked, "r");
                 PdfRenderer pdf = new PdfRenderer(fd)) {
                for (int p : pageList(spec, pdf.getPageCount())) {
                    try (PdfRenderer.Page page = pdf.openPage(p)) {
                        // page sizes are in points (1/72 in); draw at 125 dpi on white
                        int w = Math.round(page.getWidth() * RENDER_DPI / 72f);
                        int h = Math.round(page.getHeight() * RENDER_DPI / 72f);
                        Bitmap bmp = Bitmap.createBitmap(w, h, Bitmap.Config.ARGB_8888);
                        bmp.eraseColor(Color.WHITE);
                        page.render(bmp, null, null, PdfRenderer.Page.RENDER_MODE_FOR_DISPLAY);
                        out.add(png(bmp));
                        bmp.recycle();
                    }
                }
            }
            log("PDF: " + out.size() + " page(s) rendered at " + RENDER_DPI + " dpi");
        } else {
            try (InputStream in = getContentResolver().openInputStream(picked)) {
                Bitmap bmp = BitmapFactory.decodeStream(in);
                if (bmp == null) throw new RuntimeException("could not decode the image");
                log("image: " + bmp.getWidth() + "x" + bmp.getHeight());
                out.add(png(bmp));
                bmp.recycle();
            }
        }
        return out;
    }

    /** "all", "2", "1-3", "1,3-4" -> zero-based page indexes (as test_pdf.py --pages). */
    static List<Integer> pageList(String spec, int count) {
        List<Integer> out = new ArrayList<>();
        if (spec.isEmpty() || spec.equalsIgnoreCase("all")) {
            for (int i = 0; i < count; i++) out.add(i);
            return out;
        }
        for (String part : spec.replace(" ", "").split(",")) {
            if (part.contains("-")) {
                String[] ab = part.split("-", 2);
                for (int i = Integer.parseInt(ab[0]) - 1; i < Integer.parseInt(ab[1]); i++) out.add(i);
            } else {
                out.add(Integer.parseInt(part) - 1);
            }
        }
        out.removeIf(i -> i < 0 || i >= count);
        return out;
    }

    /** One block as Markdown, as test_pdf.py's render_block() writes it. */
    static String renderBlock(JSONObject b) throws Exception {
        String kind = b.optString("kind");
        if (kind.equals("table")) {
            StringBuilder s = new StringBuilder();
            JSONArray header = b.optJSONArray("header");
            JSONArray rows = b.optJSONArray("rows");
            int cols = b.optInt("n_cols", header != null ? header.length() : 0);
            if (header != null && header.length() > 0) {
                s.append(mdRow(header)).append("\n|");
                for (int i = 0; i < cols; i++) s.append(" --- |");
                s.append("\n");
            }
            if (rows != null) {
                for (int r = 0; r < rows.length(); r++) s.append(mdRow(rows.getJSONArray(r))).append("\n");
            }
            return s.toString().trim();
        }
        String text = b.optString("text");
        if (kind.equals("heading")) return "## " + text;
        if (kind.equals("caption") || kind.equals("image")) return "*" + text + "*";
        return text;
    }

    /** One line per model call, so the rows add up to the run's total time. */
    private static void csvRow(StringBuilder csv, int page, String attempt, LlamaServer.Result r) {
        double tps = r.genMs > 0 ? r.genTokens / (r.genMs / 1000) : 0;
        csv.append(String.format(Locale.ROOT, "%d,%s,%d,%.2f,%d,%.2f,%.1f,%.2f,%s\n", page, attempt,
                r.promptTokens, r.promptMs / 1000, r.genTokens, r.genMs / 1000, tps, r.wallMs / 1000, r.finishReason));
    }

    private void logAttempt(int page, String attempt, LlamaServer.Result r) {
        double tps = r.genMs > 0 ? r.genTokens / (r.genMs / 1000) : 0;
        log(String.format(Locale.ROOT, "page %d %s: reading %d tok %.1f s | writing %d tok %.1f s (%.1f tok/s) | %.1f s | %s",
                page, attempt, r.promptTokens, r.promptMs / 1000, r.genTokens, r.genMs / 1000, tps,
                r.wallMs / 1000, r.finishReason));
    }

    private boolean isPdf() {
        return pickedMime.contains("pdf") || pickedName.toLowerCase(Locale.ROOT).endsWith(".pdf");
    }

    /**
     * How much of the page's real text (its text layer) is in the model's blocks, and
     * which words the model wrote that are not on the page. Words only, case-insensitive.
     */
    static String coverage(String truth, JSONArray blocks) throws Exception {
        StringBuilder sb = new StringBuilder();
        for (int k = 0; k < blocks.length(); k++) {
            JSONObject b = blocks.getJSONObject(k);
            if ("table".equals(b.optString("kind"))) {
                sb.append(b.optJSONArray("header")).append(' ').append(b.optJSONArray("rows")).append(' ');
            } else {
                sb.append(b.optString("text")).append(' ');
            }
        }
        java.util.Set<String> t = PageGuard.words(truth), o = PageGuard.words(sb);
        java.util.Set<String> missing = new java.util.TreeSet<>(t), extra = new java.util.TreeSet<>(o);
        missing.removeAll(o);
        extra.removeAll(t);
        double found = t.isEmpty() ? 1 : 1 - (double) missing.size() / t.size();
        return String.format(Locale.ROOT, "accuracy check vs text layer: %.0f%% of the page's words found%s%s",
                found * 100, missing.isEmpty() ? "" : ", missing " + clip(missing),
                extra.isEmpty() ? "" : ", not on the page " + clip(extra));
    }

    private static String clip(java.util.Set<String> s) {
        List<String> l = new ArrayList<>(s);
        return l.size() <= 8 ? l.toString() : l.subList(0, 8) + " +" + (l.size() - 8);
    }

    /** The page's JSON, or the blocks salvaged from a cut-off answer, or null. */
    private static JSONObject readPage(LlamaServer.Result r) {
        if ("stop".equals(r.finishReason)) {
            try {
                return new JSONObject(r.content);
            } catch (Exception ignored) {
                // fall through to salvage
            }
        }
        return PageGuard.salvage(r.content);
    }

    private static String mdRow(JSONArray cells) {
        StringBuilder s = new StringBuilder("|");
        for (int i = 0; i < cells.length(); i++) s.append(" ").append(cells.optString(i).replace("|", "\\|")).append(" |");
        return s.toString();
    }

    // ------------------------------------------------------------------ text layer comparison

    private void textLayerTest() {
        if (picked == null) {
            log("Pick a PDF first.");
            return;
        }
        new Thread(() -> {
            try (ParcelFileDescriptor fd = getContentResolver().openFileDescriptor(picked, "r");
                 InputStream in = getContentResolver().openInputStream(picked)) {
                String stem = pickedName.replaceAll("\\.[^.]+$", "").replaceAll("[^A-Za-z0-9._-]+", "_");
                File out = new File(getExternalFilesDir("results"), "textlayer__" + stem);
                log("Text layer test on " + pickedName + " ...\n" + TextLayerTest.run(this, fd, in, out) + "saved to " + out);
            } catch (Throwable t) {
                log("Text layer test FAILED: " + t);
            }
        }).start();
    }

    // ------------------------------------------------------------------ the Word file

    /** Copy the .docx to the public Download/idox folder, where Word and file managers see it. */
    private Uri publishToDownloads(File docx) {
        if (android.os.Build.VERSION.SDK_INT < 29) return null;
        try {
            android.content.ContentValues v = new android.content.ContentValues();
            v.put(android.provider.MediaStore.Downloads.DISPLAY_NAME, docx.getName());
            v.put(android.provider.MediaStore.Downloads.MIME_TYPE, DOCX_MIME);
            v.put(android.provider.MediaStore.Downloads.RELATIVE_PATH, "Download/idox");
            Uri uri = getContentResolver().insert(android.provider.MediaStore.Downloads.EXTERNAL_CONTENT_URI, v);
            if (uri == null) return null;
            try (java.io.OutputStream o = getContentResolver().openOutputStream(uri);
                 java.io.FileInputStream in = new java.io.FileInputStream(docx)) {
                byte[] buf = new byte[65536];
                for (int n; (n = in.read(buf)) > 0; ) o.write(buf, 0, n);
            }
            return uri;
        } catch (Exception e) {
            log("could not copy the Word file to Download/idox: " + e.getMessage());
            return null;
        }
    }

    private void openDocx() {
        if (lastDocx == null) return;
        Intent i = new Intent(Intent.ACTION_VIEW).setDataAndType(lastDocx, DOCX_MIME)
                .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
        try {
            startActivity(i);
        } catch (android.content.ActivityNotFoundException e) {
            log("No app on the phone opens .docx files -- install Word or Google Docs, "
                    + "or open Download/idox from a file manager.");
        }
    }

    private static final String DOCX_MIME =
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document";

    // ------------------------------------------------------------------ helpers

    private static byte[] png(Bitmap bmp) {
        ByteArrayOutputStream o = new ByteArrayOutputStream();
        bmp.compress(Bitmap.CompressFormat.PNG, 100, o);
        return o.toByteArray();
    }

    private String asset(String name) throws Exception {
        try (InputStream in = getAssets().open(name)) {
            return LlamaServer.read(in);
        }
    }

    private static void copyFile(File from, File to) {
        try (java.io.FileInputStream in = new java.io.FileInputStream(from);
             FileOutputStream o = new FileOutputStream(to)) {
            byte[] buf = new byte[65536];
            for (int n; (n = in.read(buf)) > 0; ) o.write(buf, 0, n);
        } catch (Exception ignored) {
        }
    }

    private static void write(File f, String text) throws Exception {
        try (FileOutputStream o = new FileOutputStream(f)) {
            o.write(text.getBytes(StandardCharsets.UTF_8));
        }
    }

    private static int intOf(EditText e, int fallback) {
        try {
            return Integer.parseInt(e.getText().toString().trim());
        } catch (Exception ex) {
            return fallback;
        }
    }
}
