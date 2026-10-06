#!/usr/bin/env python3
"""
build_moods.py - read the full text of every English book in catalog.json and write two files
for the Wordtrail app:

    moods.json     precise mood tags per book (up to 3 of: cozy, spooky, adventurous, romantic,
                   mysterious, funny, thoughtful), replacing the app's keyword guesses
    similar.json   a pre-calculated list of the most similar books for every book

    python build_moods.py                  # read new books, then rebuild both files
    python build_moods.py --limit 1500     # read at most 1500 new books (good for CI runs)
    python build_moods.py --rescore        # throw away saved readings and start over
    python build_moods.py --report         # rebuild both files from the saved readings only
    python build_moods.py --text book.txt  # show the raw numbers for one local file

Standard library only. build_levels.py must sit next to this file (its text helpers are reused).

How it works
  Moods:    each mood has a word list (LEXICON below). For every book the script counts how often
            those words appear per 1,000 words of the WHOLE text, then compares the book with all
            other books (a z-score), so "spooky" means spookier than the typical book, not just
            "contains the word night". A matching genre in the catalog adds a small boost.
  Similar:  for every book the 40 most characteristic everyday words are saved. Books are matched
            by shared distinctive words (TF-IDF), then ranked by a blend of word overlap, mood
            profile, genre overlap, same author and similar reading level (if levels.json exists).
            Other recordings of the same book are left out of each list.

The word lists, weights and thresholds are starting values. Run `--report`, look at a few books you
know well, and adjust the settings near the top of the file.

Saved readings live in moods_cache.json.gz, so each run only reads new books.
"""
import argparse
import collections
import concurrent.futures as cf
import gzip
import json
import math
import os
import re
import sys
import time

from build_levels import to_plain, strip_gutenberg, fetch  # shared helpers

SCORING_VERSION = 1  # bump when the word lists or measuring change; saved readings are then redone

# key, label (the keys are the ones the app's Mood chips already use)
MOODS = [("cozy", "Cozy"), ("spooky", "Spooky"), ("adventurous", "Adventurous"),
         ("romantic", "Romantic"), ("mysterious", "Mysterious"), ("funny", "Funny"),
         ("thoughtful", "Thoughtful")]

# A word ending in * matches every word that starts with it.
LEXICON = {
    "cozy": """tea kettle teapot fireside hearth cottage cosy cozy garden gardens kitchen bread cake cakes
        pie pies pudding supper breakfast apple apples orchard meadow blanket quilt snug comfort* cheerful
        merry pleasant gentle neighbour* neighbor* village farm farmhouse kitten kitten* puppy picnic
        christmas lamplight candle* warm* homely tidy cheery bakery jam cream cosily""",
    "spooky": """ghost* haunt* grave graves graveyard tomb* coffin* corpse* skeleton* vampire* witch* curse*
        shadow* phantom* spectre specter spectr* eerie sinister ghastly horror horrible terror shriek*
        scream* dread* midnight moan* crypt dungeon demon* devil* fiend* creep* creak* cobweb* gloom*
        uncanny apparition* macabre cemetery howl* sepulchr* ghoul* wraith* dismal ominous lurk* haunted
        shudder* trembl* pallid ghostly""",
    "adventurous": """voyage* ship ships sail* island* treasure* pirate* captain* expedition* jungle* desert*
        mountain* cave caves savage* explor* journey* quest compass anchor cutlass sword* battle* hunt*
        wilderness canoe* frontier rifle* gallop* escape* danger* perilous adventur* cliff* shipwreck*
        harbour harbor raft* buffalo trail* mast deck cannon* musket* pistol* fort camp* tribe* arrow*
        galloped horseback""",
    "romantic": """love loved loves loving lover* beloved darling* kiss* tender* tenderness passion* sweetheart*
        courtship bride* wedding* marriage* betroth* suitor* adore* affection* devot* embrace* sigh*
        blush* ardent fond* longing wooing wooed heart hearts engaged proposal romance romantic charming
        dearest""",
    "mysterious": """mystery mysteries mysterious murder* detective* clue* suspect* crime* criminal* alibi
        inspector* poison* secret* puzzle* evidence footprint* disguise* theft thief thieves stolen
        vanish* investigat* witness* culprit solve* solution enigma riddle inquest confess* motive
        burglar* deduc* scotland yard sleuth mysteriously""",
    "funny": """laugh* joke* jest* funny amus* ridiculous absurd* comic* giggl* grin* humour humor witty
        nonsense silly foolish fool fools merriment droll chuckl* prank* mischief* farce comedy hilarious
        ludicrous jolly clown* tickle* teas* sillier""",
    "thoughtful": """philosoph* reason* wisdom wise virtue* soul souls truth* moral* conscience mankind existence
        duty duties reflect* contemplat* meditat* thought* principle* ethic* belief* faith knowledge
        understanding mind minds essence society civilization civilisation conscious* reality
        experience* nature human* universe knowledge argument* doctrine theory theories idea ideas
        judgment judgement""",
}

