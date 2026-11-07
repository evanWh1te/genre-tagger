"""scan: walk the library, read tags for new/changed files, assign artist keys.

Only files whose size or mtime changed since the last scan are re-read, so a
re-run after an interruption (or after a survey import) costs one stat per file.
"""
import collections
import json
import os
import re
import sqlite3
import sys
import unicodedata
from concurrent.futures import ThreadPoolExecutor

from . import tags
from .db import dumps, loads, now

UUID_RE = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', re.I)
FEAT_RE = re.compile(r'\s+(?:feat\.?|ft\.?|featuring)\s+.*$', re.I)
VARIOUS = {'various artists', 'various', 'va', 'various artist'}
DASHES = str.maketrans({c: '-' for c in '‐‑‒–—−'} |
                       {'‘': "'", '’': "'", '“': '"', '”': '"'})
GENRE_SPLIT = re.compile(r'\s*(?:;|/|,|\|)\s*')


def fold(name):
    """Comparison form of an artist name."""
    s = unicodedata.normalize('NFKC', name).translate(DASHES).casefold()
    return re.sub(r'\s+', ' ', s).strip()


def split_genres(values):
    """Genres as Navidrome would see them (it splits on ; / ,)."""
    out = []
    for v in values or []:
        for p in GENRE_SPLIT.split(v):
            p = p.strip()
            if p and p not in out:
                out.append(p)
    return out


def import_survey(db, survey_path, root):
    """Seed the tracks table from a survey DB made by the same tag reads.
    Rows outside `root` are ignored."""
    src = sqlite3.connect(survey_path)
    prefix = os.fsencode(str(root)).rstrip(b'/') + b'/'
    n = skipped = 0
    rows = []
    for (path, size, mtime, ext, id3v, mbid, aa, ar, genre, err) in src.execute(
            "SELECT path, size, mtime, ext, id3_version, mb_artistid, albumartist, artist, genre, error FROM files"):
        if not path.startswith(prefix):
            skipped += 1
            continue
        rows.append((path[len(prefix):], size, mtime, ext.lstrip('.'), id3v, mbid, aa, ar, genre, now(), err))
        n += 1
    db.executemany("""INSERT OR REPLACE INTO tracks
        (relpath, size, mtime, format, id3_version, mb_artistid, albumartist, artist, genre, scanned_at, error)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)""", rows)
    db.commit()
    print(f'imported {n} tracks from survey' + (f' ({skipped} outside library root ignored)' if skipped else ''))


