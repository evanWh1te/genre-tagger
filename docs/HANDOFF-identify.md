# Handoff: album identification ("identify") phase

This document briefs a new session that will build the **identify** phase
of the music pipeline. It covers why the phase exists, what has already been
learned, the constraints, and what the next stage expects from it. It was
written on 2026-11-07, at the end of the session that built `genre-tagger`
(this repo).

## Goal

For every **album** (in practice, every album folder) in the music library and
the staging folders, establish **which MusicBrainz release it is**:

- release-group MBID
- release MBID where possible
- album-artist MBID(s)
- a confidence level and the method used

Albums it can't identify confidently go to a review queue instead of being
guessed.

Identity has to be decided **per album, not per artist name**. That is the
lesson of the genre work (see "Evidence" below).

The first deliverable is **read-only**: a manifest of identifications plus a
review list, with no changes to audio files. Writing MBID tags into files is a
possible later step, and only with the user's explicit approval.

## Where this fits

The user (Evan) runs a homelab music setup and is building three connected
tools:

1. **Intake.** Take a list of music and fetch it with slskd into the staging
   folders. Not started.
2. **Discography completion.** Match library artists, then use slskd to fetch
   the albums missing from each artist's discography. Not started. It needs
   reliable artist MBIDs to query MusicBrainz release groups.
3. **Genre tagging.** Up to 6 RateYourMusic-vocabulary genres per artist,
   written to every track. This is `genre-tagger`, this repo. It is built and
   tested on the staging folders; the main library hasn't been run yet.

The target pipeline is:

```
intake (slskd / deemix / EAC rips) -> identify -> genre-tagger -> move to library
```

The discography tool also reuses the identify output. The user eventually
wants these packaged as Claude Code skills, and runnable in Docker on the
Unraid NAS.

## Environment

The user's global instructions in `~/.claude/CLAUDE.md` load automatically,
so read them. In short: aggregate rather than enumerate, dry-run first,
quarantine rather than delete, verify after writing, make jobs resumable,
keep concurrency modest, and report failures plainly.

**Machines and access**

- Workstation is WSL2 Debian 12. There's no passwordless sudo, so the page
  cache can't be dropped. Docker Desktop's WSL integration was enabled on
  2026-11-06, but the engine may not be running; check `docker info` first.
- The NAS is Unraid. Media is NFS-mounted at `/mnt/viridian/media`, which is
  `/mnt/user/Media` on the NAS itself.
- The first read of each file over NFS costs about 55 ms, and more threads
  don't help. A full tag read of the library took 1 h 52 m at 13.7 files/s.
  Anything whole-library must be resumable and should reuse earlier reads.

**Library**

- Path: `/mnt/viridian/media/Audio/Collection`. Managed by MusicBee (on
  Windows), served by Navidrome. About 3,000 top-level artist folders. Newer
  albums are at `<Album Artist>/<Album> (date) - [flac 16]`; older ones at
  `<Artist>/<Artist> - <Album> (year) [flac - 16 - 44.1 kHz]`.
- 92,558 audio tracks:

  | Format | Tracks |
  |---|---|
  | FLAC | 83,527 |
  | MP3 | 8,866 (4,589 ID3v2.3, 4,277 v2.4) |
  | WAV | 165 |

  There are also 105 `.m4a` files, out of scope so far.
- 68.7% of tracks carry a MusicBrainz artist ID tag. 6,391 artist names:
  4,617 keyed by MBID, and 1,774 name-only artists covering 22,750 tracks.

**Staging**

- Staging is `/mnt/viridian/media/Process/Audio/{inbox,deemix,EAC,slsk}`, the
  folders MusicBee watches. `slsk` is currently deleted and will return with
  the intake project.
- Siblings `lidarr` (41k files), `foobar-transcode` and `audiobooks` are
  **not** in scope.

**Credentials**

- API keys live in `.env` in the repo (not committed): `DISCOGS_TOKEN`,
  `LASTFM_API_KEY` and `GT_CONTACT`, the MusicBrainz User-Agent contact.
- AcoustID would need a new key from the user, also via env or `.env`.
- Never hardcode keys or print them.

**Git**

- Forgejo, remote `forge`; see the user's CLAUDE.md for the API.
- Commit when asked. End commit messages with the attribution line the
  harness gives you.

**Hard rule from the user:** do **not** scrape rateyourmusic.com.

## Evidence: why per-album identity

**Broadway.** The deemix folder `Broadway` held albums by several unrelated
artists named Broadway: a US post-hardcore band, a Russian rapper, and others
with R&B and Country catalogs.

- **Deezer lumps them together.** Deezer files them all under one artist ID,
  73876, so source artist IDs can't be trusted for common names.
- **MusicBrainz knows none of their ISRCs**, not even the real band's.
- **The genre tool's match was a false positive.** Its name-plus-title
  evidence matched one album to the band, and the band's genres were then
  written to all 56 tracks. 35 of them were wrong; those have been rolled
  back.
- **Library location:** `Collection/Broadway/*` and `Collection/Broadway; Wilson/*`.
  This is a good test case.

**Same-name ambiguity is common.**

- MusicBrainz has 11 "Broadway"s and 9 "Roos"es.
- Turnstyle (an Australian band) failed automatic matching: the evidence step
  sampled only the first 10 files, all from one album that MusicBrainz lacks.
  Matching **every** album would have found 2 of its 5 albums on MusicBrainz.

**Not every release is on MusicBrainz.** Deezer and scene web releases often
aren't: *GROOVE* by the Russian Broadway, and techno EPs on small labels.
These must end in **review**, not in a best guess. A silent
misidentification is worse than a leftover.

