#!/usr/bin/env python3
"""Finds the end of the spoken LibriVox intro and the start of the spoken outro of every track.
Reads data/<book id>/<track>.json ({"u","w","t"}), writes cuts.json:
  {"v":1,"b":{"<book id>":{"<track>":[intro_end_seconds, outro_start_seconds_or_null]}},"h":{...}}
Plain standard library only. Run from the repository root."""
import hashlib, json, os, re, sys

LOGIC = "1"          # bump when the detection rules change, so every track is processed again
GAP = 0.8            # seconds between two word starts that counts as a real pause
MAX_INTRO = 90.0     # an intro end at or past this is ignored
KEYS = {"chapter", "section", "book", "part", "volume", "story", "poem", "letter", "act", "canto", "preface", "introduction"}


def norm(words):
    return [re.sub(r"[^a-z0-9]", "", w.lower()) for w in words]


def islib(x):
    return x.startswith("lib") and "vox" in x


def find_intro(nw, t):
    """End of the spoken intro in seconds (0 when no intro is found)."""
    n = len(nw)
    head = min(n, 120)
    anchors = []
    for i in range(head):
        x = nw[i]
        if x == "public" and nw[i + 1:i + 2] == ["domain"]:
            anchors.append(i + 2)
        if islib(x):
            if x.endswith("org"):
                anchors.append(i + 1)
            elif nw[i + 1:i + 2] == ["org"]:
                anchors.append(i + 2)
            elif nw[i + 1:i + 3] == ["dot", "org"]:
                anchors.append(i + 3)
    if not anchors:
        return 0.0
    e = max(anchors)                       # index of the first word after the boilerplate
    start, rb = e, False
    for i in range(e, min(e + 25, n - 1)):  # "recording by <reader>"
        if nw[i] == "recording" and nw[i + 1] == "by":
            e, rb = i + 2, True
            break
    if rb:
        lo, fallback = e + 1, e + 2         # at least one word of the reader's name
    else:
        lo, fallback = start, start
    idx = None
    for j in range(lo, min(lo + 16, n)):
        if j > 0 and t[j] - t[j - 1] > GAP:
            idx = j
            break
    if idx is None:
        idx = fallback
    if idx >= n:
        return 0.0
    val = float(t[idx])
    return val if 0 < val < MAX_INTRO else 0.0


def find_outro(nw, t):
    """Start of the spoken outro in seconds, or None."""
    n = len(nw)
    if n < 5:
        return None
    end = float(t[-1])
    for i in range(max(0, n - 80), n - 2):
        if nw[i] == "end" and nw[i + 1] == "of":
            ok = nw[i + 2] in KEYS or i >= n - 25   # "end of <title>" only counts right at the end
            if ok and t[i] >= 0.75 * end and t[i] < end:
                return round(float(t[i]), 2)
    return None


def cuts_for(doc):
    w, t = doc.get("w"), doc.get("t")
    if not isinstance(w, str) or not isinstance(t, list):
        return None
    words = w.split(" ")
    if len(words) != len(t) or len(words) < 5:
        return None
    nw = norm(words)
    a = find_intro(nw, t)
    o = find_outro(nw, t)
    if o is not None and o <= a:
        o = None
    return [round(a, 2), o]


def main(root="."):
    path = os.path.join(root, "cuts.json")
    cur = {"v": 1, "b": {}, "h": {}}
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        if d.get("v") == 1:
            cur = {"v": 1, "b": d.get("b") or {}, "h": d.get("h") or {}}
    except Exception:
        pass
    data = os.path.join(root, "data")
    if os.path.isdir(data):
        for book in sorted(os.listdir(data)):
            bdir = os.path.join(data, book)
            if not os.path.isdir(bdir):
                continue
            tracks = {}
            for fn in os.listdir(bdir):
                m = re.fullmatch(r"(\d+)\.json", fn)
                if not m:
                    continue
                n = m.group(1)
                key = book + "/" + n
                with open(os.path.join(bdir, fn), "rb") as f:
                    raw = f.read()
                h = hashlib.md5(raw).hexdigest()[:8] + LOGIC
                old = (cur["b"].get(book) or {}).get(n)
                if cur["h"].get(key) == h and old is not None:
                    tracks[n] = old
                    continue
                try:
                    c = cuts_for(json.loads(raw.decode("utf-8")))
                except Exception:
                    c = None
                if c is None:
                    cur["h"].pop(key, None)
                    continue
                tracks[n] = c
                cur["h"][key] = h
            if tracks:
                cur["b"][book] = dict(sorted(tracks.items(), key=lambda kv: int(kv[0])))
            else:
                cur["b"].pop(book, None)
    keep = {b + "/" + n for b, tr in cur["b"].items() for n in tr}
    cur["h"] = {k: v for k, v in cur["h"].items() if k in keep}
    out = json.dumps(cur, separators=(",", ":"), sort_keys=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(out)
    os.replace(tmp, path)
    print("cuts.json: %d books, %d tracks" % (len(cur["b"]), len(keep)))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ".")
  
