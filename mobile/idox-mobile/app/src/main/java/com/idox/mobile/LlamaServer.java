package com.idox.mobile;

import android.util.Base64;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileReader;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.ServerSocket;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.io.ByteArrayOutputStream;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.Map;

/**
 * The bundled llama.cpp server (libllama-server.so), run as a child process on 127.0.0.1.
 *
 * Started the way VLM Tester starts it (read from its decompiled MainActivity): the
 * binary lives in the app's nativeLibraryDir, LD_LIBRARY_PATH points there, threads are
 * pinned to the 4 big cores (4-7 on the Exynos 1380), flash attention on, and the image
 * projector always stays on the CPU.
 *
 * Requests are the same as idox's blocks.py sends to llama-server on the laptop: one
 * page image, the idox system prompt, the Page JSON schema, temperature 0.
 */
final class LlamaServer {

    static final class Result {
        String content, finishReason;
        String templatePrompt;                // for continueFrom(): the same prompt, image cached
        List<String> imageB64;                // the page's image(s), base64; null in text mode
        double repeatPenalty;                 // continueFrom() keeps the read's setting
        java.util.Set<String> truthWords;     // text mode: the page's real words, else null
        int promptTokens, genTokens;
        double promptMs, genMs, wallMs;
    }

    private final File exec;
    private final String libDir;
    private Process process;
    private int port;
    private String loadedKey;

    LlamaServer(String nativeLibraryDir) {
        this.libDir = nativeLibraryDir;
        this.exec = new File(nativeLibraryDir, "libllama-server.so");
    }

    /** Start (or keep) a server for this model; restarts if anything that matters changed. */
    /** True if the model was (re)loaded now, false if the running server already had it. */
    synchronized boolean ensure(File model, File mmproj, int threads, boolean gpu, int imageTokens,
                             File log) throws Exception {
        String key = model + "|" + mmproj + "|" + threads + "|" + gpu + "|" + imageTokens;
        if (process != null && process.isAlive() && key.equals(loadedKey)) return false;
        stop();

        port = freePort();
        List<String> args = new ArrayList<>(Arrays.asList(
                exec.getAbsolutePath(), "-m", model.getAbsolutePath(),
                "--mmproj", mmproj.getAbsolutePath(),
                "--host", "127.0.0.1", "--port", String.valueOf(port),
                "-t", String.valueOf(threads), "-tb", String.valueOf(threads),
                "-Cr", "4-7", "-Crb", "4-7", "--cpu-strict", "1", "--cpu-strict-batch", "1",
                "-fa", "on", "-np", "1",
                // idox output can reach 4,000 tokens on top of ~1,700 prompt tokens.
                "-c", "8192"));
        // --jinja only for the models VLM Tester uses it with. For Qwen3-VL it makes the
        // server re-parse each answer with the template's parser, which rejected a valid
        // page with HTTP 500 "does not match the expected peg-native format".
        String name = model.getName().toLowerCase(java.util.Locale.ROOT);
        if (name.contains("minicpm") || name.contains("lfm2") || name.contains("paddle")) {
            args.add("--jinja");
        }
        // Qwen (the 2B): 8-bit KV cache. Tested on 4 text-mode pages: 237/237 words,
        // 37/37 numbers, 60/60 table cells -- same as f16 -- with ~180 MB less peak memory.
        // LFM2 (the 3B) gets it too, to fit the phone's free memory (accuracy not yet checked).
        if (name.contains("qwen") || name.contains("lfm2")) {
            args.addAll(Arrays.asList("-ctk", "q8_0", "-ctv", "q8_0"));
        }
        // The dot-product build keeps a prompt cache in RAM (default 8192 MB); peak memory
        // rose ~350-600 MB with it. 512 MB still holds the reused instructions.
        args.addAll(Arrays.asList("--cache-ram", "512"));
        if (gpu) {
            args.addAll(Arrays.asList("-ngl", "99", "--no-mmproj-offload"));
        } else {
            args.addAll(Arrays.asList("-ngl", "0", "--no-mmproj-offload", "--no-op-offload"));
        }
        ProcessBuilder pb = new ProcessBuilder(args).redirectErrorStream(true)
                .redirectOutput(ProcessBuilder.Redirect.appendTo(log));
        Map<String, String> env = pb.environment();
        env.put("LD_LIBRARY_PATH", libDir);
        // Same fixed image budget as the laptop's local 2B (--image-min/max-tokens).
        env.put("LLAMA_ARG_IMAGE_MIN_TOKENS", String.valueOf(imageTokens));
        env.put("LLAMA_ARG_IMAGE_MAX_TOKENS", String.valueOf(imageTokens));
        process = pb.start();

        long deadline = System.currentTimeMillis() + 180_000;
        while (System.currentTimeMillis() < deadline) {
            if (!process.isAlive()) {
                throw new RuntimeException("llama-server exited (code " + process.exitValue()
                        + ") while loading -- see server.log");
            }
            try {
                HttpURLConnection c = (HttpURLConnection) new URL(base() + "/health").openConnection();
                c.setConnectTimeout(1000);
                c.setReadTimeout(1000);
                if (c.getResponseCode() == 200) {
                    loadedKey = key;
                    return true;
                }
            } catch (Exception ignored) {
                // still loading
            }
            Thread.sleep(500);
        }
        stop();
        throw new RuntimeException("llama-server did not become ready in 180 s");
    }

