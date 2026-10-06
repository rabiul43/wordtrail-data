#!/usr/bin/env python3
"""Wordtrail smart index: builds search.json (typo-tolerant search) and versions.json (best audio version)."""
import json, math, os, re, sys, time, unicodedata, urllib.parse, urllib.request

CATALOG = "catalog.json"

ALIASES = [
    ("mark twain", "samuel clemens"), ("lewis carroll", "charles dodgson"),
    ("george eliot", "mary ann evans"), ("o henry", "william sydney porter"),
    ("saki", "hector hugh munro"), ("george orwell", "eric blair"),
    ("voltaire", "francois marie arouet"), ("moliere", "jean baptiste poquelin"),
    ("stendhal", "henri beyle"), ("lord byron", "george gordon byron"),
    ("dostoyevsky", "dostoevsky", "dostoevski"), ("tolstoy", "tolstoi"),
    ("chekhov", "tchekhov", "chekov"), ("shakespeare", "shakspere"),
]

def norm(s):
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = s.replace("&", " and ")
    s = re.sub(r"['\u2019`]", "", s)
    return re.sub(r"[\W_]+", " ", s).strip()

def load(p, default):
    try:
        with open(p, encoding="utf-8") as f: return json.load(f)
    except (OSError, ValueError): return default

def popularity(w_trend, w_col, w_edit):
    pop = {}
    def add(i, n): pop[str(i)] = pop.get(str(i), 0) + n
    home = load("home.json", {})
    for i in home.get("trending", []): add(i, w_trend)
    if home.get("botd"): add(home["botd"].get("id"), w_trend)
    for c in load("curation.json", {}).get("collections", []):
        for i in c.get("ids", []): add(i, w_col)
    for ids in load("editors.json", {}).values():
        for i in ids: add(i, w_edit)
    return pop

# ---------------- search.json ----------------
def with_aliases(a):
    padded = " " + a + " "
    extra = []
    for group in ALIASES:
        if any(" " + g + " " in padded for g in group):
            extra += [g for g in group if " " + g + " " not in padded]
    return (a + " " + " ".join(extra)).strip()

def build_search(cat):
    pop = popularity(2, 1, 2)
    ids, titles, authors, ai, pp = [], [], [], [], []
    amap = {}
    for r in cat["b"]:
        a = with_aliases(norm(r[2]))
        if a not in amap: amap[a] = len(authors); authors.append(a)
        ids.append(int(r[0])); titles.append(norm(r[1])); ai.append(amap[a])
        pp.append(str(min(9, pop.get(str(r[0]), 0) + (1 if r[8] else 0))))
    out = {"v": 1, "ids": ids, "t": titles, "A": authors, "ai": ai, "p": "".join(pp)}
    with open("search.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print(f"search.json: books={len(ids)} authors={len(authors)}")

# ---------------- versions.json ----------------
NOISE = re.compile(r"\b(version|ver|v)\s*\d+\b|\b(dramatic reading|dramatization|unabridged|abridged|audiobook|multi ?voice|solo)\b")

def clean_title(t):
    t = re.sub(r"\(.*?\)|\[.*?\]", " ", str(t or ""))
    t = NOISE.sub(" ", norm(t))
    t = re.sub(r"^(the|a|an)\s+", "", t.strip())
    return re.sub(r"\s+", " ", t).strip()

class UF:
    def __init__(self): self.p = {}
    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]; x = self.p[x]
        return x
    def union(self, a, b): self.p[self.find(a)] = self.find(b)

def fetch_downloads(ia_ids):
    """Real listening popularity: archive.org download counts per item. Fails safe (returns what it got)."""
    got, ids = {}, sorted(set(i for i in ia_ids if i))[:3000]
    for k in range(0, len(ids), 40):
        batch = ids[k:k + 40]
        q = "identifier:(" + " OR ".join('"%s"' % b for b in batch) + ")"
        url = "https://archive.org/advancedsearch.php?" + urllib.parse.urlencode(
            [("q", q), ("fl[]", "identifier"), ("fl[]", "downloads"), ("rows", len(batch)), ("output", "json")])
        try:
            with urllib.request.urlopen(url, timeout=25) as r:
                for d in json.load(r).get("response", {}).get("docs", []):
                    got[d["identifier"]] = int(d.get("downloads") or 0)
        except Exception as e:
            print("downloads batch skipped:", e)
    return got

# ---------------- text-match scoring ----------------
GUT_CACHE = "gutenberg_stats.json"   # {gutenberg id: [word count, chapter count]}, fetched once, then cached
HEAD = re.compile(r"^\s*(chapter|letter|book|part|canto|act|stave|volume)\s+([ivxlcdm]+|\d+)\b", re.I)
WPM = 155  # typical LibriVox reading speed

def gutenberg_stats(gid, cache, budget):
    k = str(gid)
    if k in cache: return cache[k]
    if budget[0] <= 0: return None
    budget[0] -= 1
    url = f"https://www.gutenberg.org/cache/epub/{k}/pg{k}.txt"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "wordtrail-index"})
        with urllib.request.urlopen(req, timeout=40) as r: txt = r.read().decode("utf-8", "ignore")
    except Exception as e:
        print("gutenberg skipped", k, e); return None
    a = re.search(r"\*\*\* ?START OF[^\n]*\n", txt); b = txt.rfind("*** END OF")
    if a: txt = txt[a.end(): b if b > a.end() else len(txt)]
    heads = {ln.strip().lower() for ln in txt.splitlines() if len(ln) < 60 and HEAD.match(ln)}
    cache[k] = [len(txt.split()), len(heads)]
    time.sleep(1)
    return cache[k]