# Catalog genre words that add GENRE_BOOST to a mood's score.
GENRE_HINTS = {
    "cozy": ["children", "domestic", "family", "nature", "pastoral"],
    "spooky": ["horror", "gothic", "ghost", "supernatural"],
    "adventurous": ["adventure", "western", "sea", ", war", "pirate", "exploration", "travel"],
    "romantic": ["romance", "love"],
    "mysterious": ["mystery", "detective", "crime", "thriller"],
    "funny": ["humor", "humour", "satire", "comedy", "comic"],
    "thoughtful": ["philosoph", "essay", "religion", "psycholog", "ethic", "self-help"],
}
GENRE_BOOST = 0.8      # added to a mood's z-score when the catalog genre matches
MIN_Z = 1.0            # a mood must be this far above the typical book to be tagged
FALLBACK_Z = 0.5       # if nothing reaches MIN_Z, the best mood is still tagged when above this
MAX_TAGS = 3

SIMILAR_COUNT = 8
MIN_SIMILARITY = 0.12
BLEND = {"words": 0.50, "mood": 0.20, "genre": 0.15, "author": 0.10, "level": 0.05}
TOP_WORDS = 40         # characteristic words saved per book
MIN_TOKENS = 3000      # shorter texts are too small to read a mood from
MAX_DF_SHARE = 0.03    # words in more than 3% of books say little about similarity

STOP = set("""
about above after again against all almost along already also although always among and another any anyone
anything around away back because been before behind being below between both brought but came can cannot
come comes coming could dare did does doing done down during each either else enough even ever every
everything find first from gave get gets give given goes going gone good great had has have having her here
herself him himself his how however into its itself just keep kept know known last leave left less let like
little long look looked looking made make makes many may might more most much must myself near never next
none nothing now off once one only onto other others ought our ours ourselves out over own perhaps put quite
rather really said same saw say says see seem seemed seems seen several shall she should since some
something sometimes still such take taken than that the their theirs them themselves then there these they
thing things think this those though thought through thus till time together told took toward towards under
until upon used very want wanted was way well went were what when where whether which while who whom whose
why will with within without would yet you your yours yourself yourselves
""".split())

_WORD = re.compile(r"[a-z]{2,}")
_LOWER_WORD = re.compile(r"\b[a-z]{4,}\b")


# ---------------------------------------------------------------- reading one book

def _parse_lexicon():
    out = []
    for key, _ in MOODS:
        exact, prefixes = set(), []
        for t in LEXICON[key].split():
            if t.endswith("*"):
                prefixes.append(t[:-1])
            else:
                exact.add(t)
        out.append((exact, tuple(prefixes)))
    return out


_LEX = _parse_lexicon()
_memo = {}


def moods_of(word):
    r = _memo.get(word)
    if r is None:
        r = tuple(i for i, (ex, pre) in enumerate(_LEX) if word in ex or (pre and word.startswith(pre)))
        _memo[word] = r
    return r


def read_text(text):
    """-> [token count, [mood densities per 1,000 words], [[word, count], ...]] or None if too short."""
    low = collections.Counter(_WORD.findall(text.lower()))
    n = sum(low.values())
    if n < MIN_TOKENS:
        return None
    hits = [0] * len(MOODS)
    for w, c in low.items():
        for i in moods_of(w):
            hits[i] += c
    dens = [round(h * 1000.0 / n, 3) for h in hits]
    # characteristic words: lower-case words only, so names and sentence openers drop out
    cap = collections.Counter(w for w in _LOWER_WORD.findall(text) if w not in STOP)
    top = [[w, c] for w, c in cap.most_common(TOP_WORDS) if c >= 4]
    return [n, dens, top]


def read_book(url, pause):
    time.sleep(pause)
    return read_text(strip_gutenberg(to_plain(fetch(url))))


# ---------------------------------------------------------------- catalog, cache, versions

def read_catalog(path):
    with open(path, encoding="utf-8") as fh:
        d = json.load(fh)
    books = {}
    for r in d.get("b", []):
        g = lambda i: (r[i] if len(r) > i and r[i] else "")
        books[str(r[0])] = {"title": str(g(1)), "authors": str(g(2)), "lang": str(g(4)),
                            "genres": str(g(9)).lower(), "url": str(g(10))}
    return books


