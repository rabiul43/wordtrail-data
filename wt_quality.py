#!/usr/bin/env python3
"""Wordtrail audio quality score -> quality.json
Format: {"v":1,"b":{"<book id>":{"q":0-100,"snr":dB,"lufs":LUFS,"sil":0-1,"jump":dB,"t":unix}}}
Samples ~2 tracks of each recording with ffmpeg (about 2 minutes each) and scores noise, loudness,
clipping, silence and volume jumps between tracks. Only recordings that appear in a duplicate group
(versions.json) are measured, best-ranked first. Results are cached; a run measures at most
WT_QUALITY_LIMIT books (default 30), so it stays fast and free."""
import array, json, math, os, re, subprocess, sys, tempfile, time, urllib.parse, urllib.request, wave

SAMPLE_SEC = 120
LIMIT = int(os.environ.get("WT_QUALITY_LIMIT", "30"))
REFRESH_DAYS, RETRY_DAYS = 120, 14
UA = {"User-Agent": "wordtrail-quality"}


def load(p, default):
    try:
        with open(p, encoding="utf-8") as f: return json.load(f)
    except (OSError, ValueError): return default


def get_json(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=40) as r:
        return json.load(r)


def mp3_files(ia):
    d = get_json("https://archive.org/metadata/" + urllib.parse.quote(ia))
    fs = [f for f in d.get("files", [])
          if str(f.get("name", "")).lower().endswith(".mp3") and "mp3" in str(f.get("format", "")).lower()
          and "sample" not in str(f.get("format", "")).lower()]
    small = [f for f in fs if "64kbps" in str(f.get("format", "")).lower()]
    return sorted(f["name"] for f in (small or fs))


def pick(names):
    n = len(names)
    if n == 0: return []
    if n == 1: return [names[0]]
    return sorted({names[n // 4], names[(3 * n) // 4]})


def grab(ia, name, start, out):
    url = "https://archive.org/download/%s/%s" % (ia, urllib.parse.quote(name))
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(start), "-t", str(SAMPLE_SEC), "-i", url,
                    "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", out],
                   check=True, timeout=240, capture_output=True)


def ff(path, af):
    return subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", path, "-af", af, "-f", "null", "-"],
                          capture_output=True, text=True, timeout=120).stderr


def analyze(path):
    """One 16 kHz mono wav -> dict(lufs, tp, sil, snr, sec) or None if the sample is unusable."""
    with wave.open(path) as w:
        sr, raw = w.getframerate(), w.readframes(w.getnframes())
    a = array.array("h"); a.frombytes(raw)
    sec = len(a) / float(sr)
    if sec < 20: return None
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", ff(path, "loudnorm=I=-19:TP=-1.5:print_format=json"), re.S)
    if not m: return None
    j = json.loads(m.group(0))
    lufs, tp = float(j["input_i"]), float(j["input_tp"])
    if not math.isfinite(lufs): return None
    sil = sum(float(x) for x in re.findall(r"silence_duration: ([\d.]+)", ff(path, "silencedetect=n=-38dB:d=0.4")))
    win, lv = sr // 2, []
    for i in range(0, len(a) - win + 1, win):
        e = sum(x * x for x in a[i:i + win]) / win
        lv.append(10 * math.log10(e / 32768.0 ** 2) if e > 0 else -96.0)
    lv.sort()
    snr = max(0.0, min(60.0, lv[int(len(lv) * 0.9)] - lv[int(len(lv) * 0.1)]))
    return {"lufs": lufs, "tp": tp, "sil": min(1.0, sil / sec), "snr": snr, "sec": sec}


def score(snr, lufs, tp, sil, jump):
    s = 40 * min(1, max(0, (snr - 15) / 25.0))                 # clean: speech well above the noise floor
    s += 20 * max(0, 1 - abs(lufs + 19) / 10.0)                # loudness near -19 LUFS
    s += 10 if tp < -1 else 5 if tp < 0 else 0                 # no clipping
    s += 15 if 0.04 <= sil <= 0.22 else 7 if sil <= 0.35 else 0   # natural pauses, not dead air
    s += 15 * max(0, 1 - jump / 6.0) if jump is not None else 8    # steady volume between tracks
    return int(round(s))


def measure(ia):
    names = pick(mp3_files(ia))
    res = []
    with tempfile.TemporaryDirectory() as tmp:
        for k, name in enumerate(names):
            out = os.path.join(tmp, "s%d.wav" % k)
            for start in (45, 0):                               # short file: retry from the start
                try: grab(ia, name, start, out)
                except Exception as e:
                    print("  grab failed:", name, str(e)[:80]); continue
                r = analyze(out)
                if r: res.append(r); break
    if not res: return None
    avg = lambda key: sum(r[key] for r in res) / len(res)
    jump = abs(res[0]["lufs"] - res[1]["lufs"]) if len(res) > 1 else None
    snr, lufs, sil, tp = avg("snr"), avg("lufs"), avg("sil"), max(r["tp"] for r in res)
    return {"q": score(snr, lufs, tp, sil, jump), "snr": round(snr, 1), "lufs": round(lufs, 1),
            "sil": round(sil, 2), "jump": None if jump is None else round(jump, 1), "t": int(time.time())}


def candidates(rows, done):
    """(book id, archive.org id) from duplicate groups, best-ranked first, skipping fresh results."""
    now, todo = time.time(), []
    for g in load("versions.json", {}).get("g", []):
        for rank, i in enumerate(g.get("i", [])[:4]):
            i = str(i); r = rows.get(i); d = done.get(i)
            if not r or not r[8]: continue
            age = (now - d["t"]) / 86400.0 if d else None
            if d and age < (RETRY_DAYS if d.get("q") is None else REFRESH_DAYS): continue
            todo.append((rank, i, r[8]))
    todo.sort(key=lambda x: (x[0], int(x[1])))
    return [(i, ia) for _, i, ia in todo]


def main():
    rows = {str(r[0]): r for r in load("catalog.json", {}).get("b", [])}
    out = load("quality.json", {"v": 1, "b": {}}); out.setdefault("b", {})
    todo = candidates(rows, out["b"])[:LIMIT]
    print("to measure this run:", len(todo))
    for n, (i, ia) in enumerate(todo, 1):
        try: res = measure(ia)
        except Exception as e:
            print(i, ia, "skipped:", str(e)[:100]); res = None
        out["b"][i] = res or {"q": None, "t": int(time.time())}
        print(i, ia, res and res["q"])
        if n % 5 == 0 or n == len(todo):
            with open("quality.json", "w", encoding="utf-8") as f: json.dump(out, f, separators=(",", ":"))
    if not todo and not os.path.exists("quality.json"):
        with open("quality.json", "w", encoding="utf-8") as f: json.dump(out, f, separators=(",", ":"))


if __name__ == "__main__": main()
  
