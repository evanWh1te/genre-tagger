"""lookup: fetch and cache raw source data per artist.

Sources run in parallel threads, each at its own rate limit. Discogs and
Last.fm wait for an artist's MusicBrainz step, because that decides who the
artist is (and usually gives the Discogs ID directly).

Identity rules, so nothing is guessed:
  - artists with an MBID (from tags, a pin, or an exact unique MusicBrainz
    name match) are looked up by that identity everywhere
  - a name with several exact MusicBrainz matches is ambiguous: no source is
    queried and the artist goes to the review file until it is pinned
  - a name MusicBrainz does not know is still tried on Discogs (exact unique
    name match) and Last.fm; resolve only accepts it with a Discogs match
Every response, including "not found", is cached; a re-run makes no calls
for anything already cached.
"""
import collections
import json
import os
import sys
import threading
import time

from . import db as dbm
from . import tags
from .sources import Discogs, LastFM, MusicBrainz, SourceError


def cached(db, key, source):
    r = db.execute("SELECT status, payload FROM cache WHERE artist_key=? AND source=?", (key, source)).fetchone()
    return (r[0], json.loads(r[1]) if r[1] else None) if r else None


def store(db, key, source, status, payload):
    db.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?,?,?)",
               (key, source, status, json.dumps(payload) if payload is not None else None, dbm.now()))
    db.commit()


def pinned_mbid(db, key):
    r = db.execute("SELECT mbid FROM overrides WHERE artist_key=?", (key,)).fetchone()
    return r[0] if r and r[0] else None


def evidence_for(db, settings, key, n=3):
    """(album titles, track titles) for an artist, most common albums first.
    Read from up to 10 of its files; only called for ambiguous names."""
    rows = db.execute("SELECT relpath FROM tracks WHERE artist_key=? ORDER BY relpath", (key,)).fetchall()
    root = os.fsencode(str(settings.library)).rstrip(b'/')
    albums, titles = collections.Counter(), []
    for (rel,) in rows[:10]:
        try:
            t = tags.read(root + b'/' + rel)
        except Exception:
            continue
        if t.get('album'):
            albums[t['album'][0]] += 1
        if t.get('title') and t['title'][0] not in titles:
            titles.append(t['title'][0])
    return [a for a, _ in albums.most_common(n)], titles[:n]


def identity(db, a):
    """('mbid', id) | ('unknown', None) | ('ambiguous', candidates) | ('pending', None)"""
    mbid = pinned_mbid(db, a['key']) or a['mbid']
    if mbid:
        return 'mbid', mbid
    s = cached(db, a['key'], 'mb_search')
    if s is None:
        return 'pending', None
    if s[0] == 'ok':
        return 'mbid', s[1]['mbid']
    if s[0] == 'ambiguous':
        return 'ambiguous', s[1]['candidates']
    return 'unknown', None


def mb_payload(db, a):
    kind, mbid = identity(db, a)
    if kind != 'mbid':
        return None
    c = cached(db, a['key'], 'musicbrainz')
    return c[1] if c and c[0] == 'ok' else None


def run(settings, artists, sources=('musicbrainz', 'discogs', 'lastfm')):
    ua = settings.user_agent
    if 'discogs' in sources and not settings.discogs_token:
        print('DISCOGS_TOKEN not set: skipping Discogs (nothing cached, a later run will fetch it)')
        sources = [s for s in sources if s != 'discogs']
    if 'lastfm' in sources and not settings.lastfm_key:
        print('LASTFM_API_KEY not set: skipping Last.fm (nothing cached, a later run will fetch it)')
        sources = [s for s in sources if s != 'lastfm']
    if not settings.contact:
        print('note: GT_CONTACT not set; MusicBrainz asks for contact info in the User-Agent')

    mb_done = threading.Event()
    counts = {s: collections.Counter() for s in sources}
    errors = collections.Counter()

    def mb_ready(db, a):
        kind, mbid = identity(db, a)
        if kind == 'pending':
            return False
        return kind != 'mbid' or cached(db, a['key'], 'musicbrainz') is not None

    def mb_step(db, a):
        calls = 0
        if pinned_mbid(db, a['key']) is None and a['mbid'] is None:
            s = cached(db, a['key'], 'mb_search')
            if s is None:
                s = mb.search(a['name'])
                store(db, a['key'], 'mb_search', *s)
                calls += 1
            if s[0] == 'ambiguous' and 'titles_tried' not in s[1]:
                albums, titles = evidence_for(db, settings, a['key'])
                store(db, a['key'], 'mb_search', *mb.disambiguate(a['name'], s[1]['candidates'], albums, titles))
                calls += 1
        kind, mbid = identity(db, a)
        if kind == 'mbid' and cached(db, a['key'], 'musicbrainz') is None:
            store(db, a['key'], 'musicbrainz', *mb.artist(mbid))
            calls += 1
        return kind if calls else 'cached'

    def discogs_step(db, a):
        kind, _ = identity(db, a)
        if kind == 'ambiguous':
            return 'skipped-ambiguous'
        payload = mb_payload(db, a)
        name = (payload or {}).get('name') or a['name']
        store(db, a['key'], 'discogs', *dg.lookup(payload, name, allow_search=True))
        return 'fetched'

    def lastfm_step(db, a):
        kind, mbid = identity(db, a)
        if kind == 'ambiguous':
            return 'skipped-ambiguous'
        status, data = lf.top_tags(mbid=mbid) if kind == 'mbid' else ('notfound', None)
        if status == 'notfound':
            name = (mb_payload(db, a) or {}).get('name') or a['name']
            status, data = lf.top_tags(name=name)
        store(db, a['key'], 'lastfm', status, data)
        return 'fetched'

    def worker(source, step, ready, cache_source):
        db = dbm.connect(settings.db)
        pending = list(artists)
        done = 0
        while pending:
            progressed = False
            for a in list(pending):
                if cache_source and cached(db, a['key'], cache_source) is not None:
                    pending.remove(a)
                    counts[source]['cached'] += 1
                    continue
                if ready and not ready(db, a):
                    continue
                try:
                    counts[source][step(db, a)] += 1
                except SourceError as e:
                    errors[source] += 1
                    print(f'[{source}] {a["name"]}: {e}', file=sys.stderr, flush=True)
                pending.remove(a)
                progressed = True
                done += 1
                if done % 25 == 0:
                    print(f'[{source}] {done} done, {len(pending)} left', flush=True)
            if not progressed and pending:
                if source == 'musicbrainz' or mb_done.is_set():
                    break  # whatever is left never got a MusicBrainz answer (errors)
                time.sleep(2)
        if source == 'musicbrainz':
            mb_done.set()

    mb = MusicBrainz(ua)
    dg = Discogs(settings.discogs_token, ua) if 'discogs' in sources else None
    lf = LastFM(settings.lastfm_key, ua) if 'lastfm' in sources else None

    plan = []
    if 'musicbrainz' in sources:
        plan.append(('musicbrainz', mb_step, None, None))
    else:
        mb_done.set()
    if dg:
        plan.append(('discogs', discogs_step, mb_ready, 'discogs'))
    if lf:
        plan.append(('lastfm', lastfm_step, mb_ready, 'lastfm'))

    print(f'looking up {len(artists)} artists: {", ".join(p[0] for p in plan)}', flush=True)
    threads = [threading.Thread(target=worker, args=p, daemon=True) for p in plan]
    for t in threads:
        t.start()
    for t in threads:
        while t.is_alive():
            t.join(1)
    for s in counts:
        print(f'{s}: {dict(counts[s])}' + (f', errors (not cached, retried next run): {errors[s]}' if errors[s] else ''))