def sync_state(i):
    try:
        with open(os.path.join("data", i, "status.json"), encoding="utf-8") as f: d = json.load(f)
    except (OSError, ValueError): return None
    stg = str(d.get("stage", "")).lower()
    if stg == "error": return "error"
    try: pct = float(d.get("pct") or 0)
    except (TypeError, ValueError): pct = 0
    if stg in ("done", "complete", "completed", "ready", "finished") or pct >= 1: return "done"
    return None

def text_match(r, title, major, st, sync, al=None):
    """Returns (score 0-100, positive reasons): how likely this recording is to sync accurately with the Gutenberg text."""
    m, why = 50, []
    secs, n = float(r[6] or 0), int(r[7] or 0)
    if st and st[0] and secs:
        ratio = secs / (st[0] / WPM * 60)
        if 0.85 <= ratio <= 1.15: m += 25; why.append("Matches text")
        elif 0.7 <= ratio <= 1.3: m += 12
        elif ratio < 0.6 or ratio > 1.5: m -= 25
    if st and st[1] >= 3 and n:
        c = min(n, st[1]) / max(n, st[1])
        if c >= 0.8: m += 15; why.append("Chapters match")
        elif c >= 0.5: m += 7
        elif c < 0.3: m -= 5
    gid = str(r[5] or "")
    if major and gid == major: m += 10
    elif gid: m -= 15
    elif not r[10]: m -= 10
    if re.search(r"\babridged\b", title) and "unabridged" not in title: m -= 25
    if re.search(r"excerpt|selections|extract", title): m -= 25
    if re.search(r"dramatic reading|dramatization|multi.?voice", title): m -= 15
    if al and al.get("st") == "poor": m -= 25          # nightly alignment check says the audio does not follow the text
    elif al and al.get("st") == "ok": m += 5; why.append("Aligned")
    if sync == "done": m += 10; why.append("Synced OK")
    elif sync == "error": m -= 10
    return max(0, min(100, m)), why

def build_versions(cat):
    rows = {str(r[0]): r for r in cat["b"]}
    uf = UF()
    for i, r in rows.items():
        lang = str(r[4] or "").lower()
        who = norm(r[3]) or (norm(r[2]).split(" ") or [""])[-1]
        key = ("t", lang, clean_title(r[1]), who)
        if key[2]: uf.union(("b", i), key)
        if r[5]: uf.union(("b", i), ("g", lang, str(r[5])))
    groups = {}
    for i in rows: groups.setdefault(uf.find(("b", i)), []).append(i)
    groups = [g for g in groups.values() if len(g) > 1]
    pop = popularity(1.5, 0.5, 1.5)
    dl = fetch_downloads([rows[i][8] for g in groups for i in g])
    print(f"download counts found: {len(dl)}")
    align = load("align.json", {}).get("b", {})
    cache = load(GUT_CACHE, {}); budget = [300]   # max new Gutenberg downloads per run
    out = []
    for g in groups:
        top_dl = max((dl.get(rows[i][8], 0) for i in g), default=0)
        longest = max((float(rows[i][6] or 0) for i in g), default=0)
        gids = [str(rows[i][5]) for i in g if rows[i][5]]
        major = max(set(gids), key=gids.count) if gids else None
        st = gutenberg_stats(major, cache, budget) if major else None
        scored = []
        for i in g:
            r = rows[i]; title = str(r[1] or "").lower(); why = []; s = 0.0
            if os.path.exists(os.path.join("data", i, "book.json")) or os.path.exists(os.path.join("data", i, "0.json")):
                s += 4; why.append("Synced")
            if r[5] or r[10]: s += 2; why.append("Has text")
            if r[8]: s += 1
            tot = float(r[6] or 0)
            if longest and tot:
                if tot >= longest * 0.98: s += 1; why.append("Complete")
                elif tot < longest * 0.7: s -= 2
            if re.search(r"\b(abridged)\b", title) and "unabridged" not in title: s -= 2
            if re.search(r"excerpt|selections|extract", title): s -= 2
            if re.search(r"dramatic reading|dramatization|multi.?voice", title): s -= 1
            n_dl = dl.get(r[8], 0)
            if n_dl: s += min(3.0, math.log10(n_dl + 1) * 0.5)
            if n_dl and n_dl == top_dl and n_dl >= 500: why.append("Most listened")
            p = pop.get(i, 0)
            if p: s += p; why.append("Popular")
            s += max(0.0, 0.3 - int(i) / 1e6)
            m, mwhy = text_match(r, title, major, st, sync_state(i), align.get(i))
            scored.append((m, s, i, mwhy + why))
        scored.sort(key=lambda x: (-(x[0] // 10), -x[1], int(x[2])))
        out.append({"i": [int(x[2]) for x in scored], "s": [x[0] for x in scored], "w": " \u00b7 ".join(scored[0][3][:3])})
    with open(GUT_CACHE, "w", encoding="utf-8") as f: json.dump(cache, f, separators=(",", ":"))
    out.sort(key=lambda x: x["i"][0])
    with open("versions.json", "w", encoding="utf-8") as f:
        json.dump({"v": 1, "g": out}, f, ensure_ascii=False, separators=(",", ":"))
    print(f"versions.json: groups={len(out)}")

def main():
    cat = load(CATALOG, None)
    if not cat or not cat.get("b"): sys.exit("catalog.json missing or empty")
    build_search(cat)
    build_versions(cat)

if __name__ == "__main__": main()

        
