#!/usr/bin/env python3
"""
build_levels.py - estimate a reading level (Easy / Medium / Hard, with CEFR and age) for every
English book in catalog.json, and write levels.json for the Wordtrail app.

    python build_levels.py                      # score new books, update levels.json
    python build_levels.py --limit 500          # score at most 500 new books (good for CI runs)
    python build_levels.py --rescore            # ignore saved scores and start over
    python build_levels.py --report             # print the level mix of levels.json and stop
    python build_levels.py --text some_book.txt # score one local file and show the raw numbers

Needs only the Python standard library. For better accuracy also `pip install wordfreq`; with it
the script measures how many words are rare in everyday English. Without it, a weaker stand-in is
used (the share of long words) and levels.json records which method produced the scores.

How a score is made
  1. Download the book text (catalog column 10, the Gutenberg text URL), cut the Gutenberg
     header and footer, and keep real prose paragraphs.
  2. Sample about 6,000 words from four places in the book, so a long preface does not decide.
  3. Measure three things:
       - rare words    share of words that are uncommon in modern English  (weight 0.45)
       - clause length average words per clause; ';' and ':' end a clause, so long
                       19th-century sentences are not over-penalised         (weight 0.30)
       - syllables     average syllables per word                            (weight 0.25)
     Names (capitalised words inside a sentence) are ignored.
  4. Turn each into 0..1 using the RANGES below, blend with the weights, and map the result to a
     CEFR band with CUTS.

The numbers in RANGES and CUTS are starting values, not calibrated ones. Run once, look at
`--report`, score a few books you know well with `--text`, and adjust until the bands feel right.
"""
import argparse
import concurrent.futures as cf
import html as htmllib
import json
import os
import re
import sys
import time
import urllib.request

SCORING_VERSION = 1  # bump when the formula changes, so old scores are recomputed

# (CEFR label, age label). The index into this list is what levels.json stores.
CEFR = [("A2", "ages 8\u201310"), ("B1", "ages 11\u201313"), ("B2", "ages 14\u201316"),
        ("C1", "ages 17+"), ("C2", "adult")]
# Upper score limit of each CEFR band (the last band takes everything above).
CUTS = [0.18, 0.36, 0.54, 0.72]
# CEFR band index -> tier index (0 Easy, 1 Medium, 2 Hard).
TIERS = ["Easy", "Medium", "Hard"]
TIER_OF_BAND = [0, 0, 1, 2, 2]

WEIGHTS = {"rare": 0.45, "clause": 0.30, "syl": 0.25}
# (low, high): a value at or below low scores 0, at or above high scores 1.
RANGES = {
    "clause": (6.0, 24.0),
    "syl": (1.25, 1.65),
    "rare_wordfreq": (0.02, 0.18),   # share of words with Zipf frequency below RARE_ZIPF
    "rare_fallback": (0.05, 0.22),   # share of words with 3+ syllables or 9+ letters
}
RARE_ZIPF = 3.5

SAMPLE_WORDS = 6000       # words measured per book
SAMPLE_SPOTS = (0.15, 0.40, 0.65, 0.85)
MIN_WORDS = 1500          # fewer measurable words than this: no score
MAX_BYTES = 12 * 1024 * 1024

try:
    from wordfreq import zipf_frequency
    METHOD = "wordfreq"
except Exception:  # not installed
    zipf_frequency = None
    METHOD = "fallback"


# ---------------------------------------------------------------- text preparation

def to_plain(raw):
    """bytes -> str. Handles UTF-8 / Latin-1 and Gutenberg HTML pages."""
    try:
        t = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        t = raw.decode("latin-1")
    if "<html" in t[:3000].lower() or "<body" in t[:6000].lower():
        t = re.sub(r"(?is)<(script|style).*?</\1>", " ", t)
        t = re.sub(r"(?i)</p>|<br\s*/?>|</div>|</h\d>|</li>", "\n\n", t)
        t = re.sub(r"<[^>]+>", " ", t)
        t = htmllib.unescape(t)
    return t.replace("\r\n", "\n").replace("\r", "\n")


def strip_gutenberg(t):
    m = re.search(r"\*\*\*\s*START OF (?:THE|THIS) PROJECT GUTENBERG[^\n]*\n", t, re.I)
    if m:
        t = t[m.end():]
    m = re.search(r"\*\*\*\s*END OF (?:THE|THIS) PROJECT GUTENBERG", t, re.I)
    if m:
        t = t[:m.start()]
    return t


