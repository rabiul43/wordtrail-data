#!/usr/bin/env python3
import json, re, time, gzip, os, html, urllib.request, urllib.parse, urllib.error, sys
from concurrent.futures import ThreadPoolExecutor

API = 'https://librivox.org/api/feed/audiobooks'
PROXY = 'https://wordtrail-proxy.test-rabiulclaud-1.workers.dev/?url='
PAGE, WORKERS = 100, 4
BUDGET = int(os.environ.get('BUDGET') or 40 * 60)
MAXROUNDS = int(os.environ.get('MAXROUNDS') or 10**9)
HEADERS = {'User-Agent': 'Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/124 Mobile Safari/537.36',
           'Accept': 'application/json'}
FIELDS = ('id,title,description,language,copyright_year,num_sections,totaltimesecs,authors,translators,genres,'
          'url_iarchive,url_text_source,url_librivox,url_rss,url_zip_file,url_project,url_other')
T0 = time.time()

def fetch(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.load(r).get('books', [])

def get(offset, tries=3):
    q = urllib.parse.urlencode({'format': 'json', 'extended': 1, 'fields': '{' + FIELDS + '}', 'limit': PAGE, 'offset': offset})
    url = API + '?' + q
    for t in range(tries):
        for u in (url, PROXY + urllib.parse.quote(url, safe='')):
            try:
                return fetch(u)
            except urllib.error.HTTPError as e:
                if e.code == 404: return []
            except Exception:
                pass
        time.sleep(2 * (t + 1))
    return None

def person(a):
    return ' '.join(x for x in (a.get('first_name'), a.get('last_name')) if x)

def lean(b):
    authors = b.get('authors') or []
    names = ', '.join(person(a) for a in authors if person(a)) or 'Unknown author'
    last = (authors[0].get('last_name') if authors else '') or ''
    ia = re.search(r'details/([^/?#]+)', str(b.get('url_iarchive') or ''))
    text = str(b.get('url_text_source') or '')
    g = re.search(r'gutenberg\.org/(?:ebooks|etext|files|cache/epub)/(\d+)', text)
    genres = '|'.join(x.get('name', '') for x in (b.get('genres') or []))
    return [int(b['id']), (b.get('title') or '').strip(), names, last, b.get('language') or '',
            g.group(1) if g else '', int(b.get('totaltimesecs') or 0), int(b.get('num_sections') or 0),
            ia.group(1) if ia else '', genres, text if 'gutenberg' in text else '']

def detail(b):
    desc = html.unescape(re.sub(r'<[^>]+>', ' ', str(b.get('description') or '')))
    d = {'d': ' '.join(desc.split()), 'y': b.get('copyright_year') or '',
         'tr': ', '.join(person(a) for a in (b.get('translators') or []) if person(a)),
         'a': [[a.get('id'), person(a), a.get('dob') or '', a.get('dod') or ''] for a in (b.get('authors') or [])],
         'l': b.get('url_librivox') or '', 'r': b.get('url_rss') or '', 'z': b.get('url_zip_file') or '',
         'p': b.get('url_project') or '', 'o': b.get('url_other') or '', 's': b.get('url_text_source') or ''}
    return {k: v for k, v in d.items() if v not in ('', None, [])}

def load(path, default):
    try:
        with open(path, encoding='utf-8') as f: return json.load(f)
    except Exception: return default

dump = lambda o: json.dumps(o, ensure_ascii=False, separators=(',', ':'), sort_keys=True)

old = load('catalog.json', {})
START = int(os.environ.get('START') or 0) or int(old.get('next') or 0)
rows = {r[0]: r for r in old.get('b', [])}
print('starting at offset', START, 'with', len(rows), 'books already saved', flush=True)

raw, seen, offset, done, rounds = [], set(), START, False, 0
first_fail = None
with ThreadPoolExecutor(WORKERS) as ex:
    while not done and time.time() - T0 < BUDGET and rounds < MAXROUNDS:
        offsets = [offset + i * PAGE for i in range(WORKERS)]
        for off, books in zip(offsets, ex.map(get, offsets)):
            if books is None:
                if first_fail is None: first_fail = off
                continue
            if not books: done = True; continue
            for b in books:
                if b.get('id') and b['id'] not in seen:
                    seen.add(b['id']); raw.append(b)
        offset += WORKERS * PAGE; rounds += 1
        print('fetched', len(raw), 'new books, offset', offset, 'failed pages', 0 if first_fail is None else 1, flush=True)

nxt = first_fail if first_fail is not None else (None if done else offset)
if nxt is not None and nxt <= START: nxt = None
more = nxt is not None

if not raw and not rows:
    sys.exit('Nothing fetched. Not saving.')

raw.sort(key=lambda b: int(b['id']))
for b in raw: rows[int(b['id'])] = lean(b)
if len(rows) < 100: sys.exit('Too few books (%d). Not saving.' % len(rows))

cat = {'v': 1, 'n': len(rows), 'b': sorted(rows.values(), key=lambda r: -r[0])}
if more: cat['next'] = nxt
with open('catalog.json', 'w', encoding='utf-8') as f: f.write(dump(cat))

os.makedirs('details', exist_ok=True)
chunks = {}
for b in raw: chunks.setdefault(int(b['id']) // 1000, {})[b['id']] = detail(b)
for n, d in chunks.items():
    p = 'details/%d.json' % n
    merged = load(p, {}).get('b', {}); merged.update(d)
    with open(p, 'w', encoding='utf-8') as f: f.write(dump({'v': 1, 'b': merged}))

with gzip.open('raw-%d.json.gz' % START, 'wt', encoding='utf-8') as f:
    f.write(dump({'t': int(time.time()), 'start': START, 'books': raw}))

if os.environ.get('GITHUB_OUTPUT'):
    with open(os.environ['GITHUB_OUTPUT'], 'a') as f: f.write('more=%s\n' % ('true' if more else 'false'))
print('saved', len(rows), 'books in total;', len(raw), 'new this run;', 'continuing at %d' % nxt if more else 'COMPLETE')
