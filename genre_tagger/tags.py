"""Per-format tag access. The only fields this module ever changes are genre
and the GENRETAGGER marker (which says this tool set the genre).

Multi-value genre storage (matches what MusicBee already wrote in the library,
and what Navidrome/TagLib reads as multi-valued):
  FLAC     repeated GENRE Vorbis comments (via mutagen)
  ID3v2.3  one TCON frame, values null-separated, Latin-1 (UTF-16 if needed)
  ID3v2.4  one TCON frame, values null-separated, UTF-8
The ID3 version of each file is kept as it was. The marker is a GENRETAGGER
Vorbis comment, or an ID3 TXXX frame with description GENRETAGGER.

ID3 (MP3 and WAV) is edited by splicing raw bytes: every frame except TCON and
the marker is copied byte-for-byte. mutagen's own ID3
save is NOT used, because for v2.3 it drops v2.4-only frames (TSOP, TDOR, ...)
and re-encodes UTF-8 frames, both of which occur in this library. Tags with
unsynchronisation or an extended header are refused rather than risked.

snapshot() captures raw bytes of everything that must not change; head_backup()
/ restore() put the original tag region back if a write fails verification.
"""
import hashlib
import os
import re
import struct

from mutagen._util import insert_bytes, resize_bytes
from mutagen.flac import FLAC
from mutagen.id3 import ID3, ID3NoHeaderError
from mutagen.wave import WAVE, _WaveFile

FORMATS = {b'.flac': 'flac', b'.mp3': 'mp3', b'.wav': 'wav'}
EDGE = 65536          # bytes hashed at each end of the audio region
GROW_PADDING = 2048   # padding added when a tag has to grow
FRAME_ID = re.compile(rb'[A-Z0-9]{4}')
MARKER = 'GENRETAGGER'
KEEP = object()       # write_tags(marker=KEEP) leaves the marker as it is


class UnsupportedFile(Exception):
    pass


def fmt_of(path):
    return FORMATS.get(os.path.splitext(path)[1].lower())


def _vals(frame):
    return [str(x) for x in frame.text] if frame is not None else None


# ------------------------------------------------------------------ read

def read(path):
    """Fields needed for scanning. path is absolute bytes."""
    fmt = fmt_of(path)
    out = dict(format=fmt, id3_version=None, mb_artistid=None,
               albumartist=None, artist=None, album=None, title=None, genre=None, marker=None)
    p = os.fsdecode(path)
    if fmt == 'flac':
        t = FLAC(p).tags
        if t is not None:
            out['mb_artistid'] = t.get('MUSICBRAINZ_ARTISTID')
            out['albumartist'] = t.get('ALBUMARTIST') or t.get('ALBUM ARTIST')
            out['artist'] = t.get('ARTIST')
            out['album'] = t.get('ALBUM')
            out['title'] = t.get('TITLE')
            out['genre'] = t.get('GENRE')
            out['marker'] = (t.get(MARKER) or [None])[0]
        return out
    if fmt == 'mp3':
        try:
            t = ID3(p, load_v1=False)  # never merge ID3v1 values in
        except ID3NoHeaderError:
            t = None
    elif fmt == 'wav':
        t = WAVE(p).tags
    else:
        raise UnsupportedFile(path)
    if t is not None:
        out['id3_version'] = '.'.join(map(str, t.version))
        txxx = {k.split(':', 1)[1].lower(): k for k in t.keys() if k.startswith('TXXX:')}
        if 'musicbrainz artist id' in txxx:
            out['mb_artistid'] = _vals(t[txxx['musicbrainz artist id']])
        out['albumartist'] = _vals(t.get('TPE2'))
        out['artist'] = _vals(t.get('TPE1'))
        out['album'] = _vals(t.get('TALB'))
        out['title'] = _vals(t.get('TIT2'))
        out['genre'] = _vals(t.get('TCON'))
        if MARKER.lower() in txxx:
            out['marker'] = (_vals(t[txxx[MARKER.lower()]]) or [None])[0]
    return out


def read_genre(path):
    return read(path)['genre']


def read_managed(path):
    """(genre, marker): the two fields this tool writes."""
    r = read(path)
    return r['genre'], r['marker']


# ------------------------------------------------------- raw ID3 splicing

