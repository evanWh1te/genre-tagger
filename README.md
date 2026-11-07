# genre-tagger

Artist-level genre tagging for a FLAC / MP3 / WAV library. Each unique artist
is looked up once (MusicBrainz, Discogs, Last.fm), the results are merged into
up to six genres, and those genres are written to every track by that artist.

Built for a library managed in MusicBee and served by Navidrome: genres are
written as true multi-value tags that both read, and only the genre field (plus
a `GENRETAGGER` marker saying the tool set it) is ever changed.

## Pipeline

```
scan  ->  lookup  ->  resolve  ->  apply (dry run)  ->  apply --apply
                                                      \-> rollback [--apply]
```

| command    | what it does | touches files? |
|------------|--------------|----------------|
| `scan`     | reads tags of new/changed files, assigns each track an artist | read only |
| `lookup`   | fetches and caches source data per artist | no |
| `resolve`  | merges cached data into proposals; writes proposal + review CSVs | no |
| `apply`    | dry run: per-artist plan CSV of what would change | no |
| `apply --apply` | writes genres, verifying every file | **yes** |
| `rollback` | dry run of restoring original genres | no |
| `rollback --apply` | restores the original genres | **yes** |
| `override` | manual genres, or pin the MusicBrainz artist for an ambiguous name | no |
| `status`   | counts from the state DB | no |

Everything is resumable: `scan` re-reads only files whose size/mtime changed,
`lookup` never refetches a cached artist, and `apply` skips files that already
hold the target genre. Ctrl-C at any point and re-run.

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env      # then fill in GT_CONTACT, DISCOGS_TOKEN, LASTFM_API_KEY
```

Settings come from the environment, then `.env` (the environment wins).
`--library`, `--db`, `--reports`, `--top-n`, `--workers` override per run.

| variable | default | |
|----------|---------|-|
| `GT_LIBRARY` | `/music` | library root |
| `GT_DB` | `genre_tagger.db` | state DB (one per library) |
| `GT_REPORTS` | `reports` | CSV output |
| `GT_CONTACT` | – | email/URL for the MusicBrainz User-Agent |
| `DISCOGS_TOKEN` | – | Discogs personal token; Discogs is skipped without it |
| `LASTFM_API_KEY` | – | Last.fm key; Last.fm is skipped without it |
| `GT_TOP_N` | 6 | max genres per artist |
| `GT_MIN_SCORE` | 0.6 | minimum merged score to keep a genre |
| `GT_LASTFM_MIN_COUNT` | 20 | Last.fm tags weighted below this (0–100) are ignored |
| `GT_WORKERS` | 2 | file threads for apply/rollback (scan uses 4) |
| `GT_HIERARCHY` | bundled RYM | MusicBee tag hierarchy file whose genres are the only ones written; `off` allows any |
| `GT_VOCAB_DB` | – | another state DB whose genre spellings to follow (the library's, when tagging an inbox) |

A source without credentials is skipped and nothing is cached for it, so adding
the key later and re-running `lookup` fills it in.

## Usage

```sh
gt=.venv/bin/genre-tagger

