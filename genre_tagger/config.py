"""Settings come from the environment, optionally seeded from a .env file.

Everything path-like is configurable so the same code runs on the workstation
(NFS mount) and in a container on the NAS (bind mounts).
"""
import os
import re
from dataclasses import dataclass
from pathlib import Path

from . import __version__


def load_dotenv(path):
    """Minimal .env reader: KEY=VALUE lines, # comments, optional quotes.
    Values already set in the environment win."""
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        k, v = k.strip().removeprefix('export ').strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in '"\'':
            v = v[1:-1]
        else:
            v = re.split(r'\s+#', v, maxsplit=1)[0].strip()  # inline comment
        os.environ.setdefault(k, v)


@dataclass
class Settings:
    library: Path
    db: Path
    reports: Path
    discogs_token: str | None
    lastfm_key: str | None
    contact: str | None
    top_n: int
    lastfm_min_count: int
    min_score: float
    workers: int
    vocab_db: Path | None = None  # another state DB whose genre spellings to follow (the library's, for an inbox)
    hierarchy: str = ''           # MusicBee tag hierarchy file limiting genres; '' = bundled RYM one, 'off' = none
    folders: tuple = ()           # subfolders of library to scan; empty = all of it
    include_multi: bool = False   # overwrite tracks that already have 2+ genres (staging: source genres only)

    @property
    def user_agent(self):
        contact = self.contact or 'no contact configured'
        return f'genre-tagger/{__version__} ( {contact} )'


def settings(env_file='.env', **overrides):
    load_dotenv(os.environ.get('GT_ENV_FILE', env_file))
    e = os.environ.get
    s = Settings(
        library=Path(e('GT_LIBRARY', '/music')),
        db=Path(e('GT_DB', 'genre_tagger.db')),
        reports=Path(e('GT_REPORTS', 'reports')),
        discogs_token=e('DISCOGS_TOKEN') or None,
        lastfm_key=e('LASTFM_API_KEY') or None,
        contact=e('GT_CONTACT') or None,
        top_n=int(e('GT_TOP_N', '6')),
        lastfm_min_count=int(e('GT_LASTFM_MIN_COUNT', '20')),
        min_score=float(e('GT_MIN_SCORE', '0.6')),
        workers=int(e('GT_WORKERS', '2')),
        vocab_db=Path(e('GT_VOCAB_DB')) if e('GT_VOCAB_DB') else None,
        hierarchy=e('GT_HIERARCHY', ''),
        folders=tuple(f.strip() for f in e('GT_FOLDERS', '').split(',') if f.strip()),
        include_multi=e('GT_INCLUDE_MULTI', '').lower() in ('1', 'true', 'yes', 'on'),
    )
    for k, v in overrides.items():
        if v is not None:
            setattr(s, k, Path(v) if k in ('library', 'db', 'reports', 'vocab_db') else v)
    return s
