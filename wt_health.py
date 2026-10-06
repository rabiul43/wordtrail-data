#!/usr/bin/env python3
"""Wordtrail sync dashboard + auto maintenance.
Writes health.json (synced / pending / failed books) and cleans stale folders and old failed workflow runs.
Settings come from maintenance.json: {"days": 40, "live": false}. live=false only reports what it would delete."""
import calendar, json, os, re, shutil, sys, time, urllib.request

DATA, STALLED_HOURS = "data", 8
DONE = ("done", "complete", "completed", "ready", "finished")

def load(p, default):
    try:
        with open(p, encoding="utf-8") as f: return json.load(f)
    except (OSError, ValueError): return default

def settings():
    c = load("maintenance.json", {})
    try: days = int(c.get("days", 40))
    except (TypeError, ValueError): days = 40
    return max(7, min(365, days)), bool(c.get("live", False))

def folder_size(d):
    return sum(os.path.getsize(os.path.join(a, f)) for a, _, fs in os.walk(d) for f in fs)

def scan(titles, now, days):
    out = {"synced": [], "pending": [], "failed": []}
    stale, size = [], 0
    for bid in sorted(os.listdir(DATA)) if os.path.isdir(DATA) else []:
        d = os.path.join(DATA, bid)
        if not os.path.isdir(d): continue
        size += folder_size(d)
        files = os.listdir(d)
        st = load(os.path.join(d, "status.json"), {})
        stage = str(st.get("stage", "")).lower()
        try: upd, pct = float(st.get("updated") or 0), float(st.get("pct") or 0)
        except (TypeError, ValueError): upd, pct = 0, 0
        has_book = "book.json" in files
        tracks = any(re.fullmatch(r"\d+\.json", f) for f in files)
        title = titles.get(bid, "Book " + bid)
        if has_book or stage in DONE or pct >= 1:
            out["synced"].append([bid, title])
        elif stage == "error":
            out["failed"].append([bid, title, str(st.get("msg", ""))[:120], int(upd)])
        elif stage and upd and (now - upd) / 3600 > STALLED_HOURS:
            out["failed"].append([bid, title, "Stopped without finishing", int(upd)])
        elif stage or tracks:
            out["pending"].append([bid, title, round(pct * 100), int(upd)])
        # stale: nothing usable left (no finished book, no timed tracks) and untouched for `days`
        if not has_book and not tracks and upd and (now - upd) / 86400 > days:
            stale.append(d)
    return out, stale, size

def gh(method, path, token):
    req = urllib.request.Request("https://api.github.com" + path, method=method, headers={
        "Authorization": "Bearer " + token, "Accept": "application/vnd.github+json", "User-Agent": "wordtrail-health"})
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read()
        return json.loads(body) if body else {}

def old_failed_runs(repo, token, cutoff, skip):
    found = []
    for status in ("failure", "cancelled", "timed_out"):
        for page in range(1, 6):
            try: d = gh("GET", f"/repos/{repo}/actions/runs?status={status}&per_page=100&page={page}", token)
            except Exception as e:
                print("runs skipped:", e); break
            runs = d.get("workflow_runs", [])
            for r in runs:
                made = calendar.timegm(time.strptime(r["created_at"], "%Y-%m-%dT%H:%M:%SZ"))
                if made < cutoff and str(r["id"]) != str(skip): found.append(r["id"])
            if len(runs) < 100: break
    return found

def main():
    now = time.time()
    days, live = settings()
    titles = {str(r[0]): r[1] for r in load("catalog.json", {}).get("b", [])}
    books, stale, size = scan(titles, now, days)
    repo, token = os.environ.get("REPO", ""), os.environ.get("GH_TOKEN", "")
    runs = old_failed_runs(repo, token, now - days * 86400, os.environ.get("RUN_ID", "")) if repo and token else []
    if live:
        for d in stale: shutil.rmtree(d, ignore_errors=True)
        for rid in runs[:200]:
            try: gh("DELETE", f"/repos/{repo}/actions/runs/{rid}", token)
            except Exception as e: print("run not deleted:", rid, e)
    report = {"v": 1, "t": int(now), "mb": round(size / 1048576, 1),
              "n": {k: len(v) for k, v in books.items()},
              "failed": books["failed"], "pending": books["pending"], "synced": books["synced"][:300],
              "clean": {"live": live, "days": days, "dirs": stale[:50], "runs": len(runs)}}
    with open("health.json", "w", encoding="utf-8") as f: json.dump(report, f, ensure_ascii=False, separators=(",", ":"))
    print(f"health.json: {report['n']} | stale folders={len(stale)} old failed runs={len(runs)} live={live}")

if __name__ == "__main__": main()
      