def load_cache(path):
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            d = json.load(fh)
        if d.get("v") == 1 and d.get("sv") == SCORING_VERSION:
            return d.get("b", {})
    except (OSError, ValueError):
        pass
    return {}


def save_cache(path, cache):
    tmp = path + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=9) as fh:
        json.dump({"v": 1, "sv": SCORING_VERSION, "b": cache}, fh, separators=(",", ":"))
    os.replace(tmp, path)


def load_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


# ---------------------------------------------------------------- moods

def mood_scores(cache, meta):
    """book id -> list of z-scores (one per mood), compared with every other read book."""
    ids = [i for i in cache if i in meta]
    k = len(MOODS)
    stats = []
    for m in range(k):
        col = [cache[i][1][m] for i in ids]
        mean = sum(col) / len(col) if col else 0.0
        var = sum((x - mean) ** 2 for x in col) / len(col) if col else 0.0
        stats.append((mean, math.sqrt(var)))
    out = {}
    for i in ids:
        zs = []
        for m in range(k):
            mean, sd = stats[m]
            z = (cache[i][1][m] - mean) / sd if sd > 1e-9 else 0.0
            if any(h in meta[i]["genres"] for h in GENRE_HINTS[MOODS[m][0]]):
                z += GENRE_BOOST
            zs.append(z)
        out[i] = zs
    return out


def pick_tags(zs):
    order = sorted(range(len(zs)), key=lambda m: -zs[m])
    tags = [m for m in order if zs[m] >= MIN_Z][:MAX_TAGS]
    if not tags and zs[order[0]] >= FALLBACK_Z:
        tags = [order[0]]
    return tags


# ---------------------------------------------------------------- similar books

def norm_title(t):
    t = re.sub(r"\(.*?\)|\[.*?\]", " ", t.lower())
    t = re.sub(r"[^a-z0-9]+", " ", t).strip()
    return re.sub(r"^(the|a|an) ", "", t)


def norm_author(a):
    return re.sub(r"[^a-z]+", " ", a.lower()).strip()


def build_similar(cache, meta, z, levels, groups):
    ids = [i for i in cache if i in meta and cache[i][2]]
    N = len(ids)
    if N < 2:
        return {}
    df = collections.Counter(w for i in ids for w, _ in cache[i][2])
    cap = max(20, int(N * MAX_DF_SHARE))
    idf = {w: math.log(N / d) for w, d in df.items() if d <= cap}

    vec = {}
    post = collections.defaultdict(list)
    for i in ids:
        v = {w: (1 + math.log(c)) * idf[w] for w, c in cache[i][2] if w in idf}
        norm = math.sqrt(sum(x * x for x in v.values()))
        if norm:
            v = {w: x / norm for w, x in v.items()}
            vec[i] = v
            for w, x in v.items():
                post[w].append((i, x))

    title = {i: norm_title(meta[i]["title"]) for i in ids}
    author = {i: norm_author(meta[i]["authors"]) for i in ids}
    by_author = collections.defaultdict(list)
    for i in ids:
        if author[i] and author[i] != "unknown author":
            by_author[author[i]].append(i)
    genres = {i: {g.strip() for g in re.split(r"[,;|/]+", meta[i]["genres"]) if g.strip()} for i in ids}
    mood_vec = {i: [max(0.0, x) for x in z[i]] for i in ids}
    mood_len = {i: math.sqrt(sum(x * x for x in mood_vec[i])) for i in ids}
    group_of = {}
    for gi, g in enumerate(groups or []):
        for b in g:
            group_of[str(b)] = gi

    out = {}
    for i in ids:
        acc = collections.defaultdict(float)
        for w, x in vec.get(i, {}).items():
            for j, y in post[w]:
                if j != i:
                    acc[j] += x * y
        for j in by_author.get(author[i], []):
            if j != i:
                acc.setdefault(j, 0.0)
        scored = []
        for j, cos in acc.items():
            if title[j] == title[i] or (i in group_of and group_of.get(j) == group_of[i]):
                continue  # another recording of the same book
            mc = 0.0
            if mood_len[i] and mood_len[j]:
                mc = sum(a * b for a, b in zip(mood_vec[i], mood_vec[j])) / (mood_len[i] * mood_len[j])
            gj = 0.0
            if genres[i] and genres[j]:
                gj = len(genres[i] & genres[j]) / len(genres[i] | genres[j])
            same_author = 1.0 if author[i] and author[i] == author[j] and author[i] != "unknown author" else 0.0
            lv = 0.0
            if levels and i in levels and j in levels:
                lv = max(0.0, 1.0 - abs(levels[i][2] - levels[j][2]) / 40.0)
            s = (BLEND["words"] * cos + BLEND["mood"] * mc + BLEND["genre"] * gj
                 + BLEND["author"] * same_author + BLEND["level"] * lv)
            if s >= MIN_SIMILARITY:
                scored.append((s, j))
        scored.sort(key=lambda x: (-x[0], x[1]))
        top = []
        for s, j in scored:
            if len(top) >= SIMILAR_COUNT:
                break
            top.append(int(j) if j.isdigit() else j)
        if top:
            out[i] = top
    return out


