# genre-tagger: notes for Claude sessions

This file orients a new session. Read `README.md` for how the tool works
(pipeline, scoring, identity, tag writing, reports, Docker). This file covers
what the README does not: **current state, the user's decisions, hard rules,
and traps found the hard way.** Last updated 2026-11-08.

The user's global `~/.claude/CLAUDE.md` also applies: dry-run first,
quarantine rather than delete, verify after writing, resumable jobs, modest
concurrency, report failures plainly, aggregate rather than enumerate.

## What this is

A CLI (`.venv/bin/genre-tagger`) that gives each artist up to 6 genres, using
the RateYourMusic vocabulary, from MusicBrainz, Discogs and Last.fm. It writes
the same list to every in-scope track of that artist, plus a `GENRETAGGER`
marker. Python + mutagen + SQLite; all state is in one SQLite DB per root.

It is stage 3 of the user's planned music pipeline:

```
intake (slskd / deemix / EAC) -> identify (music-identifier) -> genre-tagger -> move to library
```

It is meant to become a reusable Claude Code skill and to run in Docker on
the Unraid NAS. The Dockerfile exists, but **no image has been built yet**.

## Hard rules from the user

- **Do not scrape rateyourmusic.com.** The RYM *hierarchy* comes from a GitHub
  repo (FlakyBlueJay/mb-rym-hierarchy). That is fine; RYM itself is not.
- API keys come only from env or `.env` (`GT_CONTACT`, `DISCOGS_TOKEN`,
  `LASTFM_API_KEY`). Never hardcode, print, or commit them. `.env` is gitignored.
- **No `apply --apply` on the live library without the user's explicit go-ahead.**
  Dry runs are always fine. Show the plan CSV summary first.
- Commit when asked, with the attribution line the harness gives you. The remote
  is `forge` (Forgejo); see the global CLAUDE.md.

## Decisions the user made (don't re-litigate)

