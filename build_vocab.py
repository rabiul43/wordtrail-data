#!/usr/bin/env python3
"""
build_vocab.py - pick the words that matter most for each English book in catalog.json and write
one small file per book, vocab/<book id>.json, for the Wordtrail app.

    python build_vocab.py                  # do books that have no vocab file yet
    python build_vocab.py --limit 1500     # at most 1500 new books (good for CI runs)
    python build_vocab.py --rescore        # redo every book
    python build_vocab.py --text book.txt  # show the word list for one local file

Needs build_levels.py next to it (text helpers) and `pip install wordfreq` (word frequencies).

What goes in a book's file
  [word, CEFR level, times used in the book, example sentence from the book], hardest and most
  useful first. The word's level comes from how common it is in everyday English:
      Zipf frequency   5.0+  A2     4.5-5.0  B1     4.0-4.5  B2     3.4-4.0  C1     below 3.4  C2
  Only B1 and above are listed. Names are left out (a name is never written in lower case), as are
  words the frequency list does not know and words used fewer than MIN_COUNT times.
  A word's rank is  log(1 + times used) x (5 - Zipf):  a rare word that keeps coming back matters
  more than one that appears once.

Meanings are not stored here. The app looks a word up in a free online dictionary when the learner
opens it, so these files stay small. A book with too little text gets an empty list, so it is not
tried again on every run.

The level borders, MIN_COUNT and WORDS_PER_BOOK are starting values; adjust them after looking at a
few books you know.
"""
import argparse
import collections
import concurrent.futures as cf
import json
import math
import os
import re
import sys
import time

from build_levels import to_plain, strip_gutenberg, fetch

try:
    from wordfreq import zipf_frequency
except Exception:  # not installed
    zipf_frequency = None

VERSION = 1
# (label, lowest Zipf frequency in the band); a word takes the first band whose limit it reaches.
BANDS = [("A2", 5.0), ("B1", 4.5), ("B2", 4.0), ("C1", 3.4), ("C2", 0.01)]
SHOW_FROM = "B1"           # easier words are not listed
MIN_COUNT = 3              # a word must appear at least this often in the book
WORDS_PER_BOOK = 30
MIN_TOKENS = 3000          # shorter texts get an empty list
EXAMPLE_MIN, EXAMPLE_MAX = 40, 170

_LOWER_WORD = re.compile(r"\b[a-z]{4,}\b")
_SPLIT = re.compile(r"(?<=[.!?])[\"\u201d\u2019')]*\s+")
_TOKEN = re.compile(r"[a-z]+")


def band_of(zipf):
    for label, lo in BANDS:
        if zipf >= lo:
            return label
    return None


def stem_key(w):
    """Crude grouping key so walk / walks / walking count as one word. Used only for grouping."""
    if w.endswith("ies") and len(w) > 5:
        w = w[:-3] + "y"
    elif w.endswith("es") and len(w) > 5 and (w[-3] in "sxz" or w.endswith(("ches", "shes"))):
        w = w[:-2]
    elif w.endswith("s") and not w.endswith(("ss", "us", "is")) and len(w) > 4:
        w = w[:-1]
    if w.endswith("ing") and len(w) > 6:
        w = w[:-3]
        if len(w) > 2 and w[-1] == w[-2] and w[-1] not in "ls":
            w = w[:-1]
    elif w.endswith("ed") and len(w) > 5:
        w = w[:-2]
        if len(w) > 2 and w[-1] == w[-2] and w[-1] not in "ls":
            w = w[:-1]
    return w.rstrip("e")


