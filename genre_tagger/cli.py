"""genre-tagger CLI: scan -> lookup -> resolve -> apply (dry run by default) / rollback."""
import argparse
import collections
import csv
import json
import sys
import time
from pathlib import Path

from . import apply as ap
from . import db as dbm
from . import hierarchy as hy
from . import lookup as lk
from . import resolve as rs
from . import scan as sc
from . import tags
from .config import settings as load_settings


def _scope_args(p):
    p.add_argument('--include-multi', action='store_true',
                   help='also process artists whose tracks all have 2+ genres '
                        '(default: artists with any track holding 0-1)')
    p.add_argument('--limit', type=int, help='only the first N artists (by name), for testing')
    p.add_argument('--artist', help='only artists whose name contains this text (or exact artist key)')


def find_artist(db, ref):
    rows = db.execute("SELECT key, name FROM artists WHERE key=? OR lower(name)=lower(?)", (ref, ref)).fetchall()
    if len(rows) == 1:
        return rows[0]
    if not rows:
        sys.exit(f'no artist matches {ref!r} (use the artist name or key from a report)')
    sys.exit(f'{ref!r} matches several artists; use the key:\n' + '\n'.join(f'  {k}  {n}' for k, n in rows))


def cmd_scan(s, db, a):
    if a.from_survey:
        sc.import_survey(db, a.from_survey, s.library)
    sc.scan(db, s.library, a.workers, s.folders)
    sc.assign_keys(db)


def cmd_lookup(s, db, a):
    artists = sc.select_artists(db, s.include_multi, a.limit, a.artist, hy.load(s.hierarchy))
    lk.run(s, artists, a.sources.split(','))


def cmd_resolve(s, db, a):
    artists = sc.select_artists(db, s.include_multi, a.limit, a.artist, hy.load(s.hierarchy))
    results = rs.resolve(db, s, artists)
    proposals, review, unkeyed = rs.write_reports(db, results, s.reports)
    by = collections.Counter(r['status'] for r in results)
    changing = [r for r in results if r['genres'] and r['n_change']]
    print(f'{len(results)} artists: {dict(by)}')
    print(f'{len(changing)} artists / {sum(r["n_change"] for r in changing)} tracks would change')
    if unkeyed:
        print(f'{unkeyed} tracks have no artist tag (listed in the review file)')
    print(f'proposals: {proposals}\nreview:    {review}')


def _report_errors(s, res, kind):
    rows = [('error', r, d) for r, d in res['errors']] + [('verify_failed', r, d) for r, d in res['verify_failed']]
    if not rows:
        return
    path = Path(s.reports) / f'{kind}-errors-{time.strftime("%Y%m%d-%H%M%S")}.csv'
    with open(path, 'w', newline='', encoding='utf-8', errors='surrogateescape') as f:
        w = csv.writer(f)
        w.writerow(['kind', 'relpath', 'detail'])
        for k, r, d in rows:
            w.writerow([k, r.decode('utf-8', 'surrogateescape'), d])
    print(f'problems written to {path}; first few:')
    for k, r, d in rows[:5]:
        print(f'  {k}: {r.decode("utf-8", "surrogateescape")}: {d}')


def _run(s, db, a, targets, kind):
    plan, n_artists = ap.plan_report(targets, s.reports, kind)
    print(f'{len(targets)} tracks across {n_artists} artists to change; plan: {plan}')
    marker_only = sum(1 for _, target, _, cur, _ in targets if kind == 'apply' and cur == target)
    if marker_only:
        print(f'  of which {marker_only} only need the {tags.MARKER} marker (genre already correct)')
    if not a.apply:
        print('dry run: nothing written. Re-run with --apply to write.')
        return
    if not targets:
        return
    res = ap.execute(db, s, targets, kind)
    print(f"written+verified: {res['written']}, already correct: {res['already']}, "
          f"errors: {len(res['errors'])}, verification failures: {len(res['verify_failed'])}")
    if res['verify_failed']:
        print(f"STOPPED at the first verification failure; {res['cancelled']} files were not attempted.")
    _report_errors(s, res, kind)
    if res['written']:
        print('MusicBee: rescan tags for the changed files. Navidrome picks changes up on its next scan.')


