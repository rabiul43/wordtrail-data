#!/usr/bin/env python3
"""
wt23_build_packs.py - packs each pre-synced book into ONE compressed file.

Reads   data/<bookId>/<track>.json        (the per-track sync files your sync job already writes)
Writes  wt_packs/<bookId>.json.gz         (one gzip file per complete book)
        wt_packs/index.json               (which books have a pack, track count and size)

Only COMPLETE books are packed (every track present), so the app never loads a half-synced book from a pack.
Output is byte-for-byte stable, so an unchanged book produces no git diff.

Optional: wt23_popular_ids.txt (one book id per line, # for comments). If it exists and is not empty,
only those books are packed. Otherwise every complete book under data/ is packed.

Usage:  python wt23_build_packs.py [--data data] [--out wt_packs] [--catalog catalog.json] [--ids wt23_popular_ids.txt]
"""
import argparse, gzip, json, os, re, sys


def load_expected_counts(catalog_path):
    """book id -> number of tracks, from catalog.json (row layout: [id, title, authors, last, lang, gid, total, n, ...])."""
    try:
        with open(catalog_path, encoding="utf-8") as f:
            d = json.load(f)
        return {str(r[0]): int(r[7]) for r in d.get("b", []) if len(r) > 7 and r[7]}
    except Exception:
        return {}


def load_ids(path):
    if not os.path.isfile(path):
        return None
    ids = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if line:
                ids.add(line)
    return ids or None


def read_book(book_dir, expected_n):
    """Returns (n, tracks) when the book is complete, else None."""
    found = {}
    for name in os.listdir(book_dir):
        m = re.fullmatch(r"(\d+)\.json", name)
        if m:
            found[int(m.group(1))] = os.path.join(book_dir, name)
    if not found:
        return None
    n = expected_n or (max(found) + 1)
    if set(found) != set(range(n)):
        return None  # a track is missing (or extra): not complete yet
    tracks = {}
    for i in range(n):
        try:
            with open(found[i], encoding="utf-8") as f:
                tracks[str(i)] = json.load(f)
        except Exception:
            return None  # unreadable file: skip the whole book rather than ship a broken pack
    return n, tracks


def pack_bytes(book_id, n, tracks):
    raw = json.dumps({"v": 1, "id": book_id, "n": n, "tracks": tracks},
                     separators=(",", ":"), ensure_ascii=False, sort_keys=True).encode("utf-8")
    # mtime=0 keeps the gzip header constant, so identical content gives identical bytes
    return gzip.compress(raw, compresslevel=9, mtime=0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data")
    ap.add_argument("--out", default="wt_packs")
    ap.add_argument("--catalog", default="catalog.json")
    ap.add_argument("--ids", default="wt23_popular_ids.txt")
    a = ap.parse_args()

    if not os.path.isdir(a.data):
        print("No '%s' folder, nothing to pack." % a.data)
        return 0
    os.makedirs(a.out, exist_ok=True)

    expected = load_expected_counts(a.catalog)
    only = load_ids(a.ids)
    index, wrote, kept, skipped = {}, 0, 0, 0

    for book_id in sorted(os.listdir(a.data)):
        book_dir = os.path.join(a.data, book_id)
        if not (os.path.isdir(book_dir) and book_id.isdigit()):
            continue
        if only is not None and book_id not in only:
            continue
        res = read_book(book_dir, expected.get(book_id))
        if not res:
            skipped += 1
            continue
        n, tracks = res
        blob = pack_bytes(book_id, n, tracks)
        path = os.path.join(a.out, book_id + ".json.gz")
        same = False
        if os.path.isfile(path):
            with open(path, "rb") as f:
                same = f.read() == blob
        if same:
            kept += 1
        else:
            with open(path, "wb") as f:
                f.write(blob)
            wrote += 1
        index[book_id] = {"n": n, "bytes": len(blob)}

    # drop packs for books that no longer qualify
    for name in os.listdir(a.out):
        m = re.fullmatch(r"(\d+)\.json\.gz", name)
        if m and m.group(1) not in index:
            os.remove(os.path.join(a.out, name))

    with open(os.path.join(a.out, "index.json"), "w", encoding="utf-8") as f:
        # "b": what the app reads ({id: [tracks timed, tracks in book]}); "books": size details for people
        json.dump({"v": 1, "b": {k: [x["n"], x["n"]] for k, x in index.items()}, "books": index},
                  f, separators=(",", ":"), sort_keys=True)

    print("packs written: %d, unchanged: %d, incomplete (skipped): %d, total packed: %d"
          % (wrote, kept, skipped, len(index)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
  
