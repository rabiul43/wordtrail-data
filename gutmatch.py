"""Fills in the missing Project Gutenberg book number (column 5 of each catalog row) by matching
the audiobook's title and author against Gutenberg's own list (pg_catalog.csv).

Used by build_catalog.py; can also be run on its own:
    python gutmatch.py                 fills catalog.json in place and prints a report
    python gutmatch.py --dry           only prints the report

Rules (careful on purpose: a missing number only costs a search, a WRONG number loads the wrong book):
  * only rows with NO number yet are touched; numbers that are already there are never changed
  * the title must match after cleaning (LibriVox labels like "(Dramatic Reading)" and "(version 2)" are dropped,
    "The/A/An" and punctuation are ignored, a subtitle after ":" or ";" is ignored on either side)
  * the author's surname must match too; "Various" / unknown authors are skipped
  * the language must match (English audiobook -> English text)
  * if more than one Gutenberg book fits equally well, it is NOT guessed (AMBIGUOUS=lowest in the environment
    takes the lowest number instead)
"""
import csv, io, json, os, re, sys, unicodedata, urllib.request

PG_URL = 'https://www.gutenberg.org/cache/epub/feeds/pg_catalog.csv'
PG_FILE = os.environ.get('PG_CSV') or 'pg_catalog.csv'
LANG = {'english': 'en', 'french': 'fr', 'german': 'de', 'spanish': 'es', 'italian': 'it', 'dutch': 'nl', 'portuguese': 'pt',
        'latin': 'la', 'finnish': 'fi', 'swedish': 'sv', 'danish': 'da', 'polish': 'pl', 'greek': 'el', 'russian': 'ru',
        'hungarian': 'hu', 'chinese': 'zh', 'esperanto': 'eo', 'catalan': 'ca', 'japanese': 'ja', 'tagalog': 'tl'}
SKIP_AUTHORS = {'', 'various', 'anonymous', 'unknown', 'traditional', 'anon'}

def ascii_(s):
    return unicodedata.normalize('NFKD', str(s or '')).encode('ascii', 'ignore').decode()

KEEP_TAG = re.compile(r'\b(vol(ume)?s?|part|parts|book|books|bk|no|number|tome|band|chapter|canto|act)\b|\d', re.I)

def drop_labels(t):
    """drops trailing LibriVox labels such as (Dramatic Reading), (version 2), [abridged]; keeps (Volume 1), (Part 2) ..."""
    t = str(t or '').strip()
    while True:
        m = re.search(r'\s*[(\[]([^()\[\]]*)[)\]]\s*$', t)
        if not m: return t
        tag = m.group(1)
        if KEEP_TAG.search(tag) and not re.fullmatch(r'\s*(version|ver\.?|v\.?)\s*\d+\s*', tag, re.I): return t
        n = t[:m.start()].strip()
        if not n: return t
        t = n

def norm_title(t):
    t = ascii_(drop_labels(t)).lower().replace('&', ' and ')
    t = re.sub(r"['’`]", '', t)
    t = re.sub(r'[^a-z0-9]+', ' ', t).strip()
    return re.sub(r'^(the|a|an)\s+', '', t)

def title_parts(t):
    """(full cleaned title, title before a subtitle ': ...' / '; or, ...' - or None when there is no subtitle)"""
    full = norm_title(t)
    main = norm_title(re.split(r'\s*[:;\n]\s*|\s+--\s+', drop_labels(t), maxsplit=1)[0])
    return full, (main if main != full and len(main) >= 3 else None)

def norm_name(s):
    return re.sub(r'[^a-z]', '', ascii_(s).lower())

def surnames(authors):
    """'Austen, Jane, 1775-1817; Doyle, Arthur Conan, 1859-1930' -> {'austen', 'doyle'}"""
    out = set()
    for a in re.split(r'\s*;\s*|\s+and\s+(?=[A-Z])', str(authors or '')):
        n = norm_name(a.split(',')[0])
        if n: out.add(n)
    return out

