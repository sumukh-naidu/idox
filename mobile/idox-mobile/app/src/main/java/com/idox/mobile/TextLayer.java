package com.idox.mobile;

import android.content.Context;

import com.tom_roush.pdfbox.android.PDFBoxResourceLoader;
import com.tom_roush.pdfbox.cos.COSName;
import com.tom_roush.pdfbox.pdmodel.PDDocument;
import com.tom_roush.pdfbox.pdmodel.PDPage;
import com.tom_roush.pdfbox.pdmodel.PDResources;
import com.tom_roush.pdfbox.pdmodel.graphics.image.PDImageXObject;
import com.tom_roush.pdfbox.text.PDFTextStripper;
import com.tom_roush.pdfbox.text.TextPosition;

import java.io.IOException;
import java.io.InputStream;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

/**
 * The text layer of a digital PDF, per page, written for the model to read instead of a
 * page image (PdfBox-Android, Apache-2.0).
 *
 * Written as Markdown, one printed line per row, top to bottom -- a format small models
 * know well (a first version with "[18 bold]" tags confused the 1B):
 *
 *   # Employee Onboarding Summary            title: much larger than the body text
 *   This document summarizes new ...         body line
 *   ## Department Headcount                  heading: larger, or a short bold line
 *   | Department | New Hires | Pending |      a row whose pieces are far apart: table cells
 *   *Figure 1: Warehouse safety notice*      italic line, usually a caption
 */
final class TextLayer {

    static final class Page {
        String text = "";        // the formatted lines, as above
        String plain = "";       // just the words, for checking the model's output
        int lines, images;
        int expectedBlocks;      // headings + captions + tables + paragraphs, counted from the layout
        // Scanned page (no text layer): the image(s) stored in the PDF, taken out as they
        // are -- a JPEG's own bytes, other formats' stored pixels as PNG. No page drawing.
        List<byte[]> scanImages = new ArrayList<>();
        String scanInfo = "";
    }

    private TextLayer() {
    }

    static List<Page> extract(Context ctx, InputStream pdf) throws IOException {
        PDFBoxResourceLoader.init(ctx);
        List<Page> pages = new ArrayList<>();
        try (PDDocument doc = PDDocument.load(pdf)) {
            for (int p = 1; p <= doc.getNumberOfPages(); p++) {
                Collector c = new Collector();
                c.setSortByPosition(true);
                c.setStartPage(p);
                c.setEndPage(p);
                c.getText(doc);
                Page page = c.finish();
                page.images = countImages(doc.getPage(p - 1));
                if (page.plain.length() < 20) takeScanImages(doc.getPage(p - 1), page);
                pages.add(page);
            }
        }
        return pages;
    }

    /**
     * The scan itself, straight out of the PDF: every image the page uses, in the order the
     * page lists them. JPEG (DCTDecode) streams are passed on byte for byte, undecoded;
     * anything else is the stored pixels re-encoded losslessly as PNG.
     */
    private static void takeScanImages(PDPage page, Page out) {
        try {
            PDResources res = page.getResources();
            if (res == null) return;
            StringBuilder info = new StringBuilder();
            for (COSName name : res.getXObjectNames()) {
                if (!(res.getXObject(name) instanceof PDImageXObject)) continue;
                PDImageXObject img = (PDImageXObject) res.getXObject(name);
                byte[] bytes;
                String how;
                if ("jpg".equals(img.getSuffix())) {
                    try (InputStream in = img.getStream().createInputStream(
                            java.util.Collections.singletonList(COSName.DCT_DECODE.getName()))) {
                        java.io.ByteArrayOutputStream bo = new java.io.ByteArrayOutputStream();
                        byte[] buf = new byte[65536];
                        for (int n; (n = in.read(buf)) > 0; ) bo.write(buf, 0, n);
                        bytes = bo.toByteArray();
                    }
                    how = "JPEG as stored";
                } else {
                    // Not PdfBox's getImage(): for DeviceGray it put the gray values into the
                    // alpha channel over white, which saved as a blank white page. The stored
                    // samples are read directly instead and turned into pixels here.
                    bytes = samplesToPng(img);
                    if (bytes == null) {
                        throw new IllegalStateException("unsupported image format ("
                                + img.getColorSpace().getName() + ", " + img.getBitsPerComponent() + " bits)");
                    }
                    how = "stored pixels as PNG";
                }
                out.scanImages.add(bytes);
                if (info.length() > 0) info.append("; ");
                info.append(img.getWidth()).append("x").append(img.getHeight()).append(" ").append(how);
            }
            out.scanInfo = info.toString();
        } catch (Exception e) {
            out.scanImages.clear();
            out.scanInfo = "could not take the image out: " + e.getMessage();
        }
    }

