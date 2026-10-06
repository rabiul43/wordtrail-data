#!/usr/bin/env python3
"""Wordtrail alignment checker (nightly). Compares what Whisper heard (data/<id>/<track>.json) with the Gutenberg text.
Writes align.json {id: score, status ok/weak/poor/broken}. Broken timing files are deleted and re-synced.
  python wt_align.py            check books, write align.json, delete broken track files
  python wt_align.py --issues   open the 'sync <id>' issues for the deleted tracks (run AFTER the push)"""
import json, os, re, sys, time, urllib.request
from bisect import bisect_left

DATA = "data"
PER_NIGHT, MAX_FIX_NIGHT, MAX_FIX_BOOK, COOLDOWN = 25, 3, 2, 7 * 86400
TOK = re.compile(r"[a-z0-9]+(?:['\u2019][a-z]+)?")
UA = {"User-Agent": "wordtrail-align"}

def load(p, default):
    try:
        with open(p, encoding="utf-8") as f: return json.load(f)
    except (OSError, ValueError): return default

def toks(s): return [w.replace("'", "").replace("\u2019", "") for w in TOK.findall(s.lower())]

def clean(txt):
    a = re.search(r"\*\*\* ?START OF[^\n]*\n", txt); b = txt.rfind("*** END OF")
    if a: txt = txt[a.end(): b if b > a.end() else len(txt)]
    txt = re.sub(r"\[\s*Footnote[^:\]\n]{0,12}:.{0,3000}?\]", " ", txt, flags=re.S | re.I)
    txt = re.sub(r"\[\s*(?:Illustration|Pg|Page)[^\]]{0,300}\]|\{\d+\}", " ", txt, flags=re.S | re.I)
    return txt.replace("_", "")

def fetch_text(gid):
    url = f"https://www.gutenberg.org/cache/epub/{gid}/pg{gid}.txt"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
            return r.read().decode("utf-8", "ignore")
    except Exception as e:
        print("text skipped", gid, e); return None

def index(words):
    pos = {}
    for i in range(len(words) - 2): pos.setdefault((words[i], words[i + 1], words[i + 2]), []).append(i)
    return pos

def check_track(tr, pos):
    """-> (score 0-100 or None, problem). A problem means the timing file itself is bad (worth re-syncing)."""
    w, t = str(tr.get("w", "")).split(), tr.get("t", [])
    if len(w) < 30: return None, "empty"
    if len(w) != len(t): return None, "mismatched"
    if any(b < a for a, b in zip(t, t[1:])): return None, "timing order"
    tri = [(w[i], w[i + 1], w[i + 2]) for i in range(len(w) - 2)]
    if len(set(tri)) < 0.3 * len(tri): return None, "looping"   # Whisper repeating itself
    p, miss, hit = None, 0, 0
    for g in tri:
        c = pos.get(g)
        if not c: miss += 1; continue
        if p is None:                                  # wait for a phrase that occurs once in the book
            if len(c) == 1: p, hit, miss = c[0], hit + 1, 0
            continue
        k = bisect_left(c, p - 29)
        if k < len(c) and c[k] - p <= 600 + 15 * miss: p, hit, miss = c[k], hit + 1, 0
        else: miss += 1
    return round(100 * hit / len(tri)), ""

def check_book(bid, words_pos):
    scores, bad = [], []
    files = sorted((f for f in os.listdir(os.path.join(DATA, bid)) if re.fullmatch(r"\d+\.json", f)), key=lambda f: int(f[:-5]))
    for f in files:
        tr = load(os.path.join(DATA, bid, f), None)
        if not isinstance(tr, dict): bad.append(int(f[:-5])); continue
        s, prob = check_track(tr, words_pos)
        if prob: bad.append(int(f[:-5]))
        else: scores.append((s, len(str(tr.get("w", "")).split())))
    n = sum(x[1] for x in scores)
    score = round(sum(s * k for s, k in scores) / n) if n else 0
    st = "broken" if bad else "ok" if score >= 55 else "weak" if score >= 30 else "poor"
    return score, st, bad

def busy(bid, now):
    s = load(os.path.join(DATA, bid, "status.json"), {})
    try: upd = float(s.get("updated") or 0)
    except (TypeError, ValueError): upd = 0
    return str(s.get("stage", "")).lower() not in ("", "done", "error", "complete", "completed", "ready", "finished") and now - upd < 8 * 3600

def main():
    now = time.time()
    rows = {str(r[0]): r for r in load("catalog.json", {}).get("b", [])}
    res = load("align.json", {}).get("b", {})
    can_fix = bool(os.environ.get("WT_PAT"))
    cands = [b for b in (os.listdir(DATA) if os.path.isdir(DATA) else [])
             if os.path.isdir(os.path.join(DATA, b)) and b in rows and rows[b][5]
             and any(re.fullmatch(r"\d+\.json", f) for f in os.listdir(os.path.join(DATA, b))) and not busy(b, now)]
    cands.sort(key=lambda b: res.get(b, {}).get("t", 0))   # least recently checked first
    jobs, fixed = [], 0
    for bid in cands[:PER_NIGHT]:
        txt = fetch_text(rows[bid][5]); time.sleep(1)
        if not txt: continue
        score, st, bad = check_book(bid, index(toks(clean(txt))))
        old = res.get(bid, {})
        e = {"s": score, "st": st, "t": int(now), "rs": old.get("rs", 0), "rt": old.get("rt", 0), "n": rows[bid][1]}
        if bad: e["bad"] = bad
        if st == "broken" and can_fix and fixed < MAX_FIX_NIGHT and e["rs"] < MAX_FIX_BOOK and now - e["rt"] > COOLDOWN:
            for i in bad:
                try: os.remove(os.path.join(DATA, bid, f"{i}.json"))
                except OSError: pass
            e["rs"] += 1; e["rt"] = int(now); fixed += 1
            jobs.append({"id": bid, "cur": bad[0], "title": rows[bid][1]})
        res[bid] = e
        print(f"book {bid}: score={score} {st} bad={bad}")
    with open("align.json", "w", encoding="utf-8") as f: json.dump({"v": 1, "t": int(now), "b": res}, f, ensure_ascii=False, separators=(",", ":"))
    with open("align_resync.json", "w", encoding="utf-8") as f: json.dump(jobs, f)
    print(f"checked={min(len(cands), PER_NIGHT)} to_resync={len(jobs)}")

def issues():
    repo, token = os.environ.get("REPO", ""), os.environ.get("WT_PAT", "")
    jobs = load("align_resync.json", [])
    if not (jobs and repo and token): print("no resync needed"); return
    for j in jobs:
        body = json.dumps({"title": "sync " + j["id"], "body": f"cur={j['cur']}\n{j['title']}"}).encode()
        req = urllib.request.Request(f"https://api.github.com/repos/{repo}/issues", data=body, method="POST", headers={
            "Authorization": "Bearer " + token, "Accept": "application/vnd.github+json", "User-Agent": "wordtrail-align"})
        try:
            urllib.request.urlopen(req, timeout=30).read(); print("resync requested:", j["id"])
        except Exception as e: print("issue failed:", j["id"], e)

if __name__ == "__main__": issues() if "--issues" in sys.argv else main()
      
