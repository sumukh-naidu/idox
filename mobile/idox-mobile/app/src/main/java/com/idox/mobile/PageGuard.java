package com.idox.mobile;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.HashMap;
import java.util.HashSet;
import java.util.Map;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Safety nets around one page's model output. None of it knows anything about a
 * particular document; it only looks at the shape of the answer.
 *
 *  - looping():  the answer is repeating itself (the same block text written again and
 *                again), so generation can be stopped instead of running to the limit.
 *  - salvage():  an answer cut off part-way (loop or token limit) keeps every block that
 *                was finished before the cut, instead of losing the whole page.
 *  - dedupe():   exact repeats of a block are dropped, as blocks.py's
 *                drop_duplicate_blocks() does on the laptop.
 */
final class PageGuard {

    private PageGuard() {
    }

    // A block's text value in the streamed JSON: "text": "....", with escapes.
    private static final Pattern TEXT = Pattern.compile("\"text\"\\s*:\\s*\"((?:[^\"\\\\]|\\\\.)*)\"");

    /** Text values at least this long that appear this many times mean a loop. */
    private static final int MIN_LEN = 12, LOOP_REPEATS = 3;

    // A JSON string token. Matched one after another (find() resumes after each closing
    // quote, so tokens never overlap); a token followed by ':' is a key and is skipped.
    private static final Pattern STRING = Pattern.compile("\"((?:[^\"\\\\]|\\\\.)*)\"");

    /** Consecutive values that, repeated LOOP_REPEATS times, mean a loop (e.g. table rows). */
    private static final int RUN = 6, RUN_MIN_CHARS = 20;

    /**
     * True when the answer is repeating itself:
     *  - a block text of a useful length has been written LOOP_REPEATS times, or
     *  - the same run of RUN consecutive values (table cells, texts) has appeared
     *    LOOP_REPEATS times -- how a looping table shows up (a 2B run re-wrote the same
     *    table rows until the token limit, which the text check alone could not see).
     * A run must carry RUN_MIN_CHARS of text, so tables of empty or one-letter cells
     * that legitimately repeat do not count.
     */
    static boolean looping(CharSequence partial) {
        Map<String, Integer> seen = new HashMap<>();
        Matcher m = TEXT.matcher(partial);
        while (m.find()) {
            String t = m.group(1).trim();
            if (t.length() < MIN_LEN) continue;
            if (seen.merge(t, 1, Integer::sum) >= LOOP_REPEATS) return true;
        }

        java.util.List<String> values = new java.util.ArrayList<>();
        Matcher v = STRING.matcher(partial);
        while (v.find()) {
            int k = v.end();
            while (k < partial.length() && Character.isWhitespace(partial.charAt(k))) k++;
            if (k < partial.length() && partial.charAt(k) == ':') continue;     // a key
            values.add(v.group(1).trim());
        }
        Map<String, Integer> runs = new HashMap<>();
        for (int i = 0; i + RUN <= values.size(); i++) {
            java.util.List<String> run = values.subList(i, i + RUN);
            int chars = 0;
            for (String x : run) chars += x.length();
            if (chars < RUN_MIN_CHARS) continue;
            if (runs.merge(String.join("\u0001", run), 1, Integer::sum) >= LOOP_REPEATS) return true;
        }
        return false;
    }

    /** Words of a text, lower case, punctuation trimmed -- for comparing with the text layer. */
    static Set<String> words(CharSequence s) {
        Set<String> w = new HashSet<>();
        for (String x : s.toString().toLowerCase(java.util.Locale.ROOT).split("[^\\p{L}\\p{N}%+.,:/-]+")) {
            x = x.replaceAll("^[.,:]+|[.,:]+$", "");
            if (!x.isEmpty()) w.add(x);
        }
        return w;
    }

    /** The page's words (from its text layer) that are not in the model's blocks. */
    static Set<String> missingWords(String truth, JSONArray blocks) throws Exception {
        StringBuilder sb = new StringBuilder();
        for (int k = 0; k < blocks.length(); k++) {
            JSONObject b = blocks.getJSONObject(k);
            if ("table".equals(b.optString("kind"))) {
                sb.append(b.optJSONArray("header")).append(' ').append(b.optJSONArray("rows")).append(' ');
            } else {
                sb.append(b.optString("text")).append(' ');
            }
        }
        Set<String> missing = new java.util.TreeSet<>(words(truth));
        missing.removeAll(words(sb));
        return missing;
    }

    /**
     * Text mode only: every word of the page is already in the answer AND a block text
     * has been written a second time -- the model has finished the page and started
     * copying it again (a 2B run re-wrote page 1 from the top). Stopping here keeps the
     * complete page and drops ~half the writing; dedupe() removes the repeated block.
     */
    static boolean finishedAndRepeating(CharSequence partial, Set<String> truthWords) {
        if (truthWords == null || truthWords.isEmpty()) return false;
        Map<String, Integer> seen = new HashMap<>();
        boolean repeated = false;
        Matcher m = TEXT.matcher(partial);
        while (m.find()) {
            String t = m.group(1).trim();
            if (t.length() >= MIN_LEN && seen.merge(t, 1, Integer::sum) >= 2) {
                repeated = true;
                break;
            }
        }
        if (!repeated) return false;
        // Count only COMPLETED blocks: a word still being written in an unfinished block
        // would be dropped when the page is salvaged, so it does not count as written.
        String done = openPrefix(partial.toString());
        return done != null && words(done).containsAll(truthWords);
    }