def _syncsafe(b):
    n = 0
    for x in b:
        n = (n << 7) | (x & 0x7f)
    return n


def _to_syncsafe(n):
    return bytes([(n >> 21) & 0x7f, (n >> 14) & 0x7f, (n >> 7) & 0x7f, n & 0x7f])


def parse_id3(buf):
    """Split an ID3v2.3/2.4 tag (buf starts at its header) into raw frames.
    Returns (header bytes 3-5, [(frame id, raw frame bytes)], tag size)."""
    if buf[:3] != b'ID3':
        raise UnsupportedFile('no ID3 header')
    major, flags = buf[3], buf[5]
    if major not in (3, 4):
        raise UnsupportedFile(f'ID3v2.{major} not supported')
    if flags & 0xC0 or (major == 4 and flags & 0x10):
        raise UnsupportedFile(f'ID3 tag flags {flags:#04x} (unsync/extended header/footer) not supported')
    end = 10 + _syncsafe(buf[6:10])
    if len(buf) < end:
        raise UnsupportedFile('truncated ID3 tag')

    def boundary(n):  # does a frame of size n end on a plausible boundary?
        nxt = pos + 10 + n
        return nxt <= end and (nxt + 4 > end or buf[nxt] == 0 or FRAME_ID.fullmatch(buf[nxt:nxt + 4]))

    frames, pos = [], 10
    while pos + 10 <= end and buf[pos] != 0:
        fid, raw = buf[pos:pos + 4], buf[pos + 4:pos + 8]
        if not FRAME_ID.fullmatch(fid):
            raise UnsupportedFile(f'bad ID3 frame id {fid!r} at {pos}')
        plain = int.from_bytes(raw, 'big')
        if major == 3:
            size = plain
        else:  # v2.4 sizes are syncsafe, but some writers (old iTunes) used plain ints
            size = _syncsafe(raw) if all(x < 0x80 for x in raw) else None
            if size is None or not boundary(size):
                size = plain if boundary(plain) else None
        if size is None or pos + 10 + size > end:
            raise UnsupportedFile(f'ID3 frame {fid!r} overruns the tag')
        frames.append((fid, bytes(buf[pos:pos + 10 + size])))
        pos += 10 + size
    if any(buf[pos:end]):
        raise UnsupportedFile('non-zero bytes after the last ID3 frame')
    return bytes(buf[3:6]), frames, end


def tcon_frame(genres, major):
    if major == 3:
        try:
            body = b'\x00' + b'\x00'.join(g.encode('latin-1') for g in genres)
        except UnicodeEncodeError:
            body = b'\x01' + b'\x00\x00'.join(b'\xff\xfe' + g.encode('utf-16-le') for g in genres)
        return _frame(b'TCON', body, major)
    return _frame(b'TCON', b'\x03' + b'\x00'.join(g.encode('utf-8') for g in genres), major)


def _frame(fid, body, major):
    size = struct.pack('>I', len(body)) if major == 3 else _to_syncsafe(len(body))
    return fid + size + b'\x00\x00' + body


def marker_frame(value, major):
    """TXXX, description GENRETAGGER. Latin-1 is valid in both v2.3 and v2.4."""
    return _frame(b'TXXX', b'\x00' + MARKER.encode('latin-1') + b'\x00' + value.encode('latin-1'), major)


def is_marker_frame(fid, raw):
    if fid != b'TXXX' or len(raw) < 11:
        return False
    enc, body = raw[10], raw[11:]
    if enc in (0, 3):
        desc = body.split(b'\x00', 1)[0].decode('latin-1' if enc == 0 else 'utf-8', 'replace')
    else:  # UTF-16: description ends at the first aligned double null
        end = next((i for i in range(0, len(body) - 1, 2) if body[i:i + 2] == b'\x00\x00'), len(body))
        desc = body[:end].decode('utf-16' if enc == 1 else 'utf-16-be', 'replace')
    return desc.upper() == MARKER