def pick_words(text):
    """-> (token count, [[word, level, count, example], ...]) or (count, []) for a short text."""
    text = text.replace("_", "")
    counts = collections.Counter(_LOWER_WORD.findall(text))
    total = sum(counts.values())
    if total < MIN_TOKENS:
        return total, []
    show = [b[0] for b in BANDS]
    show = show[show.index(SHOW_FROM):]
    groups = collections.defaultdict(list)  # stem key -> [(surface word, count)]
    for w, c in counts.items():
        if c < MIN_COUNT:
            continue
        z = zipf_frequency(w, "en")
        if z <= 0 or band_of(z) not in show:
            continue  # unknown to the frequency list (archaic, typo) or too easy
        groups[stem_key(w)].append((w, c, z))
    ranked = []
    for items in groups.values():
        word = max(items, key=lambda x: x[1])[0]
        c = sum(x[1] for x in items)
        z = min(x[2] for x in items)
        ranked.append((math.log(1 + c) * (5.0 - z), word, c, z, {x[0] for x in items}))
    ranked.sort(key=lambda r: (-r[0], r[1]))
    chosen = ranked[:WORDS_PER_BOOK]

    # one example sentence per chosen word, from the book itself
    want = {}
    for _, word, _, _, forms in chosen:
        for f in forms:
            want[f] = word
    examples = {}
    for para in re.split(r"\n\s*\n", text):
        para = " ".join(para.split())
        if len(para) < EXAMPLE_MIN:
            continue
        for sent in _SPLIT.split(para):
            if not (EXAMPLE_MIN <= len(sent) <= EXAMPLE_MAX):
                continue
            for tok in set(_TOKEN.findall(sent.lower())):
                w = want.get(tok)
                if w and w not in examples:
                    examples[w] = sent.strip()
        if len(examples) >= len(chosen):
            break
    out = [[word, band_of(z), c, examples.get(word, "")] for _, word, c, z, _ in chosen]
    return total, out


def process(book, pause):
    time.sleep(pause)
    t = strip_gutenberg(to_plain(fetch(book["url"])))
    return pick_words(t)


def read_catalog(path):
    with open(path, encoding="utf-8") as fh:
        d = json.load(fh)
    books = []
    for r in d.get("b", []):
        g = lambda i: (r[i] if len(r) > i and r[i] else "")
        books.append({"id": str(r[0]), "title": str(g(1)), "lang": str(g(4)), "url": str(g(10))})
    return books


def write_vocab(folder, bid, total, words):
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, bid + ".json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"v": VERSION, "n": total, "w": words}, fh, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser(description="Per-book vocabulary lists for Wordtrail.")
    ap.add_argument("--catalog", default="catalog.json")
    ap.add_argument("--folder", default="vocab")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--pause", type=float, default=0.4)
    ap.add_argument("--rescore", action="store_true")
    ap.add_argument("--text", help="show the word list for one local .txt file")
    a = ap.parse_args()

    if zipf_frequency is None:
        sys.exit("build_vocab.py needs wordfreq:  pip install wordfreq")

    if a.text:
        with open(a.text, "rb") as fh:
            total, words = pick_words(strip_gutenberg(to_plain(fh.read())))
        print(f"{total} words in the text, {len(words)} listed")
        for w, lv, c, ex in words:
            print(f"  {w:<14} {lv}  x{c:<4} {ex[:90]}")
        return

    todo = [b for b in read_catalog(a.catalog)
            if b["url"] and (not b["lang"] or b["lang"].lower() == "english")
            and (a.rescore or not os.path.exists(os.path.join(a.folder, b["id"] + ".json")))]
    if a.limit:
        todo = todo[:a.limit]
    print(f"{len(todo)} books to do")
    done = failed = 0
    with cf.ThreadPoolExecutor(max_workers=max(1, a.workers)) as ex:
        futs = {ex.submit(process, b, a.pause): b for b in todo}
        for fut in cf.as_completed(futs):
            b = futs[fut]
            try:
                total, words = fut.result()
            except Exception as e:
                failed += 1
                print(f"  skip {b['id']} {b['title'][:40]!r}: {e}", file=sys.stderr)
                continue
            write_vocab(a.folder, b["id"], total, words)
            done += 1
            if done % 200 == 0:
                print(f"  {done}/{len(todo)}")
    print(f"done {done}, failed {failed}")


if __name__ == "__main__":
    main()