def prose_paragraphs(t):
    out = []
    for p in re.split(r"\n\s*\n", t):
        p = " ".join(p.split())
        p = re.sub(r"\[(?:Illustration|Footnote)[^\]]*\]", " ", p, flags=re.I).strip()
        # real prose: long enough, ends like a sentence, not shouting (headings, tables of contents)
        if len(p) >= 60 and re.search(r"[.!?][\"\u201d\u2019')\]]*$", p) and not p.isupper():
            out.append(p)
    return out


def sample_text(paras):
    """About SAMPLE_WORDS words, taken from several spots in the book."""
    counts = [len(p.split()) for p in paras]
    total = sum(counts)
    if total <= SAMPLE_WORDS:
        return " ".join(paras)
    per = SAMPLE_WORDS // len(SAMPLE_SPOTS)
    # index of the paragraph that holds each spot
    starts, run = [], 0
    for c in counts:
        starts.append(run)
        run += c
    chunks = []
    for s in SAMPLE_SPOTS:
        target = int(total * s)
        i = max(0, next((k for k, st in enumerate(starts) if st > target), len(paras)) - 1)
        got = 0
        while i < len(paras) and got < per:
            chunks.append(paras[i])
            got += counts[i]
            i += 1
    return " ".join(chunks)


# ---------------------------------------------------------------- measuring

_ABBR = re.compile(r"\b(Mr|Mrs|Ms|Dr|St|Prof|Sr|Jr|Capt|Col|Gen|Messrs)\.")
_CLAUSE_SPLIT = re.compile(r"[.!?;:]+[\"\u201d\u2019')\]]*\s+|\s[\u2014-]{1,2}\s")
_WORD = re.compile(r"[A-Za-z]+(?:['\u2019][A-Za-z]+)*")


def syllables(w):
    w = re.sub(r"[^a-z]", "", w.lower())
    if not w:
        return 0
    if len(w) <= 3:
        return 1
    w = re.sub(r"(?:[^laeiouy]es|ed|[^laeiouy]e)$", "", w)
    w = re.sub(r"^y", "", w)
    return max(1, len(re.findall(r"[aeiouy]{1,2}", w)))


def analyze(text):
    """Return the raw measurements for a piece of prose, or None if there is too little of it."""
    text = _ABBR.sub(r"\1", text)
    clauses = [c for c in _CLAUSE_SPLIT.split(text) if c.strip()]
    words = syl = rare = 0
    clause_n = 0
    for c in clauses:
        toks = _WORD.findall(c)
        if not toks:
            continue
        clause_n += 1
        for j, tok in enumerate(toks):
            # a capitalised word inside a clause is almost always a name
            if j > 0 and tok[0].isupper() and tok != "I" and not tok.isupper():
                continue
            w = tok.lower().replace("\u2019", "'")
            words += 1
            s = syllables(w)
            syl += s
            if zipf_frequency is not None:
                if zipf_frequency(w.split("'")[0], "en") < RARE_ZIPF:
                    rare += 1
            elif s >= 3 or len(w) >= 9:
                rare += 1
    if words < MIN_WORDS or not clause_n:
        return None
    return {"words": words, "clause": words / clause_n, "syl": syl / words, "rare": rare / words}


def _unit(v, lo, hi):
    return max(0.0, min(1.0, (v - lo) / (hi - lo)))


def score(f):
    rare_range = RANGES["rare_wordfreq" if METHOD == "wordfreq" else "rare_fallback"]
    return (WEIGHTS["rare"] * _unit(f["rare"], *rare_range)
            + WEIGHTS["clause"] * _unit(f["clause"], *RANGES["clause"])
            + WEIGHTS["syl"] * _unit(f["syl"], *RANGES["syl"]))


def band_of(s):
    for i, cut in enumerate(CUTS):
        if s < cut:
            return i
    return len(CUTS)


# ---------------------------------------------------------------- catalog and network

def read_catalog(path):
    with open(path, encoding="utf-8") as fh:
        d = json.load(fh)
    books = []
    for r in d.get("b", []):
        lang = (r[4] if len(r) > 4 else "") or ""
        url = (r[10] if len(r) > 10 else "") or ""
        books.append({"id": str(r[0]), "title": r[1], "lang": lang, "url": url})
    return books


