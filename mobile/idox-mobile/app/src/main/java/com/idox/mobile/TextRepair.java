package com.idox.mobile;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

/**
 * Text mode only: checks the model's blocks against the page's text layer (the same
 * Markdown the model was given) and repairs what small models get wrong in structure.
 * The words themselves are never invented or changed -- only text that exists in the
 * text layer is put back, and only blocks that repeat other blocks are removed.
 *
 *  1. A line the model dropped goes back where it was printed: into its own paragraph
 *     (next to the neighbouring line it was printed with), else as its own block right
 *     after the block holding the line printed before it.
 *  2. A heading the model folded into another block is split out, in place.
 *  3. A text block made only of caption lines becomes a caption, only of body lines a
 *     paragraph, of a heading line a heading.
 *  4. A text block whose text is wholly contained in another block is dropped (e.g. a
 *     paragraph re-written as a "caption").
 *  Tables are left as the model wrote them.
 */
final class TextRepair {

    private TextRepair() {
    }

    /** One printed line of the Markdown: its kind and which paragraph it belongs to. */
    private static final class Line {
        String text, kind;   // kind: heading, caption, body, table
        int group;           // lines of one paragraph share a group
    }

    private static List<Line> parse(String markdown) {
        List<Line> out = new ArrayList<>();
        int group = 0;
        String prev = "";
        for (String raw : markdown.split("\n", -1)) {
            String s = raw.trim();
            if (s.isEmpty()) {           // blank line: paragraph break
                prev = "";
                continue;
            }
            Line l = new Line();
            if (s.startsWith("|")) {
                l.kind = "table";
                l.text = s.replaceAll("^\\|\\s*|\\s*\\|$", "").replaceAll("\\s*\\|\\s*", " ");
            } else if (s.startsWith("#")) {
                l.kind = "heading";
                l.text = s.replaceFirst("^#+\\s*", "");
            } else if (s.length() > 2 && s.startsWith("*") && s.endsWith("*")) {
                l.kind = "caption";
                l.text = s.substring(1, s.length() - 1).trim();
            } else {
                l.kind = "body";
                l.text = s;
            }
            if (!(l.kind.equals("body") && prev.equals("body")) && !(l.kind.equals("table") && prev.equals("table"))) group++;
            l.group = group;
            prev = l.kind;
            out.add(l);
        }
        return out;
    }

    static String norm(String s) {
        return s.toLowerCase(Locale.ROOT).replaceAll("[\\s\\u00a0]+", " ").trim();
    }

    private static String blockText(JSONObject b) {
        if ("table".equals(b.optString("kind"))) {
            StringBuilder sb = new StringBuilder();
            JSONArray h = b.optJSONArray("header");
            if (h != null) for (int i = 0; i < h.length(); i++) sb.append(h.optString(i)).append(' ');
            JSONArray rows = b.optJSONArray("rows");
            if (rows != null) {
                for (int r = 0; r < rows.length(); r++) {
                    JSONArray row = rows.optJSONArray(r);
                    if (row != null) for (int i = 0; i < row.length(); i++) sb.append(row.optString(i)).append(' ');
                }
            }
            return norm(sb.toString());
        }
        return norm(b.optString("text"));
    }

    private static JSONObject textBlock(String kind, String text) throws Exception {
        boolean heading = kind.equals("heading");
        return new JSONObject().put("kind", kind).put("text", text).put("align", "left")
                .put("size", heading ? "large" : "normal").put("bold", heading);
    }

