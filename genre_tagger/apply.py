"""apply / rollback. Both are dry runs unless --apply is given.

For every file written:
  1. the genre currently on disk is recorded in `originals` the first time
     this tool touches the file (later runs never overwrite it)
  2. a snapshot of everything except genre and marker is taken (tags.snapshot)
  3. the genre and the GENRETAGGER marker ("2026-11-06 auto") are written;
     rollback removes the marker
  4. the file is re-read: genre and marker must equal the target and the
     snapshot must be identical; the result is recorded in `writes`
The run stops at the first verification failure. Files already holding the
target genre are skipped, so an interrupted run can simply be re-run.
"""
import csv
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import tags
from .db import dumps, loads, now
from .scan import sparse


def _undo(path, backup, before):
    """Put the original tag region back and say whether the file is whole again."""
    try:
        tags.restore(path, backup)
        if tags.snapshot(path) == before:
            return 'original tag restored and re-verified'
        return 'RESTORE INCOMPLETE: file still differs from its pre-write snapshot'
    except Exception as e:
        return f'RESTORE FAILED ({type(e).__name__}: {e})'


def _abs(root, rel):
    return os.fsencode(str(root)).rstrip(b'/') + b'/' + rel


def _fmt(g):
    return '(no tag)' if g is None else '; '.join(g) if g else '(empty)'


def marker_value(status, when=None):
    """GENRETAGGER tag value: date of the write, and how the genres were chosen."""
    return f"{time.strftime('%Y-%m-%d', time.localtime(when))} {'override' if status == 'override' else 'auto'}"


def apply_targets(db, artist_filter=None, limit=None, include_multi=False, hierarchy=None):
    """(relpath, target genres, artist name, current genres, target marker) for
    tracks whose genre differs from the resolved proposal, plus marker-only
    targets: tracks that already hold the proposed genres but have no marker
    (so an empty marker means "not confirmed by the tool"), and tracks this
    tool wrote before markers existed (dated by the original write).
    Only artists with status ok/override are included. Tracks already holding
    2+ genres (counted per scan.sparse) that this tool didn't write are left
    alone unless include_multi."""
    q = """SELECT t.relpath, t.genre, r.genres, r.status, a.name, a.key, w.genre, w.written_at, w.marker
           FROM tracks t
           JOIN resolved r ON r.artist_key = t.artist_key
           JOIN artists a ON a.key = t.artist_key
           LEFT JOIN writes w ON w.relpath = t.relpath
           WHERE r.status IN ('ok','override') AND t.error IS NULL
           ORDER BY a.name COLLATE NOCASE, t.relpath"""
    keys_seen, out = [], []
    for rel, cur, target, status, name, key, wrote, written_at, marker in db.execute(q):
        if artist_filter and artist_filter.casefold() not in name.casefold() and artist_filter != key:
            continue
        if key not in keys_seen:
            if limit and len(keys_seen) >= limit:
                break
            keys_seen.append(key)
        target_list = loads(target)
        if wrote is None and not include_multi and not sparse(cur, hierarchy) and loads(cur) != target_list:
            continue
        target, cur = target_list, loads(cur)
        if cur != target:
            out.append((rel, target, name, cur, marker_value(status)))
        elif wrote is None:
            out.append((rel, target, name, cur, marker_value(status)))  # already right: confirm it
        elif marker is None:
            out.append((rel, target, name, cur, marker_value(status, written_at)))
    return out


def rollback_targets(db, artist_filter=None):
    q = """SELECT w.relpath, o.genre, a.name, t.genre FROM writes w
           JOIN originals o ON o.relpath = w.relpath
           LEFT JOIN tracks t ON t.relpath = w.relpath
           LEFT JOIN artists a ON a.key = t.artist_key
           ORDER BY a.name COLLATE NOCASE, w.relpath"""
    out = []
    for rel, orig, name, cur in db.execute(q):
        if artist_filter and artist_filter.casefold() not in (name or '').casefold():
            continue
        out.append((rel, loads(orig), name or '?', loads(cur), None))  # None: remove the marker
    return out


def plan_report(targets, reports_dir, kind):
    """Per-artist summary of what a run would change."""
    reports_dir = Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f'{kind}-plan-{time.strftime("%Y%m%d-%H%M%S")}.csv'
    per = {}
    for rel, target, name, cur, marker in targets:
        e = per.setdefault(name, {'old': {}, 'new': target, 'n': 0, 'marker_only': 0})
        e['old'][_fmt(cur)] = e['old'].get(_fmt(cur), 0) + 1
        e['n'] += 1
        e['marker_only'] += kind == 'apply' and cur == target
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['artist', 'tracks_to_change', 'of_which_marker_only', 'old_genres (tracks)', 'new_genres'])
        for name, e in per.items():
            old = ' | '.join(f'{g} ({n})' for g, n in sorted(e['old'].items(), key=lambda kv: -kv[1]))
            w.writerow([name, e['n'], e['marker_only'], old,
                        _fmt(e['new']) if kind == 'apply' else '(original per file)'])
    return path, len(per)


