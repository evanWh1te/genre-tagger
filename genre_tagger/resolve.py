"""resolve: merge cached source data into a genre list per artist.

score(genre) = sum over sources of weight * strength, where strength is in
0..1 and relative within the source:
  MusicBrainz genres  3.0 x votes / max votes      (curated genre list)
  MusicBrainz tags    0.5 x votes / max votes      (uncurated: can't pass on their own)
  Discogs styles      2.0 x share of releases with the style
  Discogs genres      0.5 x share of releases with the genre  (15 broad buckets, metal is
                                                               filed under "Rock": corroboration only)
  Last.fm tags        2.0 x count / 100, only count >= GT_LASTFM_MIN_COUNT
  existing tags       1.0 x share of the artist's tracks with the genre
Genres scoring >= GT_MIN_SCORE are kept, best first, up to GT_TOP_N.
Existing tags alone never produce a proposal: at least one external source
must contribute.
"""
import collections
import csv
import json
import sqlite3
import time
from pathlib import Path

from . import hierarchy
from .db import loads, now
from .hierarchy import Ambiguous
from .lookup import cached, identity
from .normalize import Normalizer, key
from .scan import split_genres

W = {'mb_genre': 3.0, 'mb_tag': 0.5, 'dg_style': 2.0, 'dg_genre': 0.5, 'lastfm': 2.0, 'existing': 1.0}
FORBIDDEN = set(';/,')


def vocabulary(db, vocab_db=None):
    """Genre spellings in use, with track counts: this DB's, plus another
    state DB's (the library's, when tagging an inbox headed into it)."""
    counts = collections.Counter()
    dbs = [db] + ([sqlite3.connect(f'file:{vocab_db}?mode=ro', uri=True)] if vocab_db else [])
    for d in dbs:
        for (g,) in d.execute("SELECT genre FROM tracks WHERE genre IS NOT NULL"):
            for v in loads(g):
                counts[v] += 1
    return counts.items()


def mb_genre_names(db):
    names = set()
    for (p,) in db.execute("SELECT payload FROM cache WHERE source='musicbrainz' AND status='ok'"):
        names.update(g['name'] for g in (loads(p) or {}).get('genres') or [])
    return sorted(names)


def track_genres(db):
    """artist_key -> (n tracks, Counter of genre -> n tracks having it,
    [(current genre list, written by this tool)]).
    The Counter (the 'existing' signal) uses a track's original genre when this
    tool wrote it, so earlier output never feeds back in as evidence."""
    out = collections.defaultdict(lambda: [0, collections.Counter(), []])
    for key, g, orig, wrote in db.execute(
            """SELECT t.artist_key, t.genre, o.genre, w.relpath FROM tracks t
               LEFT JOIN writes w ON w.relpath = t.relpath
               LEFT JOIN originals o ON o.relpath = t.relpath
               WHERE t.artist_key IS NOT NULL"""):
        e = out[key]
        e[0] += 1
        e[2].append((loads(g), wrote is not None))
        for x in split_genres(loads(orig if wrote is not None else g)):
            e[1][x] += 1
    return out


def score_artist(db, a, norm, settings, existing):
    scores = collections.Counter()
    contrib = collections.Counter()
    dropped = collections.Counter()   # genres outside the hierarchy -> score they would have had
    ambiguous = []                    # (Ambiguous, score, provider) settled once the rest is known
    providers = collections.defaultdict(set)  # genre -> {'mb', 'dg', 'lastfm', 'existing'}
    name = a['name']

    def add(source, raw, strength):
        miss = []
        provider = source.split('_')[0]
        for g in norm.canon(raw, name, miss):
            if isinstance(g, Ambiguous):
                ambiguous.append((g, W[source] * strength, provider))
            else:
                scores[g] += W[source] * strength
                providers[g].add(provider)
            contrib[provider] += 1
        for g in miss:
            dropped[g] += W[source] * strength

    mb = cached(db, a['key'], 'musicbrainz')
    if mb and mb[0] == 'ok':
        genres = mb[1].get('genres') or []
        gnames = {g['name'] for g in genres}
        top = max([g.get('count', 0) for g in genres] + [1])
        for g in genres:
            add('mb_genre', g['name'], max(g.get('count', 0), 1) / top)
        tags = [t for t in mb[1].get('tags') or [] if t['name'] not in gnames]
        top = max([t.get('count', 0) for t in tags] + [1])
        for t in tags:
            if t.get('count', 0) > 0:
                add('mb_tag', t['name'], t['count'] / top)

    dg = cached(db, a['key'], 'discogs')
    if dg and dg[0] == 'ok' and dg[1].get('items'):
        items = dg[1]['items']
        styles, genres = collections.Counter(), collections.Counter()
        for it in items:
            styles.update(set(it.get('style') or []))
            genres.update(set(it.get('genre') or []))
        for s, n in styles.items():
            add('dg_style', s, n / len(items))
        for g, n in genres.items():
            add('dg_genre', g, n / len(items))

    lf = cached(db, a['key'], 'lastfm')
    if lf and lf[0] == 'ok':
        for t in lf[1]['tags']:
            if t['count'] >= settings.lastfm_min_count:
                add('lastfm', t['name'], t['count'] / 100)

    external = sum(contrib.values())
    n_tracks, counter, _ = existing
    for g, n in counter.items():
        add('existing', g, n / max(n_tracks, 1))
    # A bare ambiguous tag ("hardcore") counts only when a second provider
    # agrees: another one sent the same bare tag, or named the variant outright.
    # Last.fm alone tags plenty of metalcore bands "hardcore".
    bare_from = collections.defaultdict(set)
    for amb, s, provider in ambiguous:
        bare_from[key(amb)].add(provider)
    settled = collections.Counter()
    for amb, s, provider in ambiguous:
        pick = norm.hierarchy.settle(amb, scores)
        if not pick:
            dropped[f'{amb} (ambiguous)'] += s
        elif len(bare_from[key(amb)] | providers[pick]) < 2:
            dropped[f'{amb} (one source only)'] += s
        else:
            settled[pick] += s
    scores.update(settled)
    return scores, contrib, external, dg, dropped