def spliced_tag(old, genres, min_size, marker=KEEP):
    """New tag bytes: old frames verbatim with TCON (and, unless marker is
    KEEP, the marker TXXX) replaced; None removes. old is None when there is
    no tag (a v2.3 tag is created). Fits in min_size when it can."""
    if old is None:
        ver_flags, frames = b'\x03\x00\x00', []
    else:
        ver_flags, frames, _ = parse_id3(old)
    major = ver_flags[0]
    new, placed = [], False
    for fid, raw in frames:
        if fid == b'TCON':
            if not placed and genres is not None:
                new.append(tcon_frame(genres, major))
            placed = True
            continue
        if marker is not KEEP and is_marker_frame(fid, raw):
            continue
        new.append(raw)
    if not placed and genres is not None:
        new.append(tcon_frame(genres, major))
    if marker is not KEEP and marker is not None:
        new.append(marker_frame(marker, major))
    body = b''.join(new)
    total = min_size if 10 + len(body) <= min_size else 10 + len(body) + GROW_PADDING
    return b'ID3' + ver_flags + _to_syncsafe(total - 10) + body + b'\x00' * (total - 10 - len(body))


def _id3v2_size(fh):
    fh.seek(0)
    hdr = fh.read(10)
    if len(hdr) < 10 or hdr[:3] != b'ID3':
        return 0
    return 10 + _syncsafe(hdr[6:10]) + (10 if hdr[5] & 0x10 else 0)


def _wav_id3_chunk(fh):
    for cid, off, size in _riff_chunks(fh):
        if cid.strip().lower() == b'id3':
            return off, size
    return None


# ----------------------------------------------------------------- write

def write_genre(path, genres):
    write_tags(path, genres)


def write_tags(path, genres, marker=KEEP):
    """Set (or, for None, remove) the genre and the marker. Nothing else."""
    fmt = fmt_of(path)
    if fmt == 'flac':
        f = FLAC(os.fsdecode(path))
        if f.tags is None:
            f.add_tags()
        for field, value in (('GENRE', genres), (MARKER, KEEP if marker is KEEP else
                                                  None if marker is None else [marker])):
            if value is KEEP:
                continue
            if value is None:
                if field in f.tags:
                    del f.tags[field]
            else:
                f.tags[field] = list(value)
        f.save()
    elif fmt == 'mp3':
        with open(path, 'r+b') as fh:
            old_size = _id3v2_size(fh)
            fh.seek(0)
            old = fh.read(old_size) if old_size else None
            if old is None and genres is None and marker in (KEEP, None):
                return
            new = spliced_tag(old, genres, old_size, marker)
            if len(new) > old_size:
                insert_bytes(fh, len(new) - old_size, old_size)
            fh.seek(0)
            fh.write(new)
    elif fmt == 'wav':
        with open(path, 'r+b') as fh:
            loc = _wav_id3_chunk(fh)
            if loc is None:
                if genres is None and marker in (KEEP, None):
                    return
                old, off, size = None, None, 0
            else:
                off, size = loc
                fh.seek(off)
                old = fh.read(size)
            new = spliced_tag(old, genres, size, marker)
            if loc is not None and len(new) == size:
                fh.seek(off)
                fh.write(new)
                return
            wf = _WaveFile(fh)
            if loc is None:
                wf.insert_chunk('id3')
            chunk = wf['id3']
            chunk.resize(len(new))
            chunk.write(new)
    else:
        raise UnsupportedFile(path)


# ------------------------------------------------------ snapshot / restore

def _h(b):
    return hashlib.sha1(b).hexdigest()


def _region_fp(fh, start, end):
    fh.seek(start)
    head = fh.read(min(EDGE, end - start))
    fh.seek(max(start, end - EDGE))
    tail = fh.read(end - max(start, end - EDGE))
    return (end - start, _h(head + tail))


def _flac_blocks(fh):
    """[(type, raw block bytes)] and the audio start offset."""
    start = _id3v2_size(fh)
    fh.seek(start)
    if fh.read(4) != b'fLaC':
        raise UnsupportedFile('no fLaC marker')
    blocks = []
    while True:
        h = fh.read(4)
        if len(h) < 4:
            raise UnsupportedFile('truncated FLAC metadata')
        ln = int.from_bytes(h[1:4], 'big')
        blocks.append((h[0] & 0x7f, fh.read(ln)))
        if h[0] & 0x80:
            return blocks, fh.tell()


