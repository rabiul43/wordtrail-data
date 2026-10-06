#!/usr/bin/env python3
"""Builds curation.json from catalog.json: "New this week" + Editor's Collections.

- New this week: LibriVox ids only grow, and the catalog has no date, so we keep
  curation_state.json (id -> first-seen date). Books that appear in the catalog
  within the last 7 days are "new". First run seeds the newest 30 as new.
- Collections: rules in COLLECTIONS (genre keywords) + optional hand picks in
  editors.json ({"key": [ids...]}). Order rotates weekly so rows stay fresh.
"""
import json, hashlib, datetime, os, sys

CATALOG, STATE, OUT, PICKS = "catalog.json", "curation_state.json", "curation.json", "editors.json"
NEW_DAYS, NEW_MAX, NEW_MIN, COL_MAX, COL_MIN = 7, 30, 6, 20, 5

# key, title, blurb, any-of genre words, none-of genre words
COLLECTIONS = [
    ("mystery",  "Mystery & Detective", "Whodunits and sleuths for a rainy evening.", ["mystery", "detective", "crime"], []),
    ("classics", "Classic Novels",      "Timeless stories every reader should meet.", ["literary", "romance", "general fiction"], ["children", "poetry"]),
    ("scifi",    "Science Fiction",     "Strange worlds and bold ideas.",             ["science fiction", "fantasy"], ["children"]),
    ("kids",     "For Young Listeners", "Gentle tales for children and families.",    ["children", "fairy tales"], []),
    ("adventure","Adventure & Sea Tales","High stakes, far places.",                  ["adventure", "nautical", "sea"], []),
    ("ghost",    "Gothic & Ghost Stories","Shivers best heard after dark.",           ["gothic", "ghost", "horror", "supernatural"], []),
]

def today(): return datetime.date.today()
def load(p, default):
    try:
        with open(p, encoding="utf-8") as f: return json.load(f)
    except (OSError, ValueError): return default

def week_rank(book_id, key):
    wk = "%d-%02d" % today().isocalendar()[:2]
    return hashlib.md5(f"{wk}:{key}:{book_id}".encode()).hexdigest()

def main():
    cat = load(CATALOG, None)
    if not cat or not cat.get("b"): sys.exit("catalog.json missing or empty")
    # row: [id,title,authors,authorLast,lang,gid,total,n,ia,genres,textUrl]
    rows = [r for r in cat["b"] if not r[4] or str(r[4]).lower() == "english"]
    by_id = {str(r[0]): r for r in rows}
    ids_desc = sorted(by_id, key=int, reverse=True)

    state = load(STATE, {"first_seen": {}})
    seen = state["first_seen"]
    t = today().isoformat()
    if not seen:  # first run: everything is "old" except the newest NEW_MAX
        for i in by_id: seen[i] = "seed"
        for i in ids_desc[:NEW_MAX]: seen[i] = t
    else:
        for i in by_id: seen.setdefault(i, t)
    cutoff = (today() - datetime.timedelta(days=NEW_DAYS)).isoformat()
    fresh = [i for i in ids_desc if seen.get(i, "seed") not in ("seed",) and seen[i] >= cutoff]
    fresh = fresh[:NEW_MAX]
    if len(fresh) < NEW_MIN:  # quiet week: pad with the newest ids so the row never looks empty
        fresh += [i for i in ids_desc if i not in fresh][:NEW_MIN - len(fresh)]

    picks = load(PICKS, {})
    cols = []
    for key, title, blurb, any_of, none_of in COLLECTIONS:
        hand = [str(i) for i in picks.get(key, []) if str(i) in by_id]
        pool = []
        for i, r in by_id.items():
            g = str(r[9] or "").lower()
            if i in hand or not r[8]: continue            # need a cover (archive.org id)
            if any(w in g for w in any_of) and not any(w in g for w in none_of): pool.append(i)
        pool.sort(key=lambda i: week_rank(i, key))
        ids = (hand + pool)[:COL_MAX]
        if len(ids) >= COL_MIN: cols.append({"key": key, "title": title, "blurb": blurb, "ids": ids})

    out = {"v": 1, "updated": t, "new": fresh, "collections": cols}
    with open(OUT, "w", encoding="utf-8") as f: json.dump(out, f, separators=(",", ":"))
    with open(STATE, "w", encoding="utf-8") as f: json.dump(state, f, separators=(",", ":"))
    print(f"new={len(fresh)} collections={[(c['key'], len(c['ids'])) for c in cols]}")

if __name__ == "__main__": main()