# ---------------------------------------------------------------- output

def finalize(cache, meta, a):
    z = mood_scores(cache, meta)
    moods = {i: pick_tags(zs) for i, zs in z.items()}
    write_json(a.out_moods, {"v": 1, "sv": SCORING_VERSION, "moods": [list(m) for m in MOODS], "b": moods})

    lv = load_json(a.levels)
    levels = lv.get("b") if isinstance(lv, dict) and lv.get("v") == 1 else None
    vs = load_json(a.versions)
    groups = [g.get("i", []) for g in vs.get("g", [])] if isinstance(vs, dict) and vs.get("v") == 1 else []
    sim = build_similar(cache, meta, z, levels, groups)
    write_json(a.out_similar, {"v": 1, "sv": SCORING_VERSION, "s": sim})

    n = len(moods)
    print(f"{n} books tagged, {len(sim)} with similar-book lists")
    if n:
        tag_counts = collections.Counter(len(t) for t in moods.values())
        for m, (_, label) in enumerate(MOODS):
            c = sum(1 for t in moods.values() if m in t)
            print(f"  {label:<12} {c:>6}  {100 * c / n:5.1f}%")
        print("  tags per book: " + ", ".join(f"{k}: {tag_counts[k]}" for k in sorted(tag_counts)))


def main():
    ap = argparse.ArgumentParser(description="Mood tags and similar books for Wordtrail.")
    ap.add_argument("--catalog", default="catalog.json")
    ap.add_argument("--versions", default="versions.json")
    ap.add_argument("--levels", default="levels.json")
    ap.add_argument("--cache", default="moods_cache.json.gz")
    ap.add_argument("--out-moods", default="moods.json")
    ap.add_argument("--out-similar", default="similar.json")
    ap.add_argument("--limit", type=int, default=0, help="read at most N new books")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--pause", type=float, default=0.4)
    ap.add_argument("--rescore", action="store_true")
    ap.add_argument("--report", action="store_true", help="rebuild the outputs from saved readings only")
    ap.add_argument("--text", help="show the raw numbers for one local .txt file")
    a = ap.parse_args()

    if a.text:
        with open(a.text, "rb") as fh:
            r = read_text(strip_gutenberg(to_plain(fh.read())))
        if not r:
            sys.exit(f"Fewer than {MIN_TOKENS} words: too short to read a mood from.")
        print(f"words {r[0]}")
        for (key, label), d in zip(MOODS, r[1]):
            print(f"  {label:<12} {d:6.2f} per 1,000 words")
        print("top words: " + ", ".join(f"{w} {c}" for w, c in r[2][:20]))
        return

    meta = read_catalog(a.catalog)
    cache = {} if a.rescore else load_cache(a.cache)

    if not a.report:
        todo = [i for i, b in meta.items()
                if b["url"] and i not in cache and (not b["lang"] or b["lang"].lower() == "english")]
        if a.limit:
            todo = todo[:a.limit]
        print(f"{len(cache)} already read, {len(todo)} to do")
        done = skipped = 0
        with cf.ThreadPoolExecutor(max_workers=max(1, a.workers)) as ex:
            futs = {ex.submit(read_book, meta[i]["url"], a.pause): i for i in todo}
            for fut in cf.as_completed(futs):
                i = futs[fut]
                try:
                    r = fut.result()
                except Exception as e:
                    skipped += 1
                    print(f"  skip {i} {meta[i]['title'][:40]!r}: {e}", file=sys.stderr)
                    continue
                if not r:
                    skipped += 1  # too short to read
                    continue
                cache[i] = r
                done += 1
                if done % 200 == 0:
                    save_cache(a.cache, cache)
                    print(f"  {done}/{len(todo)}")
        save_cache(a.cache, cache)
        print(f"read {done}, skipped {skipped}")

    finalize(cache, meta, a)


if __name__ == "__main__":
    main()
