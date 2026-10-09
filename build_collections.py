#!/usr/bin/env python3
"""
build_collections.py  -  builds curation.json for Wordtrail's Collections tab.

What it does
  1. Reads your catalog (catalog.json) and, if you have them, levels.json, moods.json, versions.json.
  2. Measures popularity from two free sources:
       - Internet Archive: all-time and last-30-day downloads of every LibriVox recording
       - Project Gutenberg: its "most downloaded" lists (a small bonus for books people read worldwide)
  3. Builds 40-60 shelves (Popular, Genres, Learn English, Length, Authors, Mood), each ranked by popularity,
     with duplicate recordings collapsed to the best version.
  4. Writes curation.json  ({"v":1, "new":[ids], "collections":[{id,title,blurb,group,ids}]}).

Run it on your own computer (or Colab), then upload curation.json to the Hugging Face dataset
rabiul43/wordtrail-text under data/curation.json.

  python build_collections.py --catalog catalog.json
  python build_collections.py --catalog catalog.json --levels levels.json --moods moods.json --versions versions.json
  python build_collections.py --catalog catalog.json --offline      # reuse popularity_cache.json, no internet

Only the Python standard library is needed.
"""
import argparse, json, math, os, re, sys, time, urllib.parse, urllib.request
from collections import defaultdict

HF = 'https://huggingface.co/datasets/rabiul43/wordtrail-text/resolve/main/data/'
UA = {'User-Agent': 'wordtrail-collections/1.0'}
MAX_PER_SHELF = 30
MIN_PER_SHELF = 8


