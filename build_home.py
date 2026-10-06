#!/usr/bin/env python3
import json, os, re, sys, hashlib, datetime, urllib.request, urllib.parse

HEADERS = {'User-Agent': 'Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/124 Mobile Safari/537.36'}

def jget(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)

def load(path, default):
    try:
        with open(path, encoding='utf-8') as f: return json.load(f)
    except Exception: return default

def archive_ranking(field):
    q = urllib.parse.urlencode({'q': 'collection:librivoxaudio', 'fl[]': ['identifier', field],
                                'sort[]': field + ' desc', 'rows': 600, 'page': 1, 'output': 'json'}, doseq=True)
    docs = jget('https://archive.org/advancedsearch.php?' + q)['response']['docs']
    return [d['identifier'] for d in docs if d.get('identifier') and int(d.get(field) or 0) > 0]

cat = load('catalog.json', None)
if not cat or not cat.get('b'): sys.exit('catalog.json is missing or empty')
rows = {r[0]: r for r in cat['b']}
by_ia = {r[8]: r[0] for r in cat['b'] if r[8]}
old = load('home.json', {})

def to_ids(idents):
    out, seen = [], set()
    for i in idents:
        b = by_ia.get(i)
        if b and b not in seen: seen.add(b); out.append(b)
    return out

try:
    trending = to_ids(archive_ranking('week'))[:60]
    pool = to_ids(archive_ranking('month'))[:500]
    print('trending from Internet Archive:', len(trending), 'popular pool:', len(pool))
except Exception as e:
    print('Internet Archive failed (%s). Keeping yesterday\'s trending.' % e)
    trending, pool = old.get('trending', []), []

ok = lambda i: i in rows and rows[i][4].lower() == 'english' and rows[i][5] and 3600 <= rows[i][6] <= 15 * 3600
cands = [i for i in pool if ok(i)] or [i for i in sorted(rows) if ok(i)]
hist = [i for i in old.get('hist', []) if i in rows][-30:]
today = datetime.date.today().isoformat()
if old.get('date') == today and old.get('botd'):
    botd_id = old['botd']['id']
else:
    fresh = [i for i in cands if i not in hist] or cands
    botd_id = fresh[int(hashlib.sha256(today.encode()).hexdigest(), 16) % len(fresh)]
    hist = (hist + [botd_id])[-30:]

blurb = ''
d = load('details/%d.json' % (botd_id // 1000), {}).get('b', {}).get(str(botd_id), {})
text = d.get('d', '')
if text:
    blurb = text[:220]
    if len(text) > 220: blurb = blurb.rsplit(' ', 1)[0].rstrip('.,;: ') + '…'

out = {'v': 1, 'date': today, 'trending': trending, 'botd': {'id': botd_id, 'blurb': blurb}, 'hist': hist}
with open('home.json', 'w', encoding='utf-8') as f:
    json.dump(out, f, ensure_ascii=False, separators=(',', ':'))
print('home.json: %d trending, book of the day = %s (%s)' % (len(trending), botd_id, rows[botd_id][1]))
