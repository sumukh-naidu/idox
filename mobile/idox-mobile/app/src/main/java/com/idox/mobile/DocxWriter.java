package com.idox.mobile;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.OutputStream;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.zip.ZipEntry;
import java.util.zip.ZipOutputStream;

/**
 * Writes the extracted Page blocks as a Word .docx, the phone's version of to_docx.py.
 *
 * No model involved and no library: a .docx is a zip of a few XML parts, written here
 * directly (Apache POI would add ~15 MB and is awkward on Android). Same mapping as the
 * laptop writer: headings bold, size large/normal/small, align left/center/right,
 * captions italic, tables with borders and a bold header row, one PDF page per Word page.
 */
final class DocxWriter {

    private DocxWriter() {
    }

    /** pages: one JSON "Page" object per PDF page, as the model returned it. */
    static void write(List<JSONObject> pages, OutputStream dest) throws Exception {
        StringBuilder body = new StringBuilder();
        for (int p = 0; p < pages.size(); p++) {
            if (p > 0) body.append("<w:p><w:r><w:br w:type=\"page\"/></w:r></w:p>");
            JSONArray blocks = pages.get(p).optJSONArray("blocks");
            if (blocks == null) continue;
            for (int i = 0; i < blocks.length(); i++) {
                JSONObject b = blocks.getJSONObject(i);
                // A picture with nothing printed in it has text "": nothing to write.
                if ("image".equals(b.optString("kind")) && b.optString("text").trim().isEmpty()) continue;
                body.append("table".equals(b.optString("kind")) ? table(b) : paragraph(b));
            }
        }
        // Letter page, 1 inch margins (sectPr must be the last child of the body).
        body.append("<w:sectPr><w:pgSz w:w=\"12240\" w:h=\"15840\"/>"
                + "<w:pgMar w:top=\"1440\" w:right=\"1440\" w:bottom=\"1440\" w:left=\"1440\""
                + " w:header=\"720\" w:footer=\"720\" w:gutter=\"0\"/></w:sectPr>");

        try (ZipOutputStream zip = new ZipOutputStream(dest)) {
            put(zip, "[Content_Types].xml",
                    "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>"
                            + "<Types xmlns=\"http://schemas.openxmlformats.org/package/2006/content-types\">"
                            + "<Default Extension=\"rels\" ContentType=\"application/vnd.openxmlformats-package.relationships+xml\"/>"
                            + "<Default Extension=\"xml\" ContentType=\"application/xml\"/>"
                            + "<Override PartName=\"/word/document.xml\" ContentType=\"application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml\"/>"
                            + "</Types>");
            put(zip, "_rels/.rels",
                    "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>"
                            + "<Relationships xmlns=\"http://schemas.openxmlformats.org/package/2006/relationships\">"
                            + "<Relationship Id=\"rId1\" Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument\" Target=\"word/document.xml\"/>"
                            + "</Relationships>");
            put(zip, "word/document.xml",
                    "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>"
                            + "<w:document xmlns:w=\"http://schemas.openxmlformats.org/wordprocessingml/2006/main\">"
                            + "<w:body>" + body + "</w:body></w:document>");
        }
    }

    private static String paragraph(JSONObject b) {
        String kind = b.optString("kind");
        boolean heading = kind.equals("heading");
        boolean italic = kind.equals("caption") || kind.equals("image");
        boolean bold = b.optBoolean("bold", false) || heading;
        String size = b.optString("size", "normal");
        // half-points: large 16 pt, normal 11 pt, small 9 pt; headings at least 13 pt
        int halfPt = size.equals("large") ? 32 : size.equals("small") ? 18 : (heading ? 26 : 22);
        String jc = b.optString("align", "left");
        if (!jc.equals("center") && !jc.equals("right")) jc = "left";

        StringBuilder rPr = new StringBuilder("<w:rPr><w:rFonts w:ascii=\"Calibri\" w:hAnsi=\"Calibri\"/>");
        if (bold) rPr.append("<w:b/>");
        if (italic) rPr.append("<w:i/>");
        rPr.append("<w:sz w:val=\"").append(halfPt).append("\"/></w:rPr>");

        StringBuilder p = new StringBuilder("<w:p><w:pPr>");
        p.append("<w:spacing w:before=\"").append(heading ? 240 : 0).append("\" w:after=\"160\"/>");
        p.append("<w:jc w:val=\"").append(jc).append("\"/></w:pPr>");
        // Separate printed lines (an address, a list) keep their line breaks.
        String[] lines = b.optString("text").replace("\\n", "\n").split("\n", -1);
        for (int i = 0; i < lines.length; i++) {
            p.append("<w:r>").append(rPr);
            if (i > 0) p.append("<w:br/>");
            p.append("<w:t xml:space=\"preserve\">").append(esc(lines[i])).append("</w:t></w:r>");
        }
        return p.append("</w:p>").toString();
    }

