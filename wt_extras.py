#!/usr/bin/env python3
"""wt_extras.py - find the extra spoken parts (LibriVox notice, book/author line, reader credits)
in Wordtrail timing packs, using the pack's own word-match marks (q), and write skip times.

Run:   python wt_extras.py PACK_DIR OUT_DIR [--pad-a SECS] [--pad-b SECS] [--resume]
  PACK_DIR    folder with timing-*.json.gz (or *.json) files
  OUT_DIR     gets extras-<book id>.json per book, and extras_report.txt (read it by eye)
  --pad-a     seconds to trim from the start of each cut (default 0.05)
  --pad-b     seconds to trim from the end of each cut (default 0.10)
  --resume    skip books whose extras file already exists in OUT_DIR (safe to re-run after a crash)

How it decides (all from the pack, no audio, no GPU):
  q has one mark per spoken word: '0' = the word is NOT in the book text, anything else = matched.
  - HEAD: everything before the first place where the book text really starts is extra.
          A spoken heading just before it ("Chapter 3 ...", "Part first ...") is KEPT.
  - TAIL: everything after the last place where the book text really ends is extra.
  - MIDDLE: a long unmatched run is cut ONLY if it contains a LibriVox marker phrase,
          so real book text that Whisper misheard is never cut.
"""
import argparse, gzip, json, os, re, sys

WIN, NEED = 20, 12          # a "real text" window: 12 matched words out of 20
MID_MIN_WORDS = 6           # shortest middle run we will consider
HEAD_MAX_S, MID_MAX_S, TAIL_MAX_S = 60.0, 90.0, 150.0
PAD_A, PAD_B = 0.05, 0.10   # seconds: start a little late, end a little early
HEADING = {'chapter', 'section', 'part', 'book', 'volume', 'canto', 'act', 'scene', 'story', 'poem',
           'preface', 'introduction', 'prologue', 'epilogue', 'prelude', 'contents'}
MARK = re.compile(r'(lib[a-z]*vox|public domain|recording|recorded by|read by|narrated by|proofread|'
                  r'end of (chapter|section|part|book|the)|for more information|to volunteer)')

def norm(w):
    return re.sub(r"[^a-z0-9']", '', w.lower())


NOTICE = re.compile(r"(lib[a-z]*[ -]*vox|public domain|for more information|to volunteer|"
                    r"(recording|reading|read|recorded|narrated|proofread)( has been| was| is)? by)")
def find_end(words, k, span):
    """index just after the first sentence end at or after word k, or None"""
    for x in range(k, min(len(words), k + span)):
        if re.search(r'[.!?]["\u201d\u2019\']*$', words[x]): return x + 1
    return None

def head_end(words, t, m, h):
    """End of the spoken notice at the start, or None when there is no notice. Markers are looked for up to 25 words
    past h (h = where the text match is dense). After the last marker's sentence, short sentences that are
    mostly NOT in the book text (title/author line, reader credit) go too. A sentence that starts with a
    heading word, or is long, or is mostly matched, is book text and stays."""
    lim = min(len(words), h + 25)
    flat, pos = '', []
    for x in range(lim):
        pos.append(len(flat)); flat += re.sub(r"[^a-z0-9' -]", '', words[x].lower()) + ' '
    last = None
    for mt in NOTICE.finditer(flat):
        x = max(i for i in range(lim) if pos[i] <= mt.start())
        if t[x] <= HEAD_MAX_S: last = x
    if last is None: return None
    j = find_end(words, last, 10) or last + 1
    for _ in range(3):
        e = find_end(words, j, 15)
        if e is None or e - j > 14 or j >= len(words) or norm(words[j]) in HEADING: break
        if sum(m[j:e]) / float(e - j) >= 0.35: break
        j = e
    return j