    /**
     * PNG made from an image's stored samples: gray or RGB, 8 bits, or 1-bit black and
     * white, honouring an inverting /Decode [1 0]. Null for anything else (CMYK, indexed,
     * 16-bit, JPEG 2000), which the caller treats as "cannot take it out".
     */
    private static byte[] samplesToPng(PDImageXObject img) throws IOException {
        int w = img.getWidth(), h = img.getHeight(), bpc = img.getBitsPerComponent();
        String cs = img.getColorSpace() == null ? "" : img.getColorSpace().getName();
        int comps = cs.equals("DeviceGray") || cs.equals("CalGray") ? 1
                : cs.equals("DeviceRGB") || cs.equals("CalRGB") ? 3 : 0;
        if (img.isStencil()) comps = 1;
        if (comps == 0 || (bpc != 8 && !(bpc == 1 && comps == 1))) return null;
        boolean invert = false;
        com.tom_roush.pdfbox.cos.COSArray decode = img.getDecode();
        if (decode != null && decode.size() >= 2) {
            invert = decode.getObject(0) instanceof com.tom_roush.pdfbox.cos.COSNumber
                    && ((com.tom_roush.pdfbox.cos.COSNumber) decode.getObject(0)).floatValue() > 0.5f;
        }
        if (img.isStencil()) invert = !invert;    // stencil: 0 = paint (black)

        byte[] data;
        try (InputStream in = img.createInputStream()) {
            java.io.ByteArrayOutputStream bo = new java.io.ByteArrayOutputStream();
            byte[] buf = new byte[65536];
            for (int n; (n = in.read(buf)) > 0; ) bo.write(buf, 0, n);
            data = bo.toByteArray();
        }
        int rowBytes = bpc == 1 ? (w + 7) / 8 : w * comps;
        if (data.length < rowBytes * h) return null;
        int[] px = new int[w * h];
        for (int y = 0; y < h; y++) {
            int row = y * rowBytes;
            for (int x = 0; x < w; x++) {
                int r, g, b;
                if (bpc == 1) {
                    int bit = (data[row + (x >> 3)] >> (7 - (x & 7))) & 1;
                    r = g = b = bit == 1 ? 255 : 0;
                } else if (comps == 1) {
                    r = g = b = data[row + x] & 0xff;
                } else {
                    int o = row + x * 3;
                    r = data[o] & 0xff; g = data[o + 1] & 0xff; b = data[o + 2] & 0xff;
                }
                if (invert) { r = 255 - r; g = 255 - g; b = 255 - b; }
                px[y * w + x] = 0xff000000 | (r << 16) | (g << 8) | b;
            }
        }
        android.graphics.Bitmap bmp = android.graphics.Bitmap.createBitmap(px, w, h, android.graphics.Bitmap.Config.ARGB_8888);
        java.io.ByteArrayOutputStream bo = new java.io.ByteArrayOutputStream();
        bmp.compress(android.graphics.Bitmap.CompressFormat.PNG, 100, bo);
        bmp.recycle();
        return bo.toByteArray();
    }

    /** True if an image is (almost) one flat colour -- a failed extraction, not a scan. */
    static boolean looksBlank(byte[] image) {
        android.graphics.BitmapFactory.Options o = new android.graphics.BitmapFactory.Options();
        o.inSampleSize = 8;                       // a small copy is enough to judge
        android.graphics.Bitmap b = android.graphics.BitmapFactory.decodeByteArray(image, 0, image.length, o);
        if (b == null) return true;
        int lo = 255, hi = 0;
        for (int y = 0; y < b.getHeight(); y += 2) {
            for (int x = 0; x < b.getWidth(); x += 2) {
                int c = b.getPixel(x, y);
                int v = (((c >> 16) & 0xff) * 3 + ((c >> 8) & 0xff) * 6 + (c & 0xff)) / 10;
                lo = Math.min(lo, v);
                hi = Math.max(hi, v);
            }
        }
        b.recycle();
        return hi - lo < 40;
    }

    /** Images drawn on the page: their text is NOT in the text layer. */
    private static int countImages(PDPage page) {
        int n = 0;
        try {
            PDResources res = page.getResources();
            if (res == null) return 0;
            for (COSName name : res.getXObjectNames()) {
                if (res.getXObject(name) instanceof PDImageXObject) n++;
            }
        } catch (Exception ignored) {
        }
        return n;
    }