# ---------------------------------------------------------------- loading
def load_json(src, required=False, what=''):
    """src is a file path or an http(s) URL. Returns None (or exits) when it cannot be read."""
    if not src:
        return None
    try:
        if re.match(r'^https?://', src):
            req = urllib.request.Request(src, headers=UA)
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode('utf-8'))
        with open(src, encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        msg = 'Could not read %s (%s): %s' % (what or src, src, e)
        if required:
            sys.exit(msg)
        print('  skipped - ' + msg)
        return None


def fetch_text(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode('utf-8', 'replace')


# ---------------------------------------------------------------- popularity
def fetch_ia_downloads(cache_path, offline):
    """{ia identifier: [all-time downloads, last-30-days downloads]} for every LibriVox item."""
    cache = load_json(cache_path) if os.path.exists(cache_path) else None
    if offline:
        if not cache:
            sys.exit('--offline needs %s from an earlier run.' % cache_path)
        return cache.get('ia', {}), cache.get('gut', {})
    out = {}
    page = 1
    print('Internet Archive: downloading counts (about 20 pages)...')
    while True:
        q = urllib.parse.urlencode([
            ('q', 'collection:librivoxaudio'), ('fl[]', 'identifier'), ('fl[]', 'downloads'), ('fl[]', 'month'),
            ('rows', 1000), ('page', page), ('output', 'json')])
        try:
            d = json.loads(fetch_text('https://archive.org/advancedsearch.php?' + q))
        except Exception as e:
            print('  page %d failed (%s)' % (page, e))
            break
        docs = (d.get('response') or {}).get('docs') or []
        if not docs:
            break
        for x in docs:
            out[x['identifier']] = [int(x.get('downloads') or 0), int(x.get('month') or 0)]
        print('  page %d: %d items so far' % (page, len(out)))
        page += 1
        time.sleep(0.5)
    if not out and cache:
        print('  using the saved copy from the last run')
        out = cache.get('ia', {})
    return out, None


def fetch_gutenberg_top(cache):
    """{gutenberg id: 0..1 bonus} from Project Gutenberg's most-downloaded lists (top 100 per period)."""
    out = {}
    try:
        html = fetch_text('https://www.gutenberg.org/browse/scores/top')
        # the page lists "Top 100 EBooks yesterday / last 7 days / last 30 days", each an <ol> of /ebooks/NNN links
        for weight, label in ((1.0, '30 days'), (0.8, '7 days'), (0.5, 'yesterday')):
            m = re.search(r'Top 100 EBooks last %s.*?<ol>(.*?)</ol>' % re.escape(label), html, re.S | re.I) if label != 'yesterday' \
                else re.search(r'Top 100 EBooks yesterday.*?<ol>(.*?)</ol>', html, re.S | re.I)
            if not m:
                continue
            ids = re.findall(r'/ebooks/(\d+)', m.group(1))
            for rank, gid in enumerate(ids):
                out[gid] = max(out.get(gid, 0), weight * (1 - rank / 100.0))
    except Exception as e:
        print('Gutenberg lists skipped (%s)' % e)
    return out or (cache or {})


# ---------------------------------------------------------------- helpers
def slug(s):
    return re.sub(r'[^a-z0-9]+', '-', s.lower()).strip('-')


def hours(sec):
    return sec / 3600.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--catalog', required=True, help='catalog.json (file path or URL)')
    ap.add_argument('--levels', default=HF + 'levels.json')
    ap.add_argument('--moods', default=HF + 'moods.json')
    ap.add_argument('--versions', default='versions.json' if os.path.exists('versions.json') else '')
    ap.add_argument('--out', default='curation.json')
    ap.add_argument('--cache', default='popularity_cache.json')
    ap.add_argument('--offline', action='store_true')
    a = ap.parse_args()

    print('Reading the catalog...')
    cat = load_json(a.catalog, required=True, what='catalog')
    books = {}
    for r in cat['b']:
        r = list(r) + [''] * (11 - len(r))
        b = dict(id=str(r[0]), title=r[1], authors=r[2] or '', last=r[3] or '', lang=(r[4] or ''), gid=str(r[5] or ''),
                 total=int(r[6] or 0), n=int(r[7] or 0), ia=r[8] or '', genres=(r[9] or '').lower())
        if b['total'] > 0 and (not b['lang'] or b['lang'].lower() == 'english'):
            books[b['id']] = b
    print('  %d English recordings' % len(books))

    levels = load_json(a.levels, what='levels.json')
    moods = load_json(a.moods, what='moods.json')
    versions = load_json(a.versions, what='versions.json') if a.versions else None

    # -- duplicates: keep the best recording of each group
    group_of, rep = {}, {}
    if versions and isinstance(versions.get('g'), list):
        for gi, g in enumerate(versions['g']):
            members = [str(i) for i in g.get('i', []) if str(i) in books]
            for i in g.get('i', []):
                group_of[str(i)] = gi
            if members:
                rep[gi] = members[0]       # versions.json lists the best recording first
    def is_best(bid):
        gi = group_of.get(bid)
        return gi is None or rep.get(gi) == bid or rep.get(gi) is None

    # -- popularity
    cache = load_json(a.cache) if os.path.exists(a.cache) else None
    ia, gut = fetch_ia_downloads(a.cache, a.offline)
    if gut is None:
        gut = fetch_gutenberg_top((cache or {}).get('gut'))
    if ia or gut:
        with open(a.cache, 'w', encoding='utf-8') as f:
            json.dump({'ia': ia, 'gut': gut}, f)

    dl, month = {}, {}
    for bid, b in books.items():
        d = ia.get(b['ia']) or [0, 0]
        dl[bid], month[bid] = d[0], d[1]
    # a group's popularity is the sum over all its recordings (people split between readers)
    gsum, gmonth = defaultdict(int), defaultdict(int)
    for bid in books:
        gi = group_of.get(bid)
        if gi is not None:
            gsum[gi] += dl[bid]; gmonth[gi] += month[bid]
    def total_dl(bid):
        gi = group_of.get(bid); return gsum[gi] if gi is not None else dl[bid]
    def total_month(bid):
        gi = group_of.get(bid); return gmonth[gi] if gi is not None else month[bid]

    top = max([math.log1p(total_dl(i)) for i in books] or [1]) or 1
    score = {}
    for bid, b in books.items():
        s = 0.8 * math.log1p(total_dl(bid)) / top
        s += 0.2 * gut.get(b['gid'], 0)
        score[bid] = s
    cands = [i for i in books if is_best(i)]
    cands.sort(key=lambda i: -score[i])
    rank = {i: k for k, i in enumerate(cands)}
    print('  %d books after collapsing duplicates' % len(cands))

    tier = {}
    if levels and isinstance(levels.get('b'), dict):
        tier = {str(k): v[0] for k, v in levels['b'].items()}
    mood_tags, mood_defs = {}, []
    if moods and isinstance(moods.get('b'), dict) and moods.get('moods'):
        mood_defs = moods['moods']
        mood_tags = {str(k): v for k, v in moods['b'].items()}

    shelves = []
    def add(group, title, blurb, ids, key=None, order=None):
        ids = list(dict.fromkeys(ids))
        if len(ids) < MIN_PER_SHELF:
            return
        ids = ids[:MAX_PER_SHELF]
        shelves.append(dict(id=key or slug(title), title=title, blurb=blurb, group=group, ids=ids))

    def pick(pred, sort=None, limit=MAX_PER_SHELF):
        pool = [i for i in cands if pred(books[i])]
        if sort:
            pool.sort(key=sort)
        return pool[:limit]

    # ---------------- Popular
    add('Popular', 'Most listened ever', 'The recordings listeners around the world return to most.', cands[:MAX_PER_SHELF], 'most-listened')
    trend = sorted([i for i in cands if total_month(i) > 0], key=lambda i: -total_month(i))
    add('Popular', 'Trending this month', 'What the world has been listening to over the last 30 days.', trend, 'trending')
    mid = cands[60:700]
    rising = sorted([i for i in mid if total_dl(i) > 0], key=lambda i: -(total_month(i) / float(total_dl(i) + 50)))
    add('Popular', 'Hidden gems', 'Well loved by those who found them, and still under the radar.', rising, 'hidden-gems')
    add('Popular', 'Classic novels', 'Long, rich stories that have lasted generations.',
        pick(lambda b: hours(b['total']) >= 5 and 'fiction' in b['genres'] or 'literature' in b['genres'] and hours(b['total']) >= 5), 'classic-novels')

    # ---------------- Genres
    GENRES = [
        ('Mystery & Crime', 'Detectives, puzzles and perfect alibis.', ['mystery', 'detective', 'crime']),
        ('Science Fiction', 'Other worlds, machines and the far future.', ['science fiction', 'sci-fi']),
        ('Romance', 'Love stories, from slow burns to grand passions.', ['romance', 'love stor']),
        ('Horror & Ghost Stories', 'Best listened to with the lights on.', ['horror', 'ghost', 'gothic', 'supernatural']),
        ('Adventure', 'Seas, deserts, mountains and daring escapes.', ['adventure', 'pirate', 'exploration']),
        ('Fantasy & Fairy Tales', 'Magic, myths and once-upon-a-times.', ['fantasy', 'fairy', 'myth', 'legend']),
        ('Poetry', 'Verse read aloud.', ['poetry', 'poem']),
        ('Philosophy', 'Big questions, patiently asked.', ['philosophy']),
        ('History & Biography', 'Real lives and real events.', ['history', 'biograph', 'memoir', 'autobiograph']),
        ("Children's Stories", 'Gentle tales for younger listeners.', ['children', 'juvenile']),
        ('Humor & Satire', 'Books that make you laugh out loud.', ['humor', 'satire', 'comedy', 'comic']),
        ('Plays & Drama', 'Dramatic readings with many voices.', ['drama', 'play', 'theatre', 'theater']),
        ('Short Stories', 'Complete stories in one sitting.', ['short stor']),
        ('Religion & Spirituality', 'Sacred texts and reflection.', ['religion', 'spiritual', 'bible', 'theolog']),
        ('Nature & Science', 'The natural world, explained.', ['nature', 'natural history', 'science']),
        ('War & Military', 'Battles, soldiers and the home front.', ['war', 'military']),
        ('Westerns', 'Frontier towns and wide-open plains.', ['western']),
    ]
    for title, blurb, kws in GENRES:
        add('Genres', title, blurb, pick(lambda b, k=kws: any(w in b['genres'] for w in k)))

    # ---------------- Learn English (needs levels.json)
    if tier:
        add('Learn English', 'Easy stories (A2-B1)', 'Clear language and gentle pace. A good place to start.',
            pick(lambda b: tier.get(b['id']) == 0 and hours(b['total']) <= 8), 'easy-stories')
        add('Learn English', 'Short and easy', 'Easy listening, under two hours.',
            pick(lambda b: tier.get(b['id']) == 0 and hours(b['total']) <= 2), 'short-and-easy')
        add('Learn English', 'Medium reads (B2)', 'Fuller vocabulary and longer sentences.',
            pick(lambda b: tier.get(b['id']) == 1), 'medium-reads')
        add('Learn English', 'Challenge yourself (C1-C2)', 'Rich, demanding prose for confident listeners.',
            pick(lambda b: tier.get(b['id']) == 2), 'challenge')
        add('Learn English', "Easy children's classics", 'Simple language, familiar stories.',
            pick(lambda b: tier.get(b['id']) == 0 and any(w in b['genres'] for w in ('children', 'juvenile', 'fairy'))), 'easy-children')

    # ---------------- By length
    add('By length', 'Quick listens', 'Under one hour. Finish one on the way to work.', pick(lambda b: b['total'] < 3600), 'under-1h')
    add('By length', 'A coffee break', 'One to two hours.', pick(lambda b: 3600 <= b['total'] < 7200), '1-2h')
    add('By length', 'A weekend listen', 'Three to eight hours: a proper story without a huge commitment.',
        pick(lambda b: 3 * 3600 <= b['total'] < 8 * 3600), 'weekend')
    add('By length', 'Long journeys', 'Twelve hours or more, for road trips and long winters.',
        pick(lambda b: hours(b['total']) >= 12), 'long-journeys')

    # ---------------- Authors: the most listened-to authors with enough books
    by_author = defaultdict(list)
    for i in cands:
        name = books[i]['authors'].split(',')[0].split(';')[0].split(' and ')[0].strip()
        if name and not name.lower().startswith('unknown') and 'various' not in name.lower() and 'anonymous' not in name.lower():
            by_author[name].append(i)
    ranked = sorted(((sum(score[i] for i in ids[:10]), name, ids) for name, ids in by_author.items() if len(ids) >= 5), reverse=True)
    for _, name, ids in ranked[:14]:
        add('Authors', name, 'The best-loved recordings of %s.' % name, ids, 'author-' + slug(name))

    # ---------------- Mood (needs moods.json: moods = [[key, label?...], ...], b = {id: [mood index, ...]})
    for mi, m in enumerate(mood_defs):
        key = str(m[0]) if isinstance(m, (list, tuple)) else str(m)
        label = (m[1] if isinstance(m, (list, tuple)) and len(m) > 1 and isinstance(m[1], str) else key).strip().capitalize()
        add('Mood', label + ' reads', 'Books that feel %s.' % label.lower(),
            pick(lambda b, mi=mi: mi in (mood_tags.get(b['id']) or [])), 'mood-' + slug(key))

    # ---------------- New this week (newest recordings, English)
    new_ids = sorted(books, key=lambda i: -int(i) if i.isdigit() else 0)[:30]

    out = dict(v=1, new=new_ids, collections=shelves)
    with open(a.out, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, separators=(',', ':'))
    print('\nWrote %s: %d shelves, %d new books' % (a.out, len(shelves), len(new_ids)))
    groups = defaultdict(int)
    for s in shelves:
        groups[s['group']] += 1
    for g, n in groups.items():
        print('  %-14s %d shelves' % (g, n))
    print('\nNext: upload %s to the Hugging Face dataset rabiul43/wordtrail-text as data/%s' % (a.out, os.path.basename(a.out)))


if __name__ == '__main__':
    main()
