#!/usr/bin/env python3
"""Builds stats.json: Internet Archive plays for every LibriVox recording in catalog.json.

    python build_stats.py catalog.json stats.json

stats.json = {"v": 1, "t": <unix time>, "s": {"<book id>": [downloads, last 7 days], ...}}
Run it in the same daily GitHub job that builds home.json. Needs only the Python standard library.
"""
import gzip, json, sys, time, urllib.parse, urllib.request

SEARCH = "https://archive.org/advancedsearch.php"
ROWS = 5000


def fetch_page(page):
    q = urllib.parse.urlencode([
        ("q", "collection:librivoxaudio"), ("fl[]", "identifier"), ("fl[]", "downloads"), ("fl[]", "week"),
        ("rows", ROWS), ("page", page), ("output", "json"),
    ])
    req = urllib.request.Request(SEARCH + "?" + q, headers={"User-Agent": "wordtrail-stats/1.0"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.load(r)["response"]["docs"]
        except Exception as e:
            if attempt == 3:
                raise
            time.sleep(5 * (attempt + 1))


def ia_rows():
    page, out = 1, {}
    while True:
        docs = fetch_page(page)
        for d in docs:
            out[d["identifier"]] = (int(d.get("downloads") or 0), int(d.get("week") or 0))
        if len(docs) < ROWS:
            return out
        page += 1


def build(catalog, ia):
    """catalog rows are [id, title, authors, ..., ia identifier at index 8, ...]"""
    s = {}
    for r in catalog.get("b", []):
        ident = r[8] if len(r) > 8 else ""
        if ident and ident in ia:
            d, w = ia[ident]
            if d > 0:
                s[str(r[0])] = [d, w]
    return {"v": 1, "t": int(time.time()), "s": s}


def read_json(path):
    op = gzip.open if path.endswith(".gz") else open
    with op(path, "rt", encoding="utf-8") as f:
        return json.load(f)


if __name__ == "__main__":
    cat_path, out_path = (sys.argv + ["catalog.json", "stats.json"][len(sys.argv) - 1:])[1:3]
    out = build(read_json(cat_path), ia_rows())
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, separators=(",", ":"))
    print(f"{len(out['s'])} books with stats -> {out_path}")
