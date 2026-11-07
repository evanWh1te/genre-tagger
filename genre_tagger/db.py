"""SQLite state. One DB per library root.

Track paths are stored as raw bytes relative to the library root, so the DB
survives the library being mounted somewhere else (e.g. /music in a container)
and copes with filenames that are not valid UTF-8.

Genre values are JSON lists. NULL means the file has no genre tag at all,
which is different from [''] (a tag holding an empty string); rollback
restores whichever it was.
"""
import json
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS tracks (
    relpath     BLOB PRIMARY KEY,
    size        INTEGER,
    mtime       REAL,
    format      TEXT,     -- flac | mp3 | wav
    id3_version TEXT,     -- '2.3.0' / '2.4.0' for ID3 formats
    mb_artistid TEXT,     -- JSON list, as tagged
    albumartist TEXT,     -- JSON list
    artist      TEXT,     -- JSON list
    genre       TEXT,     -- JSON list, NULL = no tag
    artist_key  TEXT,     -- 'mb:<uuid>' or 'name:<folded name>'; NULL = unidentifiable
    scanned_at  REAL,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS tracks_artist ON tracks(artist_key);

CREATE TABLE IF NOT EXISTS artists (
    key       TEXT PRIMARY KEY,
    name      TEXT,       -- display name (most common tag spelling)
    mbid      TEXT,       -- from tags, or from an unambiguous MusicBrainz search
    tracks    INTEGER,
    current   TEXT        -- JSON list of distinct current genres (split like Navidrome)
);

-- Raw API responses. One row per (artist, source); reruns never refetch.
CREATE TABLE IF NOT EXISTS cache (
    artist_key TEXT,
    source     TEXT,      -- mb_search | musicbrainz | discogs | lastfm
    status     TEXT,      -- ok | notfound | ambiguous | skipped
    payload    TEXT,
    fetched_at REAL,
    PRIMARY KEY (artist_key, source)
);

CREATE TABLE IF NOT EXISTS overrides (
    artist_key TEXT PRIMARY KEY,
    genres     TEXT,      -- JSON list; replaces the looked-up genres entirely
    mbid       TEXT,      -- pins the MusicBrainz artist for an ambiguous name
    note       TEXT,
    updated_at REAL
);

CREATE TABLE IF NOT EXISTS resolved (
    artist_key  TEXT PRIMARY KEY,
    genres      TEXT,     -- JSON list (proposal)
    scores      TEXT,     -- JSON {genre: score} for the report
    status      TEXT,     -- ok | override | review
    reason      TEXT,
    resolved_at REAL
);

-- Genre as it was before this tool first wrote the file. Never overwritten
-- once the file has been written, so rollback always reaches the true original.
CREATE TABLE IF NOT EXISTS originals (
    relpath     BLOB PRIMARY KEY,
    genre       TEXT,
    captured_at REAL
);

CREATE TABLE IF NOT EXISTS writes (
    relpath    BLOB PRIMARY KEY,
    genre      TEXT,      -- what we wrote
    written_at REAL,
    verified   INTEGER,
    error      TEXT,
    marker     TEXT       -- GENRETAGGER value written with it (NULL: written before markers existed)
);
"""


def connect(path):
    db = sqlite3.connect(path, timeout=60, check_same_thread=False)
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA busy_timeout=60000')
    db.executescript(SCHEMA)
    if 'marker' not in [r[1] for r in db.execute('PRAGMA table_info(writes)')]:
        db.execute('ALTER TABLE writes ADD COLUMN marker TEXT')
        db.commit()
    return db


def dumps(v):
    return None if v is None else json.dumps(v)


def loads(v):
    return None if v is None else json.loads(v)


def now():
    return time.time()