    /** Gathers PdfBox's text pieces and groups them into printed lines. */
    private static final class Collector extends PDFTextStripper {
        private final List<float[]> ys = new ArrayList<>();          // y of each line
        private final List<List<Piece>> rows = new ArrayList<>();

        Collector() throws IOException {
            super();
        }

        @Override
        protected void writeString(String text, List<TextPosition> pos) {
            if (pos.isEmpty() || text.trim().isEmpty()) return;
            TextPosition f = pos.get(0);
            Piece piece = new Piece();
            piece.text = text.trim();
            piece.x = f.getXDirAdj();
            piece.size = f.getFontSizeInPt();
            String font = f.getFont() == null ? "" : f.getFont().getName();
            font = font == null ? "" : font.toLowerCase(Locale.ROOT);
            piece.bold = font.contains("bold") || font.contains("black") || font.contains("heavy");
            piece.italic = font.contains("italic") || font.contains("oblique");
            float y = f.getYDirAdj();
            // Same line if the baseline is within a third of the font size.
            for (int i = 0; i < ys.size(); i++) {
                if (Math.abs(ys.get(i)[0] - y) <= Math.max(2f, piece.size / 3f)) {
                    rows.get(i).add(piece);
                    return;
                }
            }
            ys.add(new float[]{y});
            List<Piece> row = new ArrayList<>();
            row.add(piece);
            rows.add(row);
        }

        Page finish() {
            // order lines top to bottom, pieces left to right
            List<Integer> order = new ArrayList<>();
            for (int i = 0; i < rows.size(); i++) order.add(i);
            order.sort((a, b) -> Float.compare(ys.get(a)[0], ys.get(b)[0]));

            // Body size: the font size that carries the most characters on the page.
            java.util.Map<Integer, Integer> chars = new java.util.HashMap<>();
            for (List<Piece> row : rows) {
                for (Piece pc : row) chars.merge(Math.round(pc.size), pc.text.length(), Integer::sum);
            }
            int body = 10, most = -1;
            for (java.util.Map.Entry<Integer, Integer> e : chars.entrySet()) {
                if (e.getValue() > most) {
                    most = e.getValue();
                    body = e.getKey();
                }
            }

            StringBuilder out = new StringBuilder(), plain = new StringBuilder();
            // Block count for the model's upper limit: every heading and caption is one
            // block, a run of table rows is one, and a run of body lines is one paragraph
            // -- broken where the vertical gap to the previous line is clearly larger than
            // ordinary line spacing (a blank line in the Markdown marks that break).
            int blocks = 0;
            String prevKind = "";
            float prevY = Float.NaN, prevSize = body;
            for (int i : order) {
                List<Piece> row = rows.get(i);
                row.sort((a, b) -> Float.compare(a.x, b.x));
                Piece first = row.get(0);
                float y = ys.get(i)[0];
                for (Piece pc : row) plain.append(pc.text).append(' ');
                String kind;
                if (row.size() > 1) {                                   // table row
                    kind = "table";
                    out.append("|");
                    for (Piece pc : row) out.append(' ').append(pc.text).append(" |");
                } else if (first.size >= body * 1.6f) {
                    kind = "heading";
                    out.append("# ").append(first.text);
                } else if (first.size > body * 1.15f || (first.bold && first.text.length() < 60)) {
                    kind = "heading";
                    out.append("## ").append(first.text);
                } else if (first.italic) {
                    kind = "caption";
                    out.append('*').append(first.text).append('*');
                } else {
                    kind = "body";
                    boolean gap = !Float.isNaN(prevY) && (y - prevY) > Math.max(prevSize, first.size) * 1.8f;
                    if (prevKind.equals("body") && gap) {
                        out.insert(out.length(), "\n");                   // paragraph break
                        blocks++;
                    }
                    out.append(first.text);
                }
                if (!kind.equals(prevKind) && !(kind.equals("body") && prevKind.equals("body"))) {
                    if (!(kind.equals("table") && prevKind.equals("table"))) blocks++;
                } else if (kind.equals("heading") || kind.equals("caption")) {
                    blocks++;                                           // two headings in a row
                }
                out.append('\n');
                prevKind = kind;
                prevY = y;
                prevSize = first.size;
            }
            Page p = new Page();
            p.text = out.toString();
            p.plain = plain.toString().trim();
            p.lines = rows.size();
            p.expectedBlocks = blocks;
            return p;
        }
    }

    private static final class Piece {
        String text;
        float x, size;
        boolean bold, italic;
    }
}