def cmd_apply(s, db, a):
    if not db.execute("SELECT 1 FROM resolved LIMIT 1").fetchone():
        sys.exit('nothing resolved yet: run resolve first')
    _run(s, db, a, ap.apply_targets(db, a.artist, a.limit, s.include_multi,
                                       hy.load(s.hierarchy)), 'apply')


def cmd_rollback(s, db, a):
    _run(s, db, a, ap.rollback_targets(db, a.artist), 'rollback')


def cmd_override(s, db, a):
    if a.action == 'list':
        for key, genres, mbid, note, name in db.execute(
                "SELECT o.artist_key, o.genres, o.mbid, o.note, a.name FROM overrides o "
                "LEFT JOIN artists a ON a.key = o.artist_key ORDER BY a.name"):
            print(f'{name or "?"}  [{key}]  genres={json.loads(genres) if genres else "-"}  '
                  f'mbid={mbid or "-"}  {note or ""}')
        return
    key, name = find_artist(db, a.artist)
    if a.action == 'rm':
        db.execute("DELETE FROM overrides WHERE artist_key=?", (key,))
    else:
        if not a.genres and not a.mbid:
            sys.exit('give --genres and/or --mbid')
        genres = [g.strip() for g in a.genres.split(';') if g.strip()] if a.genres else None
        if genres and any(c in g for g in genres for c in '/,'):
            sys.exit("genre names can't contain / or , (Navidrome splits on them); separate genres with ;")
        db.execute("""INSERT INTO overrides (artist_key, genres, mbid, note, updated_at) VALUES (?,?,?,?,?)
                      ON CONFLICT(artist_key) DO UPDATE SET
                        genres=COALESCE(excluded.genres, genres), mbid=COALESCE(excluded.mbid, mbid),
                        note=COALESCE(excluded.note, note), updated_at=excluded.updated_at""",
                   (key, json.dumps(genres) if genres else None, a.mbid, a.note, dbm.now()))
        if a.mbid:  # identity changed: drop cached source data so lookup refetches
            db.execute("DELETE FROM cache WHERE artist_key=? AND source != 'mb_search'", (key,))
    db.commit()
    print(f'{a.action}: {name} [{key}]')


def cmd_identities(s, db, a):
    rows = lk.identities(db)
    Path(s.reports).mkdir(parents=True, exist_ok=True)
    path = Path(s.reports) / f'identities-{time.strftime("%Y%m%d-%H%M%S")}.csv'
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]) if rows else ['artist'])
        w.writeheader()
        w.writerows(rows)
    print(f'{len(rows)} artists: {dict(collections.Counter(r["status"] for r in rows))}')
    print('confirmed by:', dict(collections.Counter(r['how'] for r in rows if r['status'] == 'confirmed')))
    print(f'identities: {path}')


def cmd_status(s, db, a):
    q = lambda sql: db.execute(sql).fetchall()
    print('library:', s.library, '| db:', s.db)
    print('tracks:', q("SELECT count(*), sum(error IS NOT NULL), sum(artist_key IS NULL) FROM tracks")[0],
          '(total, read errors, no artist)')
    print('artists:', q("SELECT count(*) FROM artists")[0][0])
    print('cache:', q("SELECT source, status, count(*) FROM cache GROUP BY 1,2"))
    print('resolved:', q("SELECT status, count(*) FROM resolved GROUP BY 1"))
    print('writes:', q("SELECT verified, count(*) FROM writes GROUP BY 1"), '| originals:',
          q("SELECT count(*) FROM originals")[0][0])


