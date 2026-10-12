package com.idox.mobile;

import android.content.Context;
import android.graphics.RectF;
import android.graphics.pdf.PdfRenderer;
import android.graphics.pdf.content.PdfPageTextContent;
import android.os.Build;
import android.os.ParcelFileDescriptor;

import com.tom_roush.pdfbox.android.PDFBoxResourceLoader;
import com.tom_roush.pdfbox.pdmodel.PDDocument;
import com.tom_roush.pdfbox.text.PDFTextStripper;
import com.tom_roush.pdfbox.text.TextPosition;

import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.Locale;

/**
 * Comparison only (the "Text layer test" button): pulls the text layer of a PDF with
 * Android's built-in PdfRenderer (Android 15+) and with PdfBox-Android, and writes what
 * each returns, with timings, so the two can be judged on the same file. Not used by
 * the conversion itself.
 */
final class TextLayerTest {

    private TextLayerTest() {
    }

    /** Runs both extractors; returns a one-line summary per extractor. */
    static String run(Context ctx, ParcelFileDescriptor fd, InputStream pdfStream, File outDir) throws Exception {
        outDir.mkdirs();
        StringBuilder summary = new StringBuilder();

        // 1. Android built-in: text runs with their bounding boxes, no font information.
        if (Build.VERSION.SDK_INT >= 35) {
            long t0 = System.nanoTime();
            StringBuilder out = new StringBuilder();
            int runs = 0, chars = 0;
            try (PdfRenderer pdf = new PdfRenderer(fd)) {
                for (int p = 0; p < pdf.getPageCount(); p++) {
                    out.append("=== page ").append(p + 1).append(" ===\n");
                    try (PdfRenderer.Page page = pdf.openPage(p)) {
                        for (PdfPageTextContent c : page.getTextContents()) {
                            runs++;
                            chars += c.getText().length();
                            List<RectF> b = c.getBounds();
                            out.append(String.format(Locale.ROOT, "[%d boxes%s] ", b.size(),
                                    b.isEmpty() ? "" : String.format(Locale.ROOT, ", first x=%.0f y=%.0f h=%.0f",
                                            b.get(0).left, b.get(0).top, b.get(0).height())));
                            out.append(c.getText()).append("\n");
                        }
                    }
                }
            }
            double ms = (System.nanoTime() - t0) / 1e6;
            write(new File(outDir, "android_builtin.txt"), out.toString());
            summary.append(String.format(Locale.ROOT, "Android built-in: %d text runs, %d chars, %.0f ms\n", runs, chars, ms));
        } else {
            summary.append("Android built-in: not available below Android 15\n");
        }

        // 2. PdfBox-Android: text plus each character's position, font and size.
        PDFBoxResourceLoader.init(ctx);
        long t0 = System.nanoTime();
        StringBuilder detailed = new StringBuilder();
        String plain;
        try (PDDocument doc = PDDocument.load(pdfStream)) {
            PDFTextStripper stripper = new PDFTextStripper() {
                @Override
                protected void writeString(String text, List<TextPosition> pos) throws java.io.IOException {
                    if (!pos.isEmpty()) {
                        TextPosition f = pos.get(0);
                        String font = f.getFont() == null ? "?" : f.getFont().getName();
                        detailed.append(String.format(Locale.ROOT, "[x=%.0f y=%.0f size=%.1f font=%s] %s\n",
                                f.getXDirAdj(), f.getYDirAdj(), f.getFontSizeInPt(), font, text));
                    }
                    super.writeString(text, pos);
                }
            };
            stripper.setSortByPosition(true);
            plain = stripper.getText(doc);
        }
        double ms = (System.nanoTime() - t0) / 1e6;
        write(new File(outDir, "pdfbox_plain.txt"), plain);
        write(new File(outDir, "pdfbox_detailed.txt"), detailed.toString());
        summary.append(String.format(Locale.ROOT, "PdfBox-Android: %d chars, %.0f ms (incl. first-use init)\n",
                plain.length(), ms));
        write(new File(outDir, "summary.txt"), summary.toString());
        return summary.toString();
    }

    private static void write(File f, String text) throws Exception {
        try (FileOutputStream o = new FileOutputStream(f)) {
            o.write(text.getBytes(StandardCharsets.UTF_8));
        }
    }
}