    Result extract(List<byte[]> images, String systemPrompt, String userPrompt, JSONObject schema,
                   int maxTokens, double temperature, double repeatPenalty,
                   java.util.Set<String> truthWords) throws Exception {
        // images == null: a text-only request (the page's text layer is in userPrompt).
        List<String> b64 = null;
        Object content = userPrompt;
        if (images != null && !images.isEmpty()) {
            b64 = new ArrayList<>();
            JSONArray parts = new JSONArray().put(new JSONObject().put("type", "text").put("text", userPrompt));
            for (byte[] img : images) {
                String e = Base64.encodeToString(img, Base64.NO_WRAP);
                b64.add(e);
                // The MIME type only marks where the image goes in the prompt; the server
                // reads the actual format (JPEG or PNG) from the bytes themselves.
                parts.put(new JSONObject().put("type", "image_url").put("image_url",
                        new JSONObject().put("url", "data:" + (isJpeg(img) ? "image/jpeg" : "image/png") + ";base64," + e)));
            }
            content = parts;
        }
        JSONArray messages = new JSONArray()
                .put(new JSONObject().put("role", "system").put("content", systemPrompt))
                .put(new JSONObject().put("role", "user").put("content", content));

        // 1. The model's own chat template turns the messages into one prompt string,
        //    with a media marker where the image goes.
        JSONObject tpl = new JSONObject(post("/apply-template", new JSONObject()
                .put("messages", messages)
                .put("chat_template_kwargs", new JSONObject().put("enable_thinking", false))));
        String prompt = tpl.getString("prompt");

        // 2. /completion returns the generated text as-is. /v1/chat/completions re-parses
        //    the finished answer with a "peg-native" parser that, in this llama.cpp build,
        //    threw away valid pages (HTTP 500). The schema still constrains every token.
        JSONObject body = new JSONObject()
                .put("prompt", promptOf(prompt, b64))
                .put("json_schema", schema)
                .put("temperature", temperature)
                .put("repeat_penalty", repeatPenalty)
                .put("n_predict", maxTokens)
                .put("cache_prompt", true)
                .put("stream", true);
        Result r = stream(body, maxTokens, "", truthWords);
        r.truthWords = truthWords;
        r.templatePrompt = prompt;
        r.imageB64 = b64;
        r.repeatPenalty = repeatPenalty;
        return r;
    }

    /**
     * Carry on an answer the model closed too early: the same prompt plus the answer so
     * far (blocks array left open), constrained by a grammar that allows only further
     * blocks and then the closing brackets. The image and everything before the cut are
     * still in the server's cache, so nothing is read again -- only the new text costs.
     */
    Result continueFrom(Result first, String prefix, String grammar, int maxTokens) throws Exception {
        JSONObject body = new JSONObject()
                .put("prompt", promptOf(first.templatePrompt + prefix, first.imageB64))
                .put("grammar", grammar)
                .put("temperature", 0)
                .put("repeat_penalty", first.repeatPenalty)
                .put("n_predict", maxTokens)
                .put("cache_prompt", true)
                .put("stream", true);
        Result r = stream(body, maxTokens, prefix, first.truthWords);
        r.truthWords = first.truthWords;
        r.templatePrompt = first.templatePrompt;
        r.imageB64 = first.imageB64;
        r.repeatPenalty = first.repeatPenalty;
        return r;
    }