**Collaboration credits** such as "Turnstile & BADBADNOTGOOD" or "Roos;
Broadway" aren't artists in their own right. Album-artist credits with
`&`, `x`, `,`, `;` and `vs.` need handling.

## Identifiers available in the files

The user's rule is to identify by content, not filename.

- **Existing MusicBrainz tags** on about 69% of library tracks. Some are stale.
- **deemix downloads** carry `ISRC`, `BARCODE` (UPC) and the label/publisher.
  UPC is worth trying against MusicBrainz release barcodes; ISRC coverage on
  MusicBrainz looked poor.
- **Scene web releases** often have the catalog number in the folder name,
  e.g. `Ken_Ishii-Syntax_Error-MDZN041-16BIT-WEB-FLAC-2026-WAVED`, plus label
  tags.
- **EAC rips** have `.log` and `.cue` files. The CD TOC can give a
  MusicBrainz disc ID.
- **Track counts and durations**, which beets' matcher uses.
- **AcoustID fingerprints** via `fpcalc` from chromaprint, for the hard cases.

## Suggested approach: test beets before building anything

beets' autotagger already does release-level matching: it scores candidate
releases on title, artist, track count and track lengths, with confidence
thresholds, and its `chroma` plugin adds AcoustID. Linuxserver publishes a
Docker image for Unraid.

1. **Configure beets identify-only.** Its library DB goes in the project's
   state directory, with:
   - `import.write: no`, `import.copy: no`, `import.move: no`
   - quiet mode, with `quiet_fallback: skip` so unmatched albums are skipped,
     not guessed
   - tuned `match.strong_rec_thresh`
   - the `chroma` plugin
   - nothing that touches tags or paths
2. **Run it on a sample:**
   - `Collection/Broadway`, which must come out per album, correctly or as
     skipped
   - the remaining staging folders
   - a few hundred library albums, mixing albums that have MBIDs (ground
     truth to measure accuracy) and albums without them
3. **Measure and report** strong-match rate, accuracy against existing MBIDs,
   skip rate and time per album over NFS. Then decide with the user whether
   beets is the engine, or whether something custom is needed.

**Cautions if anything ever writes tags:**

- beets (through mutagen/mediafile) rewrites artist and album fields to
  MusicBrainz's spelling. That may not be wanted, so ask first.
- mutagen's ID3v2.3 save is lossy on this library. It drops v2.4-only frames
  that MusicBee writes into v2.3 files (TSOP, TDOR) and re-encodes UTF-8
  frames. `genre-tagger` avoids this with raw-frame splicing
  (`genre_tagger/tags.py`); reuse that approach for any MBID writes to MP3
  or WAV.
- MusicBee caches tags. After any write, the user has to rescan tags in
  MusicBee.

## Output contract (what genre-tagger will consume)

The format is open for discussion, but genre-tagger needs, per album folder:

| Field | Meaning |
|---|---|
| `album_path` | Relative to the library or staging root. Paths may not be valid UTF-8, so store them as bytes or with surrogateescape. |
| `status` | `matched` / `review` / `not_on_musicbrainz` |
| `method` | `mbid tags` / `barcode` / `beets distance` / `acoustid` / `disc id` / `manual` |
| `confidence` | numeric or tiered |
| `mb_releasegroupid`, `mb_releaseid` | the matched release |
| `mb_albumartistids` | ordered; for collaborations, the first is the primary |
| `album_artist_names` | as MusicBrainz names them |
| `candidates` | for review rows |

A SQLite table or CSV/JSON manifest both work. genre-tagger will then key
artists by these per-album MBIDs, replacing its own name-based guessing.

## Reusable data from genre-tagger

| Path | Contents |
|---|---|
| `state/library.db` | Every library track with path (bytes, relative to the root), size, mtime, format, ID3 version, MBID/artist/album-artist/genre tags. No album or title column. Seeded from the 2-hour survey. |
| `state/staging.db` | The same for staging, plus cached MusicBrainz, Discogs and Last.fm responses per artist. |
| `genre-tagger identities` | CSV of each artist's settled MBID and how it was found (tag, exact name, title evidence, pinned). |
| `genre-tagger candidates` | Markdown review report: local albums next to MusicBrainz candidates with release lists. A useful pattern for the identify review queue. |
| `genre_tagger/sources.py` | MusicBrainz client with rate limiting and backoff (1.1 s/request, proper User-Agent), Discogs and Last.fm clients. |
| `genre_tagger/tags.py` | Raw ID3 splice writer and snapshot/verify/restore helpers. |

## Open questions for the user

1. Sample size and which albums go into the first beets test.
2. Whether identify should eventually write MBIDs into files, and which ones.
   Artist-only, or the full Picard-style set?
3. An AcoustID API key, if fingerprinting is wanted.
4. Workstation vs. running on the NAS in Docker. The NAS avoids the NFS
   per-file latency.
5. How review items should be worked: candidates report plus manual pins, the
   same flow as genre-tagger.

## Current state of genre-tagger

So the new session doesn't redo it:

- **Staging:** all 135 remaining staging tracks are genre-tagged and marked.
  Earlier batches were moved to the library by the user. Marker:
  `GENRETAGGER=YYYY-MM-DD auto|override`.
- **Library:** the genre sweep **has not been run**. It is waiting on this
  identify decision.
- **Broadway leftover:** 35 Broadway tracks in the library are back to their
  original genres. The band's new copy of *Gentlemen's Brawl* still has a
  stray "Hip Hop" from the mix-up.
- **Docs:** see `README.md` for the full tool, and the git log for history.