def fetch(url, tries=3):
    err = None
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "wordtrail-levels/1.0"})
            with urllib.request.urlopen(req, timeout=40) as r:
                return r.read(MAX_BYTES)
        except Exception as e:  # network hiccup: wait and retry
            err = e
            time.sleep(1.5 * (k + 1))
    raise err


def score_book(b, pause):
    time.sleep(pause)
    t = strip_gutenberg(to_plain(fetch(b["url"])))
    paras = prose_paragraphs(t)
    f = analyze(sample_text(paras)) if paras else None
    return f


def entry(f):
    s = score(f)
    band = band_of(s)
    return [TIER_OF_BAND[band], band, int(round(s * 100))]


# ---------------------------------------------------------------- output

def load_levels(path):
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        if d.get("v") == 1 and d.get("sv") == SCORING_VERSION and d.get("m") == METHOD:
            return d.get("b", {})
    except (OSError, ValueError):
        pass
    return {}


def save_levels(path, scores):
    d = {"v": 1, "sv": SCORING_VERSION, "m": METHOD, "cefr": [list(x) for x in CEFR],
         "tiers": TIERS, "b": scores}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(d, fh, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def report(scores):
    n = len(scores)
    print(f"{n} books scored (method: {METHOD})")
    if not n:
        return
    for i, (lab, age) in enumerate(CEFR):
        c = sum(1 for v in scores.values() if v[1] == i)
        print(f"  {lab:<3} {age:<14} {c:>6}  {100 * c / n:5.1f}%  {'#' * int(60 * c / n)}")
    for i, name in enumerate(TIERS):
        c = sum(1 for v in scores.values() if v[0] == i)
        print(f"  {name:<7} {c:>6}  {100 * c / n:5.1f}%")


def main():
    ap = argparse.ArgumentParser(description="Score book reading levels for Wordtrail.")
    ap.add_argument("--catalog", default="catalog.json")
    ap.add_argument("--out", default="levels.json")
    ap.add_argument("--limit", type=int, default=0, help="score at most N new books")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--pause", type=float, default=0.4, help="seconds each worker waits before a download")
    ap.add_argument("--rescore", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--text", help="score one local .txt file and print the details")
    a = ap.parse_args()

    if a.text:
        with open(a.text, "rb") as fh:
            t = strip_gutenberg(to_plain(fh.read()))
        f = analyze(sample_text(prose_paragraphs(t)))
        if not f:
            sys.exit("Not enough prose to score.")
        s = score(f)
        band = band_of(s)
        print(f"method {METHOD} | words {f['words']} | clause {f['clause']:.1f} | "
              f"syllables/word {f['syl']:.2f} | rare {f['rare']:.3f}")
        print(f"score {s:.2f} -> {CEFR[band][0]} ({CEFR[band][1]}), {TIERS[TIER_OF_BAND[band]]}")
        return

    scores = {} if a.rescore else load_levels(a.out)
    if a.report:
        report(scores)
        return

    if zipf_frequency is None:
        print("note: wordfreq is not installed; using the long-word fallback (less accurate).\n"
              "      pip install wordfreq   for better scores.", file=sys.stderr)

    todo = [b for b in read_catalog(a.catalog)
            if b["url"] and b["id"] not in scores and (not b["lang"] or b["lang"].lower() == "english")]
    if a.limit:
        todo = todo[:a.limit]
    print(f"{len(scores)} already scored, {len(todo)} to do")

    done = skipped = 0
    with cf.ThreadPoolExecutor(max_workers=max(1, a.workers)) as ex:
        futs = {ex.submit(score_book, b, a.pause): b for b in todo}
        for fut in cf.as_completed(futs):
            b = futs[fut]
            try:
                f = fut.result()
            except Exception as e:
                skipped += 1
                print(f"  skip {b['id']} {b['title'][:40]!r}: {e}", file=sys.stderr)
                continue
            if not f:
                skipped += 1  # too little prose (poetry, plays, lists): no level shown
                continue
            scores[b["id"]] = entry(f)
            done += 1
            if done % 200 == 0:
                save_levels(a.out, scores)
                print(f"  {done}/{len(todo)}")
    save_levels(a.out, scores)
    print(f"scored {done}, skipped {skipped}, total {len(scores)}")
    report(scores)


if __name__ == "__main__":
    main()