def keep_title(words):
    """Index where the extra part starts, when the track begins with a title that must stay:
       - a heading word first: "Chapter 3 of Dracula ..." / "Canto 5, Bickerment. From ..." keeps the heading part
       - "<Title> from <Book> by <Author>" (poems) keeps <Title>
       Returns 0 when there is no such title."""
    lim = min(len(words), 12)
    if norm(words[0]) in HEADING:
        for x in range(1, lim):
            w = norm(words[x])
            if w in ('from', 'by', 'of', 'in') or re.search(r'[.!?]$', words[x - 1]): return x
        return 0
    for x in range(1, lim):
        if norm(words[x]) == 'from' and not NOTICE.search(' '.join(words[:x]).lower()): return x
    return 0

def analyse(tr, pad_a=PAD_A, pad_b=PAD_B):
    words = tr['w'].split(' ')
    t = [float(x) for x in tr['t']]
    q = tr.get('q') or ''
    n = len(words)
    if n < 40 or len(t) != n or len(q) != n:
        return {'cuts': [], 'why': 'no-q' if not q else 'bad-length'}
    m = [1 if c != '0' else 0 for c in q]
    if sum(m) < 0.3 * n:
        return {'cuts': [], 'why': 'little-text-match'}
    pre = [0]
    for v in m: pre.append(pre[-1] + v)
    dens = lambda a, b: pre[min(n, b)] - pre[max(0, a)]
    cuts = []
    # HEAD: h = first matched word that starts a dense window (an upper limit, not the cut itself)
    h = next((i for i in range(0, n - WIN) if m[i] and dens(i, i + WIN) >= NEED), None)
    if h is not None:
        j = head_end(words, t, m, h)          # only a real notice makes a head cut
        if j is not None and j > 0 and t[j] <= HEAD_MAX_S:
            p = keep_title(words)             # a chapter title said BEFORE the notice is kept
            a0 = 0.0 if p == 0 or p >= j else round(t[p] - pad_a, 2)
            cuts.append({'type': 'head', 'a': a0, 'b': round(max(0.0, t[j] - pad_b), 2), 'i': p if a0 else 0, 'j': j})
    # TAIL: last matched word that ends a dense window
    e = next((i for i in range(n - 1, WIN - 1, -1) if m[i] and dens(i - WIN + 1, i + 1) >= NEED), None)
    if e is not None and n - 1 - e >= 4:
        s = e + 1
        if t[-1] - t[s] <= TAIL_MAX_S:
            cuts.append({'type': 'tail', 'a': round(t[s] - pad_a, 2), 'b': None, 'i': s, 'j': n})
    # MIDDLE: marker-bearing unmatched runs between head and tail
    lo = cuts[0]['j'] if cuts and cuts[0]['type'] == 'head' else 0
    hi = cuts[-1]['i'] if cuts and cuts[-1]['type'] == 'tail' else n
    i = lo
    while i < hi:
        if m[i]: i += 1; continue
        j = i
        while j < hi and (not m[j] or (j + 1 < hi and not m[j + 1] and False)): j += 1
        # let one or two stray matches inside a run stay in it
        while j < hi and j + 2 < hi and sum(m[j:j + 3]) <= 1 and not (m[j + 1] and m[j + 2]):
            j += 1
            while j < hi and not m[j]: j += 1
        if j - i >= MID_MIN_WORDS and t[j - 1] - t[i] <= MID_MAX_S:
            i0 = i + 1 if (j - i > 1 and re.search(r'[.!?]$', words[i])) else i   # run starts inside the previous sentence
            txt = ' '.join(words[i0:j]).lower()
            if MARK.search(txt):
                cuts.append({'type': 'mid', 'a': round(t[i0] - pad_a, 2), 'b': round(t[min(j, n - 1)] - pad_b, 2), 'i': i0, 'j': j})
        i = max(j, i + 1)
    cuts.sort(key=lambda c: c['a'])

    # MERGE: a mid cut that runs right into a tail (gap <= 2s) is redundant —
    # collapse both into a single tail cut starting at the mid's 'a'.
    merged = []
    i = 0
    while i < len(cuts):
        c = cuts[i]
        if (c['type'] == 'mid' and i + 1 < len(cuts)
                and cuts[i + 1]['type'] == 'tail'
                and cuts[i + 1]['a'] - (c['b'] or 0) <= 2.0):
            tail = cuts[i + 1]
            merged.append({'type': 'tail', 'a': c['a'], 'b': None,
                           'i': c['i'], 'j': tail['j']})
            i += 2
        else:
            merged.append(c)
            i += 1
    cuts = merged

    return {'cuts': cuts, 'why': 'ok'}