$gt scan                           # first run reads every file (hours on the NAS)
$gt lookup                         # artists with 0-1 genres; --include-multi for all
$gt resolve                        # -> reports/proposals-*.csv, reports/review-*.csv
$gt apply                          # dry run -> reports/apply-plan-*.csv
$gt apply --apply                  # write
$gt rollback                       # dry run
$gt rollback --apply               # restore originals
```

Scope flags for `lookup` / `resolve`: `--include-multi` (also artists that
already have 2+ genres), `--limit N` (first N artists by name), `--artist TEXT`.
`apply` takes `--artist` and `--limit`; `rollback` takes `--artist`.
`apply` writes whatever the most recent `resolve` produced.

### Testing on a subset

Use a separate DB per library root:

```sh
export GT_LIBRARY=/mnt/viridian/media/Process/Audio/inbox GT_DB=test-inbox/inbox.db GT_REPORTS=test-inbox/reports
export GT_VOCAB_DB=state/library.db    # follow the library's spellings
$gt scan && $gt lookup --limit 25 && $gt resolve --limit 25 && $gt apply --limit 25
```

### Seeding from a survey

`scan --from-survey survey.db` imports rows from a survey database (same tag
reads, stored as `files(path BLOB, size, mtime, ext, id3_version, mb_artistid,
albumartist, artist, genre, error)`), then stat-walks the library and re-reads
only what changed since. This turns a multi-hour first scan into under a minute.

## How artists are identified

Per track, in order:

1. the MusicBrainz artist ID tag (the first one, for collaborations)
2. the album artist (or artist) name, folded onto an MBID when the library maps
   that exact name to exactly one MBID elsewhere
3. the folded name (case, whitespace, dash and quote variants unified;
   `feat.` credits stripped)

"Various Artists" album artists fall back to the track artist. Tracks with no
artist tag at all are listed in the review file.

Name-only artists are searched on MusicBrainz by **exact** name or alias:

- one match: used
- several matches: the library's album titles, then track titles, are searched
  on MusicBrainz; a candidate is accepted only if it is the **only** one credited
  on a matching release group / recording (this also handles compilation-only
  appearances)
- still ambiguous: no source is queried; the artist goes to the review file with
  the candidates listed. Pin the right one with `override set "Name" --mbid <id>`.
- no match: Discogs (exact, unique name) and Last.fm are tried; resolve only
  accepts the result if Discogs found a unique exact match.

## How genres are chosen

`score = sum(weight x strength)` across sources, strength relative within each source:

| source | weight | strength |
|--------|--------|----------|
| MusicBrainz genres (curated) | 3.0 | votes / max votes |
| MusicBrainz tags (uncurated) | 0.5 | votes / max votes, so these never pass alone |
| Discogs styles | 2.0 | share of the artist's masters with the style |
| Discogs genres | 0.5 | share of masters with the genre; only 15 broad buckets (metal is filed under "Rock"), so these never pass alone |
| Last.fm top tags | 2.0 | count / 100, only count >= `GT_LASTFM_MIN_COUNT` |
| existing tags | 1.0 | share of the artist's tracks with the genre |

Genres scoring at least `GT_MIN_SCORE` are kept, best first, up to `GT_TOP_N`.
Existing tags alone never produce a proposal.

Normalization (`genre_tagger/normalize.py`):

- spelling and casing follow the library's own most common spelling, so
  `hip-hop` becomes `Hip Hop` if that is what the library uses
- true synonyms are merged (`R&B`, `Drum & Bass`, `Electronica` -> `Electronic`, ...)
- compound values are split: `Rap/Hip Hop` -> `Rap`, `Hip Hop`;
  `Pop/Rock→Rock→Metal` -> `Metal`; Discogs `Funk / Soul` -> `Funk`, `Soul`
- junk is dropped: `seen live`, favourites, decades and years, nationalities,
  `female vocalists`, the artist's own name, ...; `80s Punk` -> `Punk`
- no genre name ever contains `;`, `/` or `,`, because Navidrome splits on them

### Genre dictionary (RateYourMusic hierarchy)

By default only genres in the RateYourMusic tag hierarchy for MusicBee
([FlakyBlueJay/mb-rym-hierarchy](https://github.com/FlakyBlueJay/mb-rym-hierarchy),
bundled as `genre_tagger/data/rym-hierarchy.txt`, commit `6773def`, 2026-11-02)
are written, spelled the RYM way. This is a static file from GitHub; nothing is
fetched from rateyourmusic.com. Point `GT_HIERARCHY` at your own MusicBee copy
to follow it instead, or set it to `off`.

- exact names match regardless of case, spacing, hyphens and `&`/`and`
  (`drum & bass` -> `Drum and Bass`)
- `hierarchy.ALIASES` maps source tags RYM names differently
  (`Punk` -> `Punk Rock`, `Thrash` -> `Thrash Metal`, `Rap` -> `Hip Hop`, ...)
- a tag matching several bracketed RYM names (`Hardcore` -> `Hardcore [EDM]` or
  `Hardcore [Punk]`) goes to the one whose top-level groups (Electronic, Rock, ...)
  hold most of the artist's other genres; `Hardcore [Punk]` is written `Hardcore Punk`.
  Such a bare tag only counts when a second source (MusicBrainz, Discogs,
  Last.fm, existing tags) also sent it or named the variant outright: Last.fm
  alone tags many metalcore bands "hardcore"
- category nodes with a separator (`R&B / Soul / Funk`) are never written
- everything else is dropped; the proposals CSV lists what was dropped in
  `not_in_hierarchy` (only values that would otherwise have been kept), which
  is where to look for a missing alias

## How tags are written

| format | storage |
|--------|---------|
| FLAC | repeated `GENRE` Vorbis comments |
| MP3 / WAV, ID3v2.3 | one `TCON` frame, values null-separated (Latin-1, or UTF-16 when needed) |
| MP3 / WAV, ID3v2.4 | one `TCON` frame, values null-separated (UTF-8) |

This matches what MusicBee already wrote for multi-genre files in the library,
and Navidrome (TagLib) reads all three as multi-valued. Each file keeps its ID3
version.

Every file the tool writes also gets a `GENRETAGGER` tag (a Vorbis comment in
FLAC, an ID3 `TXXX:GENRETAGGER` frame in MP3/WAV) with the date and how the
genres were chosen, e.g. `2026-11-06 auto` or `2026-11-06 override`. In
MusicBee, map a custom tag to `GENRETAGGER` (Preferences > Tags (1) > Define
New Tags) to show, sort or filter on it; Navidrome ignores it. `rollback`
removes it. Files written before the marker existed get it, dated by their
original write, on the next `apply --apply` (genre left as is).

ID3 tags are edited by splicing raw bytes: every frame except `TCON` is copied
byte-for-byte. mutagen's ID3 save is deliberately not used: for v2.3 files it
drops v2.4-only frames such as `TSOP` (artist sort) and `TDOR` (original date)
and re-encodes UTF-8 frames, and both occur in this library. Tags with
unsynchronisation or an extended header are refused, not risked. ID3v1 tails
are left untouched.

## Safety

- `apply` and `rollback` are dry runs unless given `--apply`.
- Before the first write, each file's genre is recorded in the DB (`originals`)
  and exported to `reports/backup-originals-*.csv`. The original is never
  overwritten by later runs, so rollback always reaches the true original,
  including "no genre tag at all".
- Every write is verified by re-reading the file: the genre must read back
  exactly, and a raw-byte snapshot of everything else (all other frames or
  comments, artwork and metadata blocks, ID3 version, RIFF chunks, and the
  audio region's length plus a hash of its first and last 64 KiB) must be
  unchanged.
- If verification fails, the original tag region (kept in memory) is put back,
  the file is re-verified against its pre-write snapshot, and the run stops.
- Problems go to `reports/*-errors-*.csv`; the console shows counts and the
  first few.

After an apply, have MusicBee rescan tags for the changed files; Navidrome picks
the changes up on its next library scan.

## Reports

| file | contents |
|------|----------|
| `proposals-*.csv` | every artist in scope: status, tracks, tracks to change, current genres, proposed genres, contributing sources, top scores, reason |
| `review-*.csv` | artists needing a human (ambiguous name, no match, no data) and tracks with no artist tag |
| `apply-plan-*.csv` / `rollback-plan-*.csv` | per artist: tracks to change, old genres (with track counts), new genres |
| `backup-originals-*.csv` | relpath + original genre (JSON) of every file about to be written |
| `*-errors-*.csv` | files that failed or were refused, with the reason |

## Overrides

```sh
$gt override set "Converge" --genres "Metalcore; Mathcore; Hardcore Punk"
$gt override set "Ghost" --mbid 6ec1c1b8-...    # pin an ambiguous name; lookup refetches
$gt override list
$gt override rm "Converge"
```

Separate genres with `;`. Override genres replace the looked-up result
entirely. Run `resolve` after changing overrides.

## Docker / Unraid

The image keeps state in `/data` and expects the library at `/music`. The DB
stores paths relative to the library root, so a DB built on the workstation
works in the container as long as `/music` is the same folder.

```sh
docker build -t genre-tagger .

docker run --rm --user 99:100 \
  -v /mnt/user/Media/Audio/Collection:/music \
  -v /mnt/user/appdata/genre-tagger:/data \
  genre-tagger status
```

Put the `.env` in the `/data` folder. `GT_LIBRARY`, `GT_DB` and `GT_REPORTS`
are fixed by the image, so the same `.env` works on both machines. Run as
`99:100` (nobody:users) so files and reports keep Unraid's ownership.
Running on the NAS itself avoids NFS: the workstation pays about 55 ms of
latency per file.

## Timing (this library, over NFS from the workstation)

- full tag read: about 14 files/s, roughly 2 h for 92k tracks (avoid with `--from-survey`)
- lookup: MusicBrainz about 1 artist/s (2 calls for name-only artists), Discogs
  about 3 calls per artist at 60/min; sources run in parallel, so expect about
  4–5 h for 4,800 artists
- apply: bounded by per-file latency; resumable, so it can run in sessions