def scan(db, root, workers=4):
    root_b = os.fsencode(str(root)).rstrip(b'/')
    known = {r[0]: (r[1], r[2]) for r in db.execute("SELECT relpath, size, mtime FROM tracks")}
    seen, todo = set(), []
    for d, _, files in os.walk(root_b):
        for f in files:
            if tags.fmt_of(f) is None:
                continue
            p = os.path.join(d, f)
            rel = p[len(root_b) + 1:]
            seen.add(rel)
            st = os.stat(p)
            if known.get(rel) != (st.st_size, st.st_mtime):
                todo.append((rel, p, st.st_size, st.st_mtime))
    print(f'{len(seen)} audio files; {len(seen) - len(todo)} unchanged, {len(todo)} to read', flush=True)

    def read(item):
        rel, p, size, mtime = item
        try:
            return rel, size, mtime, tags.read(p), None
        except Exception as e:
            return rel, size, mtime, {'format': tags.fmt_of(p)}, f'{type(e).__name__}: {e}'

    errors = 0
    with ThreadPoolExecutor(workers) as ex:
        for i, (rel, size, mtime, t, err) in enumerate(ex.map(read, todo), 1):
            errors += err is not None
            db.execute("""INSERT OR REPLACE INTO tracks
                (relpath, size, mtime, format, id3_version, mb_artistid, albumartist, artist, genre, scanned_at, error)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                       (rel, size, mtime, t.get('format'), t.get('id3_version'), dumps(t.get('mb_artistid')),
                        dumps(t.get('albumartist')), dumps(t.get('artist')), dumps(t.get('genre')), now(), err))
            if i % 500 == 0:
                db.commit()
                print(f'  read {i}/{len(todo)}', flush=True)
    gone = [r for r in known if r not in seen]
    db.executemany("DELETE FROM tracks WHERE relpath=?", [(r,) for r in gone])
    db.commit()
    print(f'read errors: {errors}; removed {len(gone)} tracks no longer on disk')
    if errors:
        for rel, err in db.execute("SELECT relpath, error FROM tracks WHERE error IS NOT NULL LIMIT 5"):
            print(f'  {os.fsdecode(rel)}: {err}', file=sys.stderr)


def assign_keys(db):
    """Artist identity per track, in order of preference:
      1. MusicBrainz artist ID (first one, for collaborations)
      2. the album artist / artist name, folded onto an MBID when the library
         maps that name to exactly one MBID elsewhere
      3. the folded name itself
    'Various Artists' album artists fall back to the track artist. Tracks with
    no usable name get NULL and are listed for review."""
    rows = db.execute("SELECT relpath, mb_artistid, albumartist, artist, genre FROM tracks WHERE error IS NULL").fetchall()

    name2mb = collections.defaultdict(collections.Counter)
    for _, mb, _, ar, _ in rows:
        ids = UUID_RE.findall(' '.join(loads(mb) or []))
        ar = loads(ar) or []
        if len(ids) == 1 and len(ar) == 1:
            name2mb[fold(ar[0])][ids[0].lower()] += 1
    unique = {n: c.most_common(1)[0][0] for n, c in name2mb.items() if len(c) == 1}

    updates = []
    art = collections.defaultdict(lambda: {'names': collections.Counter(), 'tracks': 0,
                                           'genres': collections.Counter(), 'mbid': None})
    for rel, mb, aa, ar, genre in rows:
        ids = UUID_RE.findall(' '.join(loads(mb) or []))
        aa, ar = loads(aa) or [], loads(ar) or []
        album_artist = aa[0].strip() if aa and fold(aa[0]) not in VARIOUS else None
        track_artist = ar[0].strip() if ar else None
        if ids:
            k, mbid, name = f'mb:{ids[0].lower()}', ids[0].lower(), track_artist or album_artist
        else:
            name = album_artist or (FEAT_RE.sub('', track_artist) if track_artist else None)
            if not name:
                updates.append((None, rel))
                continue
            mbid = unique.get(fold(name))
            k = f'mb:{mbid}' if mbid else f'name:{fold(name)}'
        updates.append((k, rel))
        a = art[k]
        a['tracks'] += 1
        a['mbid'] = a['mbid'] or mbid
        if name:
            a['names'][name] += 1
        for g in split_genres(loads(genre)):
            a['genres'][g] += 1

    db.executemany("UPDATE tracks SET artist_key=? WHERE relpath=?", updates)
    db.execute("DELETE FROM artists")
    db.executemany("INSERT INTO artists (key, name, mbid, tracks, current) VALUES (?,?,?,?,?)",
                   [(k, a['names'].most_common(1)[0][0] if a['names'] else k, a['mbid'], a['tracks'],
                     json.dumps([g for g, _ in a['genres'].most_common()]))
                    for k, a in art.items()])
    db.commit()
    by = collections.Counter(k.split(':')[0] for k in art)
    unkeyed = sum(1 for k, _ in updates if k is None)
    print(f'{len(art)} artists ({by["mb"]} with MBID, {by["name"]} name-only); '
          f'{unkeyed} tracks with no artist tag')


def select_artists(db, include_multi=False, limit=None, artist=None):
    """Artists in scope, by name. Default scope: at most one current genre."""
    rows = db.execute("SELECT key, name, mbid, tracks, current FROM artists ORDER BY name COLLATE NOCASE").fetchall()
    out = []
    for key, name, mbid, n, current in rows:
        current = json.loads(current)
        if not include_multi and len(current) > 1:
            continue
        if artist and artist.casefold() not in (name or '').casefold() and artist != key:
            continue
        out.append({'key': key, 'name': name, 'mbid': mbid, 'tracks': n, 'current': current})
    return out[:limit] if limit else out