    /**
     * The longest valid Page that can be made from a cut-off answer: the analysis plus
     * every block whose closing brace was written. Null if not even that much exists.
     */
    static JSONObject salvage(String partial) {
        int blocksAt = partial.indexOf("\"blocks\"");
        if (blocksAt < 0) return null;
        int arrayAt = partial.indexOf('[', blocksAt);
        if (arrayAt < 0) return null;

        int lastEnd = lastBlockEnd(partial, arrayAt);
        String json = partial.substring(0, arrayAt + 1)
                + (lastEnd > 0 ? partial.substring(arrayAt + 1, lastEnd + 1) : "") + "]}";
        try {
            return new JSONObject(json);
        } catch (Exception e) {
            return null;
        }
    }

    /**
     * The answer cut back to the end of its last complete block, with the blocks array
     * left open -- the starting point for LlamaServer.continueFrom(). Null if no block
     * was completed (then there is nothing to continue from).
     */
    static String openPrefix(String answer) {
        int blocksAt = answer.indexOf("\"blocks\"");
        int arrayAt = blocksAt < 0 ? -1 : answer.indexOf('[', blocksAt);
        if (arrayAt < 0) return null;
        int end = lastBlockEnd(answer, arrayAt);
        return end < 0 ? null : answer.substring(0, end + 1);
    }

    /** Index of the closing brace of the last complete element of the array at arrayAt. */
    private static int lastBlockEnd(String s, int arrayAt) {
        int depth = 0, lastEnd = -1;
        boolean inString = false, escaped = false;
        for (int i = arrayAt + 1; i < s.length(); i++) {
            char c = s.charAt(i);
            if (inString) {
                if (escaped) escaped = false;
                else if (c == '\\') escaped = true;
                else if (c == '"') inString = false;
                continue;
            }
            if (c == '"') inString = true;
            else if (c == '{' || c == '[') depth++;
            else if (c == '}' || c == ']') {
                depth--;
                if (depth == 0 && c == '}') lastEnd = i;
                if (depth < 0) break;
            }
        }
        return lastEnd;
    }

    /**
     * Strip Markdown marks a model copied from text-layer input into the blocks: leading
     * "#", wrapping "*" or "**", stray "|" -- they are formatting of the input, never
     * page text. Returns how many strings were changed.
     */
    static int cleanMarkdown(JSONObject page) throws Exception {
        JSONArray blocks = page.optJSONArray("blocks");
        if (blocks == null) return 0;
        int changed = 0;
        for (int i = 0; i < blocks.length(); i++) {
            JSONObject b = blocks.getJSONObject(i);
            if ("table".equals(b.optString("kind"))) {
                JSONArray header = b.optJSONArray("header");
                if (header != null) changed += cleanCells(header);
                JSONArray rows = b.optJSONArray("rows");
                if (rows != null) {
                    for (int r = 0; r < rows.length(); r++) changed += cleanCells(rows.getJSONArray(r));
                }
            } else {
                String t = b.optString("text"), c = cleanText(t);
                if (!c.equals(t)) {
                    b.put("text", c);
                    changed++;
                }
            }
        }
        return changed;
    }

    private static int cleanCells(JSONArray cells) throws Exception {
        int changed = 0;
        for (int i = 0; i < cells.length(); i++) {
            String t = cells.optString(i), c = cleanText(t).replaceAll("^\\|\\s*|\\s*\\|$", "").trim();
            if (!c.equals(t)) {
                cells.put(i, c);
                changed++;
            }
        }
        return changed;
    }

    private static String cleanText(String t) {
        String[] lines = t.split("\n", -1);
        for (int i = 0; i < lines.length; i++) {
            String l = lines[i].replaceFirst("^\\s*#{1,6}\\s+", "");
            if (l.matches("^\\s*\\*{1,2}[^*].*[^*]\\*{1,2}\\s*$")) l = l.trim().replaceAll("^\\*{1,2}|\\*{1,2}$", "");
            lines[i] = l;
        }
        return String.join("\n", lines);
    }

    /** Remove exact repeats of a block (same kind and same text/cells). Returns how many. */
    static int dedupe(JSONObject page) throws Exception {
        JSONArray blocks = page.optJSONArray("blocks");
        if (blocks == null) return 0;
        Set<String> seen = new HashSet<>();
        JSONArray kept = new JSONArray();
        int dropped = 0;
        for (int i = 0; i < blocks.length(); i++) {
            JSONObject b = blocks.getJSONObject(i);
            String key = b.optString("kind") + "|" + ("table".equals(b.optString("kind"))
                    ? b.optJSONArray("header") + "|" + b.optJSONArray("rows")
                    : b.optString("text").trim().replaceAll("\\s+", " "));
            // Very short repeats ("Total", a page number) can legitimately recur.
            boolean meaningful = key.length() > 12;
            if (meaningful && !seen.add(key)) {
                dropped++;
                continue;
            }
            kept.put(b);
        }
        page.put("blocks", kept);
        return dropped;
    }
}