def main(argv=None):
    p = argparse.ArgumentParser(prog='genre-tagger', description=__doc__)
    p.add_argument('--env-file', default='.env')
    p.add_argument('--library', help='library root (env GT_LIBRARY)')
    p.add_argument('--db', help='state database (env GT_DB)')
    p.add_argument('--reports', help='report directory (env GT_REPORTS)')
    p.add_argument('--top-n', type=int, help='max genres per artist (env GT_TOP_N, default 6)')
    p.add_argument('--workers', type=int, help='file I/O threads (env GT_WORKERS, default 2)')
    p.add_argument('--vocab-db', help="follow this state DB's genre spellings, e.g. the library's "
                                      'when tagging an inbox (env GT_VOCAB_DB)')
    p.add_argument('--folders', help='comma-separated subfolders of the library to scan, e.g. '
                                     '"inbox,deemix,EAC,slsk" (env GT_FOLDERS; default: all)')
    p.add_argument('--hierarchy', help='MusicBee tag hierarchy file; only its genres are written. '
                                       "Default: bundled RYM hierarchy; 'off' to allow any (env GT_HIERARCHY)")
    sub =p.add_subparsers(dest='cmd', required=True)

    x = sub.add_parser('scan', help='read tags (only new/changed files) and assign artist keys')
    x.add_argument('--from-survey', help='seed from a survey DB instead of re-reading every file')
    x.set_defaults(fn=cmd_scan)

    x = sub.add_parser('lookup', help='fetch and cache MusicBrainz / Discogs / Last.fm data')
    _scope_args(x)
    x.add_argument('--sources', default='musicbrainz,discogs,lastfm')
    x.set_defaults(fn=cmd_lookup)

    x = sub.add_parser('resolve', help='merge cached data into proposals; writes proposal + review CSVs')
    _scope_args(x)
    x.set_defaults(fn=cmd_resolve)

    x = sub.add_parser('apply', help='write resolved genres (dry run unless --apply)')
    x.add_argument('--apply', action='store_true', help='actually write files')
    x.add_argument('--artist')
    x.add_argument('--limit', type=int, help='only the first N artists')
    x.add_argument('--include-multi', action='store_true',
                   help='also overwrite tracks that already have 2+ genres (default: only 0-1)')
    x.set_defaults(fn=cmd_apply)

    x = sub.add_parser('rollback', help='restore original genres (dry run unless --apply)')
    x.add_argument('--apply', action='store_true', help='actually write files')
    x.add_argument('--artist')
    x.set_defaults(fn=cmd_rollback)

    x = sub.add_parser('override', help='manual genres or MBID pin for an artist')
    x.add_argument('action', choices=['set', 'rm', 'list'])
    x.add_argument('artist', nargs='?', help='artist name or key')
    x.add_argument('--genres', help='"Genre A; Genre B"')
    x.add_argument('--mbid', help='pin the MusicBrainz artist (for ambiguous names)')
    x.add_argument('--note')
    x.set_defaults(fn=cmd_override)

    x = sub.add_parser('identities', help='export each artist\'s MusicBrainz identity (CSV), from cached lookups')
    x.set_defaults(fn=cmd_identities)

    x = sub.add_parser('status', help='summary of the state database')
    x.set_defaults(fn=cmd_status)

    a = p.parse_args(argv)
    if a.cmd == 'override' and a.action != 'list' and not a.artist:
        p.error('override set/rm needs an artist')
    s = load_settings(a.env_file, library=a.library, db=a.db, reports=a.reports,
                      top_n=a.top_n, workers=a.workers, vocab_db=a.vocab_db,
                      hierarchy=a.hierarchy,
                      folders=tuple(f.strip() for f in a.folders.split(',') if f.strip()) if a.folders else None)
    if getattr(a, 'include_multi', False):
        s.include_multi = True
    if a.cmd == 'scan':
        a.workers = s.workers if a.workers else 4
    db = dbm.connect(s.db)
    try:
        a.fn(s, db, a)
    except KeyboardInterrupt:
        sys.exit('\ninterrupted; progress so far is saved, re-run to continue')
