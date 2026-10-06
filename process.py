"""Wordtrail timing job. Runs on GitHub Actions (free) when the app opens an issue "sync <book id>".
Listens to each track with Whisper (the track being heard first), saves every spoken word with its
time to data/<book id>/<track>.json, and pushes after every track so the app can pick it up.
Progress (stage, message, percent) is written to data/<book id>/status.json for the app's progress bar."""
import json, os, re, subprocess, requests
from faster_whisper import WhisperModel
from wordtrail_status import Status
try:
    from wt_ctc import Refiner
    from faster_whisper.audio import decode_audio
except Exception as e:   # word refining is optional: without it the Whisper times are used as before
    Refiner = None; print("word refine unavailable:", e, flush=True)

m = re.fullmatch(r"sync (\d+)", os.environ.get("TITLE", "").strip())
if not m: raise SystemExit("bad title")
bid = m.group(1)
mm = re.search(r"cur=(\d+)", os.environ.get("BODY", "") or "")
cur = int(mm.group(1)) if mm else 0
BODY = os.environ.get("BODY", "") or ""
nm = re.search(r"^ntfy=([A-Za-z0-9_-]{6,64})\s*$", BODY, re.M)
ntfy_topic = nm.group(1) if nm else ""
book_title = next((l.strip() for l in BODY.splitlines() if l.strip() and not l.startswith(("cur=", "ntfy="))), "your book")

def alert(title, text):
    """Optional phone alert through the free ntfy app. Never stops the job."""
    if not ntfy_topic: return
    try:
        requests.post(f"https://ntfy.sh/{ntfy_topic}", data=text.encode("utf-8"), headers={"Title": title}, timeout=15)
    except Exception:
        pass

r = requests.get("https://librivox.org/api/feed/audiobooks", params={"id": bid, "format": "json", "extended": 1}, timeout=60)
r.raise_for_status()
urls = [s["listen_url"].replace("http:", "https:") for s in r.json()["books"][0]["sections"]]
order = [(cur + k) % len(urls) for k in range(len(urls))]
N = len(urls)
MODEL_NAME = "small.en"   # "base.en" = faster, "small.en" = more accurate (better for poetry and old words)
st = Status(bid, N)
st.update("queued", "Getting the speech model ready (first start takes a minute)", force=True)

def toks(s):
    return [w.replace("'", "").replace("\u2019", "") for w in re.findall(r"[a-z0-9]+(?:['\u2019][a-z]+)?", s.lower())]

def push(path):
    subprocess.run(["git", "add", path], check=True)
    subprocess.run(["git", "commit", "-m", f"timing {bid}"], check=True)
    for _ in range(6):
        if subprocess.run(["git", "push", "origin", "HEAD"]).returncode == 0: return
        subprocess.run(["git", "pull", "--rebase", "--autostash"])

try:
    model = WhisperModel(MODEL_NAME, device="cpu", compute_type="int8", cpu_threads=os.cpu_count() or 2)
    ref = None
    if Refiner:
        try: ref = Refiner()
        except Exception as e: print("word refine off:", e, flush=True)
    os.makedirs(f"data/{bid}", exist_ok=True)
    for pos, i in enumerate(order, 1):
        out = f"data/{bid}/{i}.json"
        if os.path.exists(out): continue
        mp3 = f"/tmp/t{i}.mp3"
        st.update("downloading", f"Downloading track {i + 1} of {N}", pos, 0.0)
        with requests.get(urls[i], stream=True, timeout=120) as resp:
            resp.raise_for_status()
            with open(mp3, "wb") as f:
                for c in resp.iter_content(1 << 20): f.write(c)
        audio = None
        if ref:
            try: audio = decode_audio(mp3, sampling_rate=16000)
            except Exception as e: print("decode failed, using Whisper times:", e, flush=True)
        segs, info = model.transcribe(mp3, language="en", word_timestamps=True, beam_size=1, condition_on_previous_text=False)
        W, T, nref = [], [], 0
        for s in segs:
            sw, ss = [], []
            for w in s.words:
                for t in toks(w.word): sw.append(t); ss.append(w.start)
            if audio is not None and sw:
                try:
                    new = ref.refine(audio, s.start, s.end, sw, ss)
                    nref += sum(1 for a, b in zip(new, ss) if a != b); ss = new
                except Exception as e: print("refine skipped:", str(e)[:100], flush=True)
            for t, x in zip(sw, ss): W.append(t); T.append(round(x, 2))
            if info.duration: st.update("transcribing", f"Listening to track {i + 1} of {N}", pos, min(1.0, s.end / info.duration))
        os.remove(mp3)
        for k in range(1, len(T)):
            if T[k] < T[k - 1]: T[k] = T[k - 1]   # keep times in order across segments
        json.dump({"u": urls[i], "w": " ".join(W), "t": T}, open(out, "w"), separators=(",", ":"))
        push(out)
        print("track", i + 1, "of", len(urls), len(W), "words", nref, "re-timed", flush=True)
    st.update("done", "All tracks ready", force=True)
    alert("Sync finished", f"{book_title} is ready to read along.")
except BaseException as e:
    st.update("error", f"{type(e).__name__}: {str(e)[:150]}", force=True)
    alert("Sync problem", f"{book_title}: {type(e).__name__}")
    raise