    private static String table(JSONObject b) throws Exception {
        JSONArray header = b.optJSONArray("header");
        JSONArray rows = b.optJSONArray("rows");
        int cols = b.optInt("n_cols", 0);
        if (header != null) cols = Math.max(cols, header.length());
        if (rows != null) {
            for (int r = 0; r < rows.length(); r++) cols = Math.max(cols, rows.getJSONArray(r).length());
        }
        if (cols == 0) return "";

        int colW = 9360 / cols;     // the 6.5 in text width, shared equally
        String border = "w:val=\"single\" w:sz=\"4\" w:space=\"0\" w:color=\"808080\"";
        StringBuilder t = new StringBuilder("<w:tbl><w:tblPr><w:tblW w:w=\"9360\" w:type=\"dxa\"/>"
                + "<w:tblBorders><w:top " + border + "/><w:left " + border + "/><w:bottom " + border + "/>"
                + "<w:right " + border + "/><w:insideH " + border + "/><w:insideV " + border + "/></w:tblBorders>"
                + "<w:tblCellMar><w:left w:w=\"80\" w:type=\"dxa\"/><w:right w:w=\"80\" w:type=\"dxa\"/></w:tblCellMar>"
                + "</w:tblPr><w:tblGrid>");
        for (int c = 0; c < cols; c++) t.append("<w:gridCol w:w=\"").append(colW).append("\"/>");
        t.append("</w:tblGrid>");
        // A header of only empty strings (the model's way of saying "no header") is skipped.
        if (header != null && !header.toString().replaceAll("[\\[\\]\",\\s]", "").isEmpty()) {
            t.append(row(header, cols, colW, true));
        }
        if (rows != null) {
            for (int r = 0; r < rows.length(); r++) t.append(row(rows.getJSONArray(r), cols, colW, false));
        }
        // A paragraph after the table keeps Word from merging two adjacent tables.
        return t.append("</w:tbl><w:p/>").toString();
    }

    /** One row, padded with empty cells so every row has the table's column count. */
    private static String row(JSONArray cells, int cols, int colW, boolean bold) {
        StringBuilder r = new StringBuilder("<w:tr>");
        for (int c = 0; c < cols; c++) {
            String text = c < cells.length() ? cells.optString(c) : "";
            r.append("<w:tc><w:tcPr><w:tcW w:w=\"").append(colW).append("\" w:type=\"dxa\"/></w:tcPr>")
                    .append("<w:p><w:pPr><w:spacing w:after=\"0\"/></w:pPr><w:r><w:rPr>")
                    .append("<w:rFonts w:ascii=\"Calibri\" w:hAnsi=\"Calibri\"/>")
                    .append(bold ? "<w:b/>" : "").append("<w:sz w:val=\"20\"/></w:rPr>")
                    .append("<w:t xml:space=\"preserve\">").append(esc(text)).append("</w:t></w:r></w:p></w:tc>");
        }
        return r.append("</w:tr>").toString();
    }

    private static void put(ZipOutputStream zip, String name, String xml) throws Exception {
        zip.putNextEntry(new ZipEntry(name));
        zip.write(xml.getBytes(StandardCharsets.UTF_8));
        zip.closeEntry();
    }

    /** XML-escape, and drop control characters Word refuses to open. */
    private static String esc(String s) {
        StringBuilder o = new StringBuilder(s.length());
        for (int i = 0; i < s.length(); i++) {
            char ch = s.charAt(i);
            switch (ch) {
                case '&': o.append("&amp;"); break;
                case '<': o.append("&lt;"); break;
                case '>': o.append("&gt;"); break;
                case '"': o.append("&quot;"); break;
                default:
                    if (ch >= 0x20 || ch == '\t') o.append(ch);
            }
        }
        return o.toString();
    }
}
