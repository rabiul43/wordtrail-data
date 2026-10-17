#!/usr/bin/env python3
"""Builds stats.json: Internet Archive plays for every LibriVox recording in catalog.json.

    python build_stats.py catalog.json stats.json

stats.json = {"v": 1, "t": <unix time>, "s": {"<book id>": [downloads, last 7 days], ...}}
Needs only the Python standard library. Prints what Archive says if something goes wrong.
"""
import gzip, json, sys, time, urllib.error, urllib.parse, urllib.request

UA = {"User-Agent": "wordtrail-stats/1.1 (+https://github.com)"}
SCRAPE = "https://archive.org/services/search/v1/scrape"
SEARCH = "https://archive.org/advancedsearch.php"
QUERY = "collection:librivoxaudio"


def get_json(url, tries=4):
    last = ""
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            last = "HTTP %s: %s" % (e.code, e.read()[:600].decode("utf-8", "replace"))
            if e.code in (400, 404):
                break  # asking again will not help
        except Exception as e:  # network trouble, bad JSON
            last = repr(e)
        time.sleep(5 * (attempt + 1))
    raise RuntimeError("request failed: %s\n%s" % (url, last))


def pair(d):
    return int(d.get("downloads") or 0), int(d.get("week") or 0)


def scrape_rows(fields):
    """Archive's scrape API: no page limit, walks through results with a cursor."""
    out, cursor = {}, None
    while True:
        params = [("q", QUERY), ("fields", ",".join(fields)), ("count", 10000)]
        if cursor:
            params.append(("cursor", cursor))
        j = get_json(SCRAPE + "?" + urllib.parse.urlencode(params))
        if "items" not in j:
            raise RuntimeError("unexpected reply: " + json.dumps(j)[:600])
        for d in j["items"]:
            out[d["identifier"]] = pair(d)
        cursor = j.get("cursor")
        if not cursor or not j["items"]:
            return out


def search_rows():
    """Older advancedsearch API, used only as a fallback (it may stop after about 10,000 results)."""
    out, page, rows = {}, 1, 1000
    while page * rows <= 10000:
        params = [("q", QUERY), ("fl[]", "identifier"), ("fl[]", "downloads"), ("fl[]", "week"),
                  ("rows", rows), ("page", page), ("output", "json")]
        j = get_json(SEARCH + "?" + urllib.parse.urlencode(params))
        if "response" not in j:
            raise RuntimeError("unexpected reply: " + json.dumps(j)[:600])
        docs = j["response"]["docs"]
        for d in docs:
            out[d["identifier"]] = pair(d)
        if len(docs) < rows:
            break
        page += 1
    return out


def ia_rows():
    attempts = [
        ("scrape API (downloads + week)", lambda: scrape_rows(["identifier", "downloads", "week"])),
        ("scrape API (downloads only)", lambda: scrape_rows(["identifier", "downloads"])),
        ("advancedsearch API", search_rows),
    ]
    problems = []
    for name, fn in attempts:
        try:
            rows = fn()
            if rows:
                print("Archive data from: %s (%d items)" % (name, len(rows)))
                return rows
            problems.append("%s: returned no items" % name)
        except Exception as e:
            problems.append("%s: %s" % (name, e))
        print("FAILED ->", problems[-1], file=sys.stderr)
    raise SystemExit("Could not get play counts from Internet Archive.\n" + "\n".join(problems))


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
    args = sys.argv[1:] + ["catalog.json", "stats.json"][max(0, len(sys.argv) - 1):]
    cat_path, out_path = args[0], args[1]
    catalog = read_json(cat_path)
    out = build(catalog, ia_rows())
    if not out["s"]:
        raise SystemExit("0 books matched. Check that catalog rows keep the Archive id at index 8.")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, separators=(",", ":"))
    print("%d books with stats -> %s" % (len(out["s"]), out_path))