- Up to **6 genres** per artist (MusicBee's maximum).
- **Only genres in the RYM hierarchy** are written. The bundled copy is at
  `genre_tagger/data/rym-hierarchy.txt`, upstream commit 6773def. Source tags
  map through `ALIASES` in `hierarchy.py`.
- A bare **"hardcore"** needs 2+ providers to agree. Bare **R&B** is dropped.
  **Classic Rock** is dropped (the user deferred on this; dropped is the current state).
- A **`GENRETAGGER`** marker on each written file, in the form `YYYY-MM-DD auto`
  or `... override`. It is a Vorbis comment for FLAC and an ID3 `TXXX` frame
  for MP3/WAV. Tracks that already hold the proposed genres also get the marker,
  so an empty marker means "not confirmed by the tool". Rollback removes it.
- **Staging overwrites everything** (`GT_INCLUDE_MULTI=1`), because downloads
  carry only the source's genres. The **library uses per-track scope**: tracks
  with ≤1 RYM genre get the artist's list; tracks the user curated with 2+
  are left alone. The user confirmed this on 2026-11-07. It puts about 81k of
  92.5k library tracks in scope.
- MBIDs were **not** written into files before the genre sweep. Identification
  moved to a separate project instead (below).

## Current state (2026-11-08)

**Staging is done.** The staging folders are
`/mnt/viridian/media/Process/Audio/{inbox,deemix,EAC}`. `slsk` was deleted and
will return with the intake project; add it back to `GT_FOLDERS` then. The
README example still lists it. Every staging track on record is written,
marked and verified with a TagLib re-read. Most batches were then moved into
the library by the user, so staging rollback no longer reaches them. The
originals CSVs in `state/reports-staging/` are the record.

**The library genre sweep has not been run.** It is deliberately blocked on
the identify phase. Lesson from Broadway (below): identity has to be decided
per album, not per artist name. The identify work was handed to
`/home/evan/Repositories/WhitehomIO/music-identifier`; the brief is
`docs/HANDOFF-identify.md`, also copied there as `HANDOFF.md`. When that
project produces a per-album manifest (album path → release-group, release,
and album-artist MBIDs, status, confidence), the next step for genre-tagger
is to **key artists by those MBIDs** instead of its own name-based guessing
in `lookup.py`. Only then run the library lookup, which takes about 4–5 h for
about 4.7k artists, and get approval before applying.

**Known leftovers:**
- `Collection/Broadway*`: 35 wrongly tagged tracks were rolled back via
  `state/fix-broadway.db`. The real band's copy of *Gentlemen's Brawl* still has
  a stray "Hip Hop" genre. It's a small manual fix; ask before touching it.
- The Docker image has not been built or tested on Unraid.
- 105 `.m4a` files in the library are out of scope (formats handled: FLAC,
  MP3, WAV).

## Local state (gitignored — not in the repo)

| Path | What |
|---|---|
| `.env` | keys (present) |
| `state/staging.db` | staging root `Process/Audio`, scan cache, API response cache, overrides, writes + originals |
| `state/reports-staging/` | proposals, review, apply-plan, backup-originals CSVs, `candidates-*.md` |
| `state/library.db` | library root `Audio/Collection`, seeded from the 2-hour tag survey; **lookups not run** |
| `state/fix-broadway.db`, `state/reports-fix-broadway/` | the Broadway rollback |
| `test-inbox/inbox.db` | backup of the inbox DB from before it was migrated into staging.db |

Staging env, as used:

```sh
export GT_LIBRARY=/mnt/viridian/media/Process/Audio GT_FOLDERS=inbox,deemix,EAC
export GT_DB=state/staging.db GT_REPORTS=state/reports-staging
export GT_INCLUDE_MULTI=1 GT_VOCAB_DB=state/library.db
```

Overrides in staging.db: MBID pins for Turnstyle, Ken Ishii, Vinicius Honorio
and Hevi. Genre overrides for Roos (Hip Hop), Brian Futch (Blues Rock; Rock;
Singer-Songwriter) and arancina (Lo-Fi Hip Hop).

## Workflow for leftovers (review artists)

`resolve` puts unresolved artists in `review-*.csv`: ambiguous names, no MB
match, and no genres. Work them like this:

1. Run `genre-tagger candidates` to get a Markdown report that sets local
   albums beside the MusicBrainz candidates and their release lists.
2. Research where needed. MusicBrainz, Discogs and Bandcamp are fine; RYM is not.
3. Pin with `override ARTIST --mbid ...`, or set `--genres "A; B"`.
4. Run `lookup`, then `resolve`, then `apply` as a dry run. Show the user,
   then `--apply`.

## Traps (each cost real time)

- **mutagen's ID3v2.3 save is lossy** on this library. It drops TSOP/TDOR that
  MusicBee writes into v2.3, and it re-encodes UTF-8. `tags.py` splices raw
  frames instead; don't "simplify" that back to `tags.save()`.
- **Navidrome splits genres on `;`, `/` and `,`**, so a written genre name must
  never contain them. MusicBee's v2.3 multi-genre format is Latin-1, null-separated.
- **Deezer artist IDs lump same-name artists together** (Broadway, 73876).
  Name-plus-title evidence produced a false positive and wrong genres on
  35 tracks. A silent misidentification is worse than a review row.
- **The tool's own writes must not count as evidence.** The "existing genre"
  signal reads originals for files the tool wrote. An earlier bug fed writes
  back as votes.
- **Artists already written to must stay in scope**, or later corrections
  skip them. This was a regression once.
- **Lookup race:** Discogs and Last.fm must wait for the MusicBrainz step
  (`mb_seen`). Otherwise they run on a missing MBID, and you get MB-only genres.
- **Bracketed RYM names** ("Hardcore [Punk]" vs "[EDM]", "Trap" vs "Trap [EDM]")
  are settled by the artist's other genres. `BRACKET_DISPLAY` writes the
  readable form.
- **`scan` refuses to run if a `GT_FOLDERS` entry is missing.** This is
  deliberate, so a vanished folder can't silently drop its tracks.
- **NFS:** the first read of each file costs about 55 ms, and more threads
  don't help. A whole-library tag read takes about 2 h. Paths are stored as
  BLOBs relative to the root, because some aren't valid UTF-8.
- **TagLib** (used for verification) chokes on non-UTF-8 paths. Copy such a
  file to an ASCII-named temp path to check it.
- The `sqlite3` CLI isn't installed, so query with Python. There's no
  passwordless sudo, so you can't drop the page cache.
- **MusicBee caches tags.** After any write, the user has to rescan tags in
  MusicBee before the result shows up there.

## Code map

| File | Role |
|---|---|
| `config.py` | settings from env / `.env` (`GT_*`) |
| `db.py` | schema + migrations (files, artists, cache, overrides, proposals, writes) |
| `scan.py` | tag reads, artist keys, `sparse()` scope test, `select_artists` |
| `sources.py` | rate-limited MusicBrainz (1.1 s/req), Discogs, Last.fm clients |
| `lookup.py` | identity resolution, caching, collab fallback, `identities`, `candidates` |
| `hierarchy.py` | RYM tree parser, `ALIASES`, ambiguity + `settle()` |
| `normalize.py` | key/synonym/junk normalization, display spelling |
| `resolve.py` | weighted scoring (weights in `W`), proposals/review CSVs |
| `tags.py` | read/write/snapshot/verify, raw ID3 splicing, marker |
| `apply.py` | apply/rollback targets, verified writes, marker values |
| `cli.py` | argparse front end |

There is no test suite. Verification has been done by dry-run CSVs plus a
TagLib re-read of every written file after each apply.
