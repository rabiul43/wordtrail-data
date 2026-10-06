"""Wordtrail word-time refiner (CTC forced alignment).

Whisper already gives every spoken word a start time, but its word times can be 0.1-0.4 s off.
This takes Whisper's words for one segment and re-times them against the audio with a small
wav2vec2 model (free, CPU only): the words stay exactly the same, only their start times get
more precise. If anything goes wrong for a segment, the original Whisper times are kept, so it
can never make a sync worse than it was.
Used by process.py. Needs torch + torchaudio (see sync.yml)."""
import numpy as np

SR = 16000
FRAME = 0.02          # wav2vec2 output step: 20 ms
PAD = 0.4             # extra audio around each segment, seconds
MAXD = 0.8            # a refined time more than this far from Whisper's is not trusted
SHIFT = 0.0           # seconds added to every refined start (tune if highlights feel early/late)


def ctc_starts(lp, ids, blank=0):
    """Viterbi forced alignment. lp: (frames, classes) log-probs. ids: label ids to place in order.
    Returns the first frame of every id, or None when the audio is too short for the labels."""
    T, n = lp.shape[0], len(ids)
    if n == 0 or T < n + sum(1 for k in range(1, n) if ids[k] == ids[k - 1]): return None
    S = 2 * n + 1
    lab = np.full(S, blank, dtype=np.int64); lab[1::2] = ids
    em = lp[:, lab]
    NEG = -1e30
    skip = np.zeros(S, dtype=bool)
    if n > 1: skip[3::2] = lab[3::2] != lab[1:S - 2:2]
    dp = np.full(S, NEG); dp[0] = em[0, 0]; dp[1] = em[0, 1]
    bp = np.zeros((T, S), dtype=np.int8)
    rows = np.arange(S)
    for t in range(1, T):
        c1 = np.concatenate(([NEG], dp[:-1]))
        c2 = np.where(skip, np.concatenate(([NEG, NEG], dp[:-2])), NEG)
        st = np.stack([dp, c1, c2]); k = st.argmax(0)
        dp = st[k, rows] + em[t]; bp[t] = k
    s = S - 1 if dp[S - 1] >= dp[S - 2] else S - 2
    first = {}
    for t in range(T - 1, -1, -1):
        if s % 2 == 1: first[(s - 1) // 2] = t
        s -= int(bp[t][s])
        if s < 0: return None
    return [first.get(k) for k in range(n)]


class Refiner:
    def __init__(self):
        import torch, torchaudio
        torch.set_num_threads(max(1, __import__("os").cpu_count() or 2))
        bundle = torchaudio.pipelines.WAV2VEC2_ASR_BASE_960H
        self.torch, self.model = torch, bundle.get_model().eval()
        labels = bundle.get_labels()
        self.idx = {c: i for i, c in enumerate(labels)}
        self.blank, self.sep = self.idx["-"], self.idx["|"]

    def _emission(self, wave):
        """float32 mono 16 kHz samples -> (frames, classes) log-probs."""
        with self.torch.inference_mode():
            em, _ = self.model(self.torch.from_numpy(np.ascontiguousarray(wave))[None])
            return self.torch.log_softmax(em[0], -1).numpy()

    def refine(self, audio, seg_start, seg_end, toks, starts):
        """audio: float32 mono 16 kHz for the whole track. toks/starts: Whisper's words and start
        times for one segment. Returns new start times (same length)."""
        out = list(starts)
        a0 = max(0.0, seg_start - PAD); a1 = min(len(audio) / SR, seg_end + PAD)
        if a1 - a0 < 0.5 or a1 - a0 > 40 or not toks: return out
        ids, first, skipped = [], {}, 0
        for wi, t in enumerate(toks):
            cs = [self.idx[c] for c in t.upper() if c in self.idx and c not in "-|"]
            if not cs: skipped += 1; continue          # digits etc.: keep Whisper's time
            if ids: ids.append(self.sep)
            first[wi] = len(ids); ids.extend(cs)
        if not first or skipped > 0.2 * len(toks): return out
        lp = self._emission(audio[int(a0 * SR):int(a1 * SR)])
        fr = ctc_starts(lp, ids, self.blank)
        if fr is None: return out
        new, bad = list(out), 0
        for wi, pos in first.items():
            if fr[pos] is None: bad += 1; continue
            v = a0 + fr[pos] * FRAME + SHIFT
            if abs(v - starts[wi]) > MAXD: bad += 1; continue
            new[wi] = v
        if bad > 0.25 * len(first): return out         # the audio does not match these words well
        for k in range(1, len(new)):                   # never go backwards
            if new[k] < new[k - 1]: new[k] = new[k - 1]
        return new