def resolve(db, settings, artists):
    norm = Normalizer(vocabulary(db, settings.vocab_db), mb_genre_names(db), hierarchy.load(settings.hierarchy))
    existing = track_genres(db)
    overrides = {k: (loads(g), note) for k, g, note in
                 db.execute("SELECT artist_key, genres, note FROM overrides WHERE genres IS NOT NULL")}
    results = []
    for a in artists:
        ex = existing.get(a['key'], (0, collections.Counter(), []))
        scores, contrib, external, dg, dropped = score_artist(db, a, norm, settings, ex)
        kind, info = identity(db, a)
        genres, status, reason = [], 'review', ''
        if a['key'] in overrides:
            genres, status, reason = overrides[a['key']][0], 'override', overrides[a['key']][1] or 'manual override'
        elif kind == 'pending':
            reason = 'not looked up yet (run lookup)'
        elif kind == 'ambiguous':
            reason = 'ambiguous MusicBrainz name: ' + ' | '.join(
                f"{c['id']} {c['name']}" + (f" ({c['disambiguation']})" if c.get('disambiguation') else '')
                + (f" [{c['country']}]" if c.get('country') else '') for c in info)
        elif kind == 'unknown' and not (dg and dg[0] == 'ok' and dg[1].get('via') == 'search'):
            reason = 'no MusicBrainz match and no unique exact Discogs match'
        elif kind == 'mbid' and (cached(db, a['key'], 'musicbrainz') or ('',))[0] == 'notfound':
            reason = f'MBID {info} not found on MusicBrainz (stale tag?)'
        elif external == 0:
            reason = 'no genre data from any source'
        else:
            ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
            genres = [g for g, s in ranked if s >= settings.min_score][:settings.top_n]
            if genres:
                status = 'ok'
            else:
                reason = 'no genre scored >= %.2f; best: %s' % (
                    settings.min_score, ', '.join(f'{g} {s:.2f}' for g, s in ranked[:3]))
        bad = [g for g in genres if FORBIDDEN & set(g)]
        if bad:
            genres, status, reason = [], 'review', f'separator in genre name {bad}'
        # what a default-scope apply would rewrite: 0-1 genre tracks and this tool's own writes
        n_change = sum(1 for g, wrote in ex[2]
                       if g != genres and (wrote or len(split_genres(g)) <= 1)) if genres else 0
        results.append({**a, 'genres': genres, 'scores': dict(scores), 'status': status, 'reason': reason,
                        'contrib': dict(contrib), 'n_change': n_change,
                        'dropped': {g: s for g, s in dropped.items() if s >= settings.min_score}})

    db.execute("DELETE FROM resolved")
    db.executemany("INSERT INTO resolved VALUES (?,?,?,?,?,?)",
                   [(r['key'], json.dumps(r['genres']), json.dumps(r['scores']), r['status'], r['reason'], now())
                    for r in results])
    db.commit()
    return results


def write_reports(db, results, reports_dir):
    reports_dir = Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime('%Y%m%d-%H%M%S')
    proposals = reports_dir / f'proposals-{ts}.csv'
    review = reports_dir / f'review-{ts}.csv'
    with open(proposals, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['artist', 'artist_key', 'status', 'tracks', 'tracks_to_change', 'current_genres',
                    'proposed_genres', 'sources', 'top_scores', 'not_in_hierarchy', 'reason'])
        for r in results:
            top = sorted(r['scores'].items(), key=lambda kv: -kv[1])[:10]
            drop = sorted(r['dropped'].items(), key=lambda kv: -kv[1])[:6]
            w.writerow([r['name'], r['key'], r['status'], r['tracks'], r['n_change'], '; '.join(r['current']),
                        '; '.join(r['genres']), ' '.join(f'{k}:{v}' for k, v in sorted(r['contrib'].items())),
                        '; '.join(f'{g} {s:.2f}' for g, s in top),
                        '; '.join(f'{g} {s:.2f}' for g, s in drop), r['reason']])
    unkeyed = db.execute("SELECT relpath FROM tracks WHERE artist_key IS NULL AND error IS NULL").fetchall()
    with open(review, 'w', newline='', encoding='utf-8', errors='surrogateescape') as f:
        w = csv.writer(f)
        w.writerow(['artist', 'artist_key', 'tracks', 'reason'])
        for r in results:
            if r['status'] == 'review':
                w.writerow([r['name'], r['key'], r['tracks'], r['reason']])
        for (rel,) in unkeyed:
            w.writerow(['', '', 1, f'no artist tag: {rel.decode("utf-8", "surrogateescape")}'])
    return proposals, review, len(unkeyed)