def run(pack_dir, out_dir, pad_a=PAD_A, pad_b=PAD_B, resume=False):
    os.makedirs(out_dir, exist_ok=True)
    all_fns = sorted(fn for fn in os.listdir(pack_dir)
                     if fn.endswith('.json.gz') or fn.endswith('.json'))
    total = len(all_fns)
    done = skipped = 0
    rep = []
    for fn in all_fns:
        p = os.path.join(pack_dir, fn)
        d = json.loads((gzip.open(p).read() if fn.endswith('.gz') else open(p, 'rb').read()).decode())
        bid = str(d.get('bid') or re.sub(r'\D', '', fn))
        out_path = os.path.join(out_dir, 'extras-%s.json' % bid)
        if resume and os.path.exists(out_path):
            skipped += 1
            if skipped <= 3 or skipped % 100 == 0:
                print('skip %s (%d/%d)' % (bid, skipped + done, total))
            continue
        out = {'bid': bid, 'v': 1, 'tracks': {}}
        rep.append('=== %s  %s' % (bid, d.get('title') or ''))
        for k in sorted(d['tracks'], key=lambda x: int(x)):
            tr = d['tracks'][k]
            if not isinstance(tr, dict) or 'w' not in tr: continue
            r = analyse(tr, pad_a=pad_a, pad_b=pad_b)
            words = tr['w'].split(' ')
            out['tracks'][k] = [[c['a'], c['b'], c['type']] for c in r['cuts']]
            if r['cuts']:
                rep.append('# track %s  %s  (%d cuts)' % (k, r['why'], len(r['cuts'])))
            else:
                flag = '' if r['why'] == 'ok' else '  << %s' % r['why']
                rep.append('# track %s  (0 cuts)%s' % (k, flag))
            total_dur = float(tr['t'][-1]) if tr.get('t') else 0.0
            for c in r['cuts']:
                i, j = c['i'], c['j']
                rep.append('   %-4s %7.2f -> %-7s REMOVED: %s' % (c['type'], c['a'], c['b'], ' '.join(words[i:j])[:230]))
                if c['type'] != 'tail':
                    rep.append('        KEPT next: %s' % ' '.join(words[j:j + 9]))
                if c['type'] == 'head' and c['b'] is not None and c['b'] > 35.0:
                    rep.append('        !! WARNING: head cut is long (%.1f s), verify' % c['b'])
                if c['type'] == 'tail' and total_dur > 0 and c['a'] < 0.6 * total_dur:
                    rep.append('        !! WARNING: tail starts early (%.0f%% through track), verify' % (100.0 * c['a'] / total_dur))
        json.dump(out, open(out_path, 'w'), separators=(',', ':'))
        done += 1
        if done % 50 == 0 or done + skipped == total:
            print('processed %d / %d  (skipped %d)' % (done, total - skipped, skipped))
    open(os.path.join(out_dir, 'extras_report.txt'), 'w').write('\n'.join(rep))
    print('done: %d processed, %d skipped, %d report lines -> %s'
          % (done, skipped, len(rep), os.path.join(out_dir, 'extras_report.txt')))

if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('pack_dir')
    ap.add_argument('out_dir')
    ap.add_argument('--pad-a', type=float, default=PAD_A, metavar='SECS',
                    help='seconds to trim from cut start (default %(default)s)')
    ap.add_argument('--pad-b', type=float, default=PAD_B, metavar='SECS',
                    help='seconds to trim from cut end (default %(default)s)')
    ap.add_argument('--resume', action='store_true',
                    help='skip books whose extras file already exists in OUT_DIR')
    args = ap.parse_args()
    run(args.pack_dir, args.out_dir, pad_a=args.pad_a, pad_b=args.pad_b, resume=args.resume)