def backup_originals(db, targets, reports_dir):
    """CSV copy of the original genre of every file about to be written."""
    Path(reports_dir).mkdir(parents=True, exist_ok=True)
    path = Path(reports_dir) / f'backup-originals-{time.strftime("%Y%m%d-%H%M%S")}.csv'
    rels = [t[0] for t in targets]
    with open(path, 'w', newline='', encoding='utf-8', errors='surrogateescape') as f:
        w = csv.writer(f)
        w.writerow(['relpath', 'original_genre_json'])
        for rel in rels:
            r = db.execute("SELECT genre FROM originals WHERE relpath=?", (rel,)).fetchone()
            w.writerow([rel.decode('utf-8', 'surrogateescape'), r[0] if r and r[0] is not None else 'null'])
    return path


def execute(db, settings, targets, mode):
    """mode: 'apply' or 'rollback'. Returns a summary Counter-like dict."""
    lock = threading.Lock()
    root = settings.library
    stop = threading.Event()
    result = {'written': 0, 'already': 0, 'cancelled': 0, 'errors': [], 'verify_failed': []}

    if mode == 'apply':
        # Seed originals from the scan before anything is written, then back them up.
        with lock:
            for rel, _, _, cur, _ in targets:
                db.execute("INSERT OR IGNORE INTO originals VALUES (?,?,?)", (rel, dumps(cur), now()))
            db.commit()
        print(f'original genres backed up to {backup_originals(db, targets, settings.reports)}', flush=True)

    def one(item):
        rel, target, name, _, marker = item
        if stop.is_set():
            return 'cancelled', rel, None
        path = _abs(root, rel)
        try:
            current, cur_marker = tags.read_managed(path)
            with lock:
                written_before = db.execute("SELECT 1 FROM writes WHERE relpath=?", (rel,)).fetchone()
                if mode == 'apply' and not written_before:
                    # the file may have changed since the scan; record what is really there
                    db.execute("UPDATE originals SET genre=?, captured_at=? WHERE relpath=?",
                               (dumps(current), now(), rel))
                    db.commit()
            # apply: a file that already has the genre keeps its marker if it has one
            if current == target and (cur_marker == marker or (mode == 'apply' and cur_marker)):
                with lock:
                    if mode == 'rollback':
                        db.execute("DELETE FROM writes WHERE relpath=?", (rel,))
                    elif db.execute("SELECT 1 FROM writes WHERE relpath=?", (rel,)).fetchone():
                        db.execute("UPDATE writes SET marker=? WHERE relpath=?", (cur_marker, rel))
                    db.execute("UPDATE tracks SET genre=? WHERE relpath=?", (dumps(current), rel))
                    db.commit()
                return 'already', rel, None
            before = tags.snapshot(path)
            backup = tags.head_backup(path)
            try:
                tags.write_tags(path, target, marker)
                after = tags.snapshot(path)
                got, got_marker = tags.read_managed(path)
            except Exception as e:
                stop.set()
                return 'verify_failed', rel, f'write failed ({type(e).__name__}: {e}); ' + _undo(path, backup, before)
            problems = []
            if got != target:
                problems.append(f'genre reads back as {got!r}, expected {target!r}')
            if got_marker != marker:
                problems.append(f'{tags.MARKER} reads back as {got_marker!r}, expected {marker!r}')
            for k in before:
                if before[k] != after.get(k):
                    problems.append(f'{k} changed')
            if problems:
                problems.append(_undo(path, backup, before))
                got = tags.read_genre(path)
            st = os.stat(path)
            with lock:
                if mode == 'apply' and not problems:
                    # a marker-only write keeps the original write time
                    db.execute("""INSERT INTO writes (relpath, genre, written_at, verified, error, marker)
                                  VALUES (?,?,?,1,NULL,?)
                                  ON CONFLICT(relpath) DO UPDATE SET marker=excluded.marker, verified=1,
                                    written_at=CASE WHEN writes.genre=excluded.genre THEN writes.written_at
                                                    ELSE excluded.written_at END,
                                    genre=excluded.genre""",
                               (rel, dumps(target), now(), marker))
                elif mode == 'rollback' and not problems:
                    db.execute("DELETE FROM writes WHERE relpath=?", (rel,))
                db.execute("UPDATE tracks SET genre=?, size=?, mtime=? WHERE relpath=?",
                           (dumps(got), st.st_size, st.st_mtime, rel))
                db.commit()
            if problems:
                stop.set()
                return 'verify_failed', rel, '; '.join(problems)
            return 'written', rel, None
        except Exception as e:
            return 'error', rel, f'{type(e).__name__}: {e}'

    t0 = time.time()
    with ThreadPoolExecutor(settings.workers) as ex:
        futures = [ex.submit(one, t) for t in targets]
        for i, fut in enumerate(as_completed(futures), 1):
            status, rel, detail = fut.result()
            if status in ('written', 'already', 'cancelled'):
                result[status] += 1
            elif status == 'error':
                result['errors'].append((rel, detail))
            elif status == 'verify_failed':
                result['verify_failed'].append((rel, detail))
            if i % 250 == 0:
                print(f'  {i}/{len(targets)} ({i / (time.time() - t0):.1f} files/s)', flush=True)
    return result