def surname_ok(last, pg_surnames):
    n = norm_name(last)
    if not n or n in SKIP_AUTHORS: return False
    return any(n == s or (len(n) >= 4 and len(s) >= 4 and (s.endswith(n) or n.endswith(s))) for s in pg_surnames)   # "de Maupassant" / "Maupassant"

def load_pg(path=None, download=True):
    """Gutenberg's list as {title key: [(number, surnames, languages, 'F' full title | 'M' before-subtitle)]} (texts only)"""
    path = path or PG_FILE
    if not os.path.exists(path):
        if not download: raise FileNotFoundError(path)
        print('downloading', PG_URL, flush=True)
        req = urllib.request.Request(PG_URL, headers={'User-Agent': 'wordtrail-catalog/1.0'})
        with urllib.request.urlopen(req, timeout=120) as r, open(path, 'wb') as f: f.write(r.read())
    idx = {}
    with open(path, encoding='utf-8-sig', newline='') as f:
        for r in csv.DictReader(f):
            if (r.get('Type') or '').strip() != 'Text': continue
            try: num = int(r['Text#'])
            except Exception: continue
            langs = set(re.split(r'[;,\s]+', (r.get('Language') or '').lower())) - {''}
            ent = (num, surnames(r.get('Authors')), langs)
            full, main = title_parts(r.get('Title'))
            if len(full) >= 3: idx.setdefault(full, []).append(ent + ('F',))
            if main: idx.setdefault(main, []).append(ent + ('M',))
    return idx

def find(idx, title, last, lang):
    """-> ('ok', number) | ('ambiguous', [numbers]) | ('none', None) | ('skip', None)"""
    code = LANG.get(str(lang or '').strip().lower())
    if not code or norm_name(last) in SKIP_AUTHORS: return ('skip', None)
    full, main = title_parts(title)
    found = {}   # number -> True when the whole titles are equal
    def look(key, kinds):
        for num, sn, langs, kind in idx.get(key, ()):
            if kind in kinds and code in langs and surname_ok(last, sn):
                found[num] = found.get(num, False) or (key == full and kind == 'F')
    if len(full) >= 3: look(full, 'FM')       # whole title <-> whole title, or <-> the main part of a Gutenberg title with subtitle
    if main: look(main, 'F')                  # our main part <-> a whole Gutenberg title. (main <-> main is NOT allowed: "Poems: Third Series" is not "Poems: First Series")
    if not found: return ('none', None)
    exact = sorted(n for n, e in found.items() if e)   # equal whole titles beat the looser matches
    nums = exact or sorted(found)
    if len(nums) == 1: return ('ok', nums[0])
    if os.environ.get('AMBIGUOUS') == 'lowest': return ('ok', nums[0])
    return ('ambiguous', nums)

def fill(rows, idx=None, quiet=False):
    """rows: dict or list of catalog rows [id, title, authors, last, lang, gid, ...]. Fills r[5] in place. Returns counts."""
    idx = idx if idx is not None else load_pg()
    rs = rows.values() if isinstance(rows, dict) else rows
    c = {'had': 0, 'filled': 0, 'ambiguous': 0, 'none': 0, 'skip': 0}
    amb = []
    for r in rs:
        if r[5]: c['had'] += 1; continue
        st, v = find(idx, r[1], r[3], r[4])
        if st == 'ok': r[5] = str(v); c['filled'] += 1
        else:
            c[st] += 1
            if st == 'ambiguous' and len(amb) < 15: amb.append((r[0], r[1], r[3], v[:6]))
    if not quiet:
        print('gutmatch: already had %(had)d, filled %(filled)d, ambiguous (left empty) %(ambiguous)d, no match %(none)d, skipped (language/author) %(skip)d' % c, flush=True)
        for a in amb: print('   ambiguous:', a)
    return c

if __name__ == '__main__':
    d = json.load(open('catalog.json', encoding='utf-8'))
    c = fill(d['b'])
    if '--dry' not in sys.argv and c['filled']:
        with open('catalog.json', 'w', encoding='utf-8') as f: f.write(json.dumps(d, ensure_ascii=False, separators=(',', ':'), sort_keys=True))
        print('catalog.json updated')