    /** Repairs page in place; returns a short note of what changed ("" if nothing). */
    static String repair(JSONObject page, String markdown) throws Exception {
        JSONArray arr = page.getJSONArray("blocks");
        List<JSONObject> blocks = new ArrayList<>();
        for (int i = 0; i < arr.length(); i++) blocks.add(arr.getJSONObject(i));
        List<Line> lines = parse(markdown);
        int restored = 0, split = 0, relabelled = 0, dropped = 0;

        // 2. headings folded into another block: split them out, in place
        for (Line l : lines) {
            if (!l.kind.equals("heading") || l.text.length() < 3) continue;
            String h = norm(l.text);
            boolean asHeading = false;
            for (JSONObject b : blocks) {
                if ("heading".equals(b.optString("kind")) && blockText(b).equals(h)) asHeading = true;
            }
            if (asHeading) continue;
            for (int i = 0; i < blocks.size(); i++) {
                JSONObject b = blocks.get(i);
                if ("table".equals(b.optString("kind"))) {
                    // a heading taken as the table's first row / header
                    JSONArray hdr = b.optJSONArray("header");
                    if (hdr != null && norm(String.join(" ", toList(hdr))).equals(h)) {
                        JSONArray rows = b.optJSONArray("rows");
                        if (rows != null && rows.length() > 0) {
                            b.put("header", rows.getJSONArray(0));
                            JSONArray rest = new JSONArray();
                            for (int r = 1; r < rows.length(); r++) rest.put(rows.get(r));
                            b.put("rows", rest);
                        }
                        blocks.add(i, textBlock("heading", l.text));
                        split++;
                        break;
                    }
                    continue;
                }
                String t = b.optString("text");
                int at = norm(t).indexOf(h);
                if (at < 0) continue;
                String before = t.substring(0, Math.min(at, t.length())).trim();
                String after = t.length() >= at + l.text.length() ? t.substring(at + l.text.length()).trim() : "";
                List<JSONObject> parts = new ArrayList<>();
                if (!before.isEmpty()) parts.add(new JSONObject(b.toString()).put("text", before));
                parts.add(textBlock("heading", l.text));
                if (!after.isEmpty()) parts.add(new JSONObject(b.toString()).put("text", after));
                blocks.remove(i);
                blocks.addAll(i, parts);
                split++;
                break;
            }
        }

        // 1. dropped lines: back into their paragraph, or as a block in their place
        for (int li = 0; li < lines.size(); li++) {
            Line l = lines.get(li);
            if (l.kind.equals("table") || l.text.length() < 3) continue;
            String n = norm(l.text);
            boolean found = false;
            for (JSONObject b : blocks) if (blockText(b).contains(n)) { found = true; break; }
            if (found) continue;
            // the block holding the nearest earlier line (same paragraph first)
            int target = -1;
            boolean sameGroup = false;
            for (int k = li - 1; k >= 0 && target < 0; k--) {
                String pn = norm(lines.get(k).text);
                for (int b = 0; b < blocks.size(); b++) {
                    if (blockText(blocks.get(b)).contains(lastWords(pn))) {
                        target = b;
                        sameGroup = lines.get(k).group == l.group;
                        break;
                    }
                }
            }
            if (sameGroup && l.kind.equals("body") && !"table".equals(blocks.get(target).optString("kind"))) {
                JSONObject b = blocks.get(target);
                b.put("text", b.optString("text").trim() + " " + l.text);
            } else {
                String kind = l.kind.equals("body") ? "paragraph" : l.kind;
                blocks.add(target + 1, textBlock(kind, l.text));
            }
            restored++;
        }

        // 3. kind from the text layer: what kind of lines does each text block consist of?
        for (JSONObject b : blocks) {
            String k = b.optString("kind");
            if (k.equals("table") || k.equals("list") || k.equals("image")) continue;
            String t = blockText(b);
            String kinds = "";
            for (Line l : lines) {
                if (!l.kind.equals("table") && l.text.length() >= 3 && t.contains(norm(l.text))) {
                    if (!kinds.contains(l.kind)) kinds += l.kind + ",";
                }
            }
            String want = kinds.equals("caption,") ? "caption" : kinds.equals("body,") ? "paragraph"
                    : kinds.equals("heading,") ? "heading" : null;
            if (want != null && !want.equals(k)) {
                b.put("kind", want);
                relabelled++;
            }
        }

        // 4. a text block wholly contained in another block is a repeat: drop it
        //    (and a text block with no text at all)
        List<JSONObject> kept = new ArrayList<>();
        for (int i = 0; i < blocks.size(); i++) {
            JSONObject b = blocks.get(i);
            String t = blockText(b);
            if (!"table".equals(b.optString("kind")) && t.isEmpty()) {
                dropped++;
                continue;
            }
            boolean inside = false;
            if (!"table".equals(b.optString("kind")) && t.length() >= 25) {
                for (int j = 0; j < blocks.size(); j++) {
                    if (j == i) continue;
                    String o = blockText(blocks.get(j));
                    if (o.contains(t) && (o.length() > t.length() || j < i)) {
                        inside = true;
                        break;
                    }
                }
            }
            if (inside) dropped++;
            else kept.add(b);
        }

        // 5. table rows that are really a heading or a body line: take them out
        int moved = 0;
        for (int i = 0; i < kept.size(); i++) {
            JSONObject b = kept.get(i);
            if (!"table".equals(b.optString("kind"))) continue;
            JSONArray rows = b.optJSONArray("rows");
            if (rows == null) continue;
            JSONArray keepRows = new JSONArray();
            List<JSONObject> after = new ArrayList<>();
            for (int r = 0; r < rows.length(); r++) {
                JSONArray row = rows.optJSONArray(r);
                String rt = row == null ? "" : norm(String.join(" ", toList(row)));
                Line hit = null;
                for (Line l : lines) {
                    if (!l.kind.equals("table") && l.text.length() >= 3 && !rt.isEmpty()
                            && (rt.equals(norm(l.text)) || rt.contains(norm(l.text)))) { hit = l; break; }
                }
                if (hit == null) keepRows.put(row);
                else {
                    // keep the WHOLE row's text: one row can hold several printed lines
                    String rowText = String.join(" ", toList(row)).trim();
                    after.add(textBlock(hit.kind.equals("body") ? "paragraph" : hit.kind, rowText));
                    moved++;
                }
            }
            b.put("rows", keepRows);
            kept.addAll(i + 1, after);
        }

        // 6. order: every block goes where its first line was printed
        List<int[]> order = new ArrayList<>();      // {first line index, original index}
        for (int i = 0; i < kept.size(); i++) order.add(new int[]{firstLine(kept.get(i), lines), i});
        boolean reordered = false;
        for (int i = 1; i < order.size(); i++) if (order.get(i)[0] < order.get(i - 1)[0]) reordered = true;
        if (reordered) {
            order.sort((x, y) -> x[0] != y[0] ? Integer.compare(x[0], y[0]) : Integer.compare(x[1], y[1]));
            List<JSONObject> sorted = new ArrayList<>();
            for (int[] o : order) sorted.add(kept.get(o[1]));
            kept = sorted;
        }

        // 7. paragraph grouping: a text block holding several printed paragraphs is split,
        //    and neighbouring blocks that are pieces of one paragraph are joined
        List<JSONObject> grouped = new ArrayList<>();
        int regrouped = 0;
        for (JSONObject b : kept) {
            String k = b.optString("kind");
            if (k.equals("table") || k.equals("list") || k.equals("image")) { grouped.add(b); continue; }
            List<Integer> groups = groupsOf(b, lines);
            if (groups.size() > 1) {                                  // split
                for (int g : groups) {
                    StringBuilder t = new StringBuilder();
                    String gk = "body";
                    for (Line l : lines) if (l.group == g) { t.append(t.length() > 0 ? " " : "").append(l.text); gk = l.kind; }
                    grouped.add(new JSONObject(b.toString()).put("text", t.toString())
                            .put("kind", gk.equals("body") ? "paragraph" : gk));
                }
                regrouped++;
                continue;
            }
            if (!grouped.isEmpty() && groups.size() == 1) {           // join with the previous piece?
                JSONObject prev = grouped.get(grouped.size() - 1);
                List<Integer> pg = "table".equals(prev.optString("kind")) ? new ArrayList<>() : groupsOf(prev, lines);
                if (pg.size() == 1 && pg.get(0).equals(groups.get(0))) {
                    if (blockText(prev).contains(blockText(b))) {     // the same text again: drop it
                        regrouped++;
                        continue;
                    }
                    if (k.equals("paragraph") && "paragraph".equals(prev.optString("kind"))) {
                        prev.put("text", prev.optString("text").trim() + " " + b.optString("text").trim());
                        regrouped++;
                        continue;
                    }
                }
            }
            grouped.add(b);
        }
        kept = grouped;

        JSONArray out = new JSONArray();
        for (JSONObject b : kept) out.put(b);
        page.put("blocks", out);
        List<String> notes = new ArrayList<>();
        if (restored > 0) notes.add(restored + " missing line(s) restored");
        if (split > 0) notes.add(split + " heading(s) split out");
        if (relabelled > 0) notes.add(relabelled + " block kind(s) corrected");
        if (dropped > 0) notes.add(dropped + " repeated block(s) dropped");
        if (moved > 0) notes.add(moved + " text row(s) moved out of a table");
        if (reordered) notes.add("blocks put back in page order");
        if (regrouped > 0) notes.add(regrouped + " paragraph(s) re-joined or split");
        return String.join(", ", notes);
    }