def _riff_chunks(fh):
    """(id, data_offset, size) for each top-level RIFF chunk."""
    fh.seek(0)
    riff = fh.read(12)
    if riff[:4] != b'RIFF':
        raise UnsupportedFile('not RIFF')
    out, pos = [], 12
    fh.seek(0, 2)
    end = fh.tell()
    while pos + 8 <= end:
        fh.seek(pos)
        cid, size = struct.unpack('<4sI', fh.read(8))
        out.append((cid, pos + 8, size))
        pos += 8 + size + (size & 1)
    return out


def _id3_frames_fp(buf):
    if buf is None:
        return None
    ver_flags, frames, _ = parse_id3(buf)
    return ver_flags, [(fid.decode(), _h(raw)) for fid, raw in frames
                       if fid != b'TCON' and not is_marker_frame(fid, raw)]


def snapshot(path):
    """Raw-byte fingerprint of everything except the genre: all other tags and
    artwork, non-tag metadata, ID3 version/flags, and the audio region (length +
    hash of its first/last 64 KiB; for MP3 that includes any ID3v1/APE tail)."""
    fmt = fmt_of(path)
    snap = {'format': fmt}
    with open(path, 'rb') as fh:
        fh.seek(0, 2)
        end = fh.tell()
        if fmt == 'flac':
            blocks, audio = _flac_blocks(fh)
            snap['blocks'] = [(t, _h(b)) for t, b in blocks if t not in (1, 4)]  # minus padding, comments
            snap['audio'] = _region_fp(fh, audio, end)
        elif fmt == 'mp3':
            size = _id3v2_size(fh)
            fh.seek(0)
            snap['id3'] = _id3_frames_fp(fh.read(size) if size else None)
            snap['audio'] = _region_fp(fh, size, end)
        elif fmt == 'wav':
            chunks = []
            for cid, off, size in _riff_chunks(fh):
                fh.seek(off)
                if cid.strip().lower() == b'id3':
                    snap['id3'] = _id3_frames_fp(fh.read(size))
                elif cid == b'data':
                    chunks.append(('data',) + _region_fp(fh, off, off + size))
                else:
                    chunks.append((cid.decode('latin-1'), size, _h(fh.read(size))))
            snap.setdefault('id3', None)
            snap['chunks'] = chunks
        else:
            raise UnsupportedFile(path)
    if fmt == 'flac':  # Vorbis comments other than GENRE and the marker, plus the vendor string
        t = FLAC(os.fsdecode(path)).tags
        snap['comments'] = (t.vendor if t is not None else None,
                            sorted((k.lower(), v) for k, v in (t or []) if k.lower() not in ('genre', MARKER.lower())))
    return snap


def head_backup(path):
    """The original tag region, kept in memory so a failed write can be undone."""
    fmt = fmt_of(path)
    with open(path, 'rb') as fh:
        if fmt == 'flac':
            _, audio = _flac_blocks(fh)
            fh.seek(0)
            return fmt, fh.read(audio), None
        if fmt == 'mp3':
            size = _id3v2_size(fh)
            fh.seek(0)
            head = fh.read(size)
            fh.seek(0, 2)
            tail = None
            if fh.tell() >= 128:
                fh.seek(-128, 2)
                t = fh.read(128)
                tail = t if t[:3] == b'TAG' else None
            return fmt, head, tail
        if fmt == 'wav':
            loc = _wav_id3_chunk(fh)
            if loc is None:
                return fmt, None, None
            fh.seek(loc[0])
            return fmt, fh.read(loc[1]), None
    raise UnsupportedFile(path)


def restore(path, backup):
    fmt, head, tail = backup
    with open(path, 'r+b') as fh:
        if fmt in ('flac', 'mp3'):
            cur = _flac_blocks(fh)[1] if fmt == 'flac' else _id3v2_size(fh)
            resize_bytes(fh, cur, len(head), 0)
            fh.seek(0)
            fh.write(head)
            if tail is not None:
                fh.seek(-128, 2)
                if fh.read(3) == b'TAG':
                    fh.seek(-128, 2)
                    fh.write(tail)
        else:
            wf = _WaveFile(fh)
            if head is None:
                if 'id3' in wf:
                    wf['id3'].delete()
            else:
                chunk = wf['id3']
                chunk.resize(len(head))
                chunk.write(head)