    /** Run one streamed /completion; content = prefix + generated text. Stops loops early. */
    private Result stream(JSONObject body, int maxTokens, String prefix,
                          java.util.Set<String> truthWords) throws Exception {
        long t0 = System.nanoTime();
        Result r = new Result();
        StringBuilder text = new StringBuilder(prefix);
        HttpURLConnection c = (HttpURLConnection) new URL(base() + "/completion").openConnection();
        c.setRequestMethod("POST");
        c.setDoOutput(true);
        c.setConnectTimeout(5000);
        c.setReadTimeout(30 * 60 * 1000);
        c.setRequestProperty("Content-Type", "application/json");
        try (OutputStream o = c.getOutputStream()) {
            o.write(body.toString().getBytes(StandardCharsets.UTF_8));
        }
        if (c.getResponseCode() >= 400) {
            throw new RuntimeException("/completion answered " + c.getResponseCode() + ": "
                    + read(c.getErrorStream()));
        }
        int chunks = 0;
        try (BufferedReader in = new BufferedReader(new java.io.InputStreamReader(
                c.getInputStream(), StandardCharsets.UTF_8))) {
            for (String line; (line = in.readLine()) != null; ) {
                if (!line.startsWith("data: ")) continue;
                JSONObject d = new JSONObject(line.substring(6));
                text.append(d.optString("content", ""));
                chunks++;
                if (d.optBoolean("stop")) {
                    JSONObject t = d.optJSONObject("timings");
                    if (t != null) {
                        r.promptTokens = t.optInt("prompt_n");
                        r.promptMs = t.optDouble("prompt_ms");
                        r.genTokens = t.optInt("predicted_n");
                        r.genMs = t.optDouble("predicted_ms");
                    }
                    // Hit n_predict: "stop_type": "limit" (older builds: stopped_limit).
                    boolean limit = "limit".equals(d.optString("stop_type"))
                            || d.optBoolean("stopped_limit")
                            || (r.genTokens > 0 && r.genTokens >= maxTokens);
                    r.finishReason = limit ? "length" : "stop";
                    break;
                }
                // Check for a loop every 32 tokens: cheap, and stops within seconds.
                if (chunks % 32 == 0 && PageGuard.looping(text)) {
                    r.finishReason = "loop";
                    r.genTokens = chunks;
                    break;              // closing the connection stops the server's task
                }
                if (chunks % 16 == 0 && PageGuard.finishedAndRepeating(text, truthWords)) {
                    r.finishReason = "done";    // page complete, model started over: stop
                    r.genTokens = chunks;
                    break;
                }
            }
        } finally {
            c.disconnect();
        }
        if (r.finishReason == null) r.finishReason = "disconnected";
        r.content = text.toString();
        r.wallMs = (System.nanoTime() - t0) / 1e6;
        return r;
    }

    /** The /completion prompt: text plus the image when there is one. */
    private static Object promptOf(String text, List<String> imagesB64) throws Exception {
        if (imagesB64 == null || imagesB64.isEmpty()) return text;
        JSONArray data = new JSONArray();
        for (String e : imagesB64) data.put(e);
        return new JSONObject().put("prompt_string", text).put("multimodal_data", data);
    }

    private static boolean isJpeg(byte[] b) {
        return b.length > 2 && (b[0] & 0xff) == 0xFF && (b[1] & 0xff) == 0xD8;
    }

    private String post(String path, JSONObject body) throws Exception {
        HttpURLConnection c = (HttpURLConnection) new URL(base() + path).openConnection();
        c.setRequestMethod("POST");
        c.setDoOutput(true);
        c.setConnectTimeout(5000);
        c.setReadTimeout(30 * 60 * 1000);
        c.setRequestProperty("Content-Type", "application/json");
        try (OutputStream o = c.getOutputStream()) {
            o.write(body.toString().getBytes(StandardCharsets.UTF_8));
        }
        int code = c.getResponseCode();
        String text = read(code < 400 ? c.getInputStream() : c.getErrorStream());
        if (code >= 400) throw new RuntimeException(path + " answered " + code + ": " + text);
        return text;
    }

    /** Peak resident memory of the server process, in MB, or -1 if unknown. */
    long peakRssMb() {
        try {
            java.lang.reflect.Field f = process.getClass().getDeclaredField("pid");
            f.setAccessible(true);
            int pid = f.getInt(process);
            try (BufferedReader r = new BufferedReader(new FileReader("/proc/" + pid + "/status"))) {
                for (String line; (line = r.readLine()) != null; ) {
                    if (line.startsWith("VmHWM:")) {
                        return Long.parseLong(line.replaceAll("[^0-9]", "")) / 1024;
                    }
                }
            }
        } catch (Exception ignored) {
        }
        return -1;
    }

    synchronized void stop() {
        if (process != null) {
            process.destroy();
            try {
                process.waitFor();
            } catch (InterruptedException ignored) {
            }
        }
        process = null;
        loadedKey = null;
    }

    private String base() {
        return "http://127.0.0.1:" + port;
    }

    private static int freePort() throws Exception {
        try (ServerSocket s = new ServerSocket(0)) {
            return s.getLocalPort();
        }
    }

    static String read(InputStream in) throws Exception {
        if (in == null) return "";
        // Not InputStream.readAllBytes(): that needs Android 13, the app supports 9+.
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        byte[] buf = new byte[16384];
        for (int n; (n = in.read(buf)) > 0; ) out.write(buf, 0, n);
        in.close();
        return out.toString("UTF-8");
    }
}