    /** Index of the first printed line this block holds (tables: their first row), else MAX. */
    private static int firstLine(JSONObject b, List<Line> lines) {
        String t = blockText(b);
        boolean table = "table".equals(b.optString("kind"));
        for (int i = 0; i < lines.size(); i++) {
            Line l = lines.get(i);
            if (l.text.length() < 3 || table != l.kind.equals("table")) continue;
            if (t.contains(norm(l.text))) return i;
        }
        return Integer.MAX_VALUE;
    }

    /** The paragraph groups whose lines this text block holds, in page order. */
    private static List<Integer> groupsOf(JSONObject b, List<Line> lines) {
        String t = blockText(b);
        List<Integer> g = new ArrayList<>();
        for (Line l : lines) {
            if (l.kind.equals("table") || l.text.length() < 3) continue;
            if (t.contains(norm(l.text)) && !g.contains(l.group)) g.add(l.group);
        }
        return g;
    }

    /** The end of a line, to find the block holding it even if the model reflowed it. */
    private static String lastWords(String s) {
        return s.length() <= 40 ? s : s.substring(s.length() - 40).trim();
    }

    private static List<String> toList(JSONArray a) {
        List<String> l = new ArrayList<>();
        for (int i = 0; i < a.length(); i++) l.add(a.optString(i));
        return l;
    }
}
