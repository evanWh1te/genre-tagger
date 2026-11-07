"""RateYourMusic genre dictionary, read from a MusicBee tag hierarchy template.

The template (https://github.com/FlakyBlueJay/mb-rym-hierarchy, bundled as
data/rym-hierarchy.txt, commit 6773def of 2026-11-02) is an indented tree:
group lines, and leaves "Name::genre" / "Name::mood". The Genres and Scenes &
Movements trees both map to the Genre tag, so every ::genre leaf is allowed.

When a hierarchy is loaded, a genre is only ever written if it is in it:
  - exact name (same key() as normalize uses), RYM spelling wins
  - else an alias below ("Punk" -> "Punk Rock", "Thrash" -> "Thrash Metal")
  - a name with bracketed RYM variants ("Hardcore" -> "Hardcore [EDM]" or the
    punk one; "Trap" -> "Trap" (hip hop) or "Trap [EDM]"): returned as
    Ambiguous, settled per artist by which top-level groups the artist's
    other genres sit in
  - else dropped
Names containing ; / , ("R&B / Soul / Funk") are category nodes and are
never written, because Navidrome splits on those characters.
"""
import collections
import re
from pathlib import Path

from .normalize import key

BUNDLED = Path(__file__).parent / 'data' / 'rym-hierarchy.txt'

# source spelling (by key) -> RYM name, for tags RYM calls something else
ALIASES = {
    'punk': 'Punk Rock', 'rap': 'Hip Hop', 'thrash': 'Thrash Metal', 'progrock': 'Progressive Rock',
    'prog': 'Progressive Rock', 'surf': 'Surf Rock', 'psychedelic': 'Psychedelia', 'crust': 'Crust Punk',
    'fusion': 'Jazz Fusion', 'minimal': 'Minimal Techno', 'classical': 'Classical Music',
    'edm': 'Electronic Dance Music', 'breaks': 'Breakbeat', 'motown': 'Motown Sound',
    'drumnbass': 'Drum and Bass', 'dnb': 'Drum and Bass', 'modernelectricblues': 'Electric Blues',
    'score': 'Film Score', 'filmscore': 'Film Score', 'originalscore': 'Film Score',
    'filmscores': 'Film Score', 'soundtrack': 'Soundtrack', 'ost': 'Soundtrack',
    'originalsoundtrack': 'Soundtrack', 'videogamesoundtrack': 'Video Game Music',
    'gamesoundtrack': 'Video Game Music', 'videogame': 'Video Game Music', 'electronica': 'Electronic',
    'nwobhm': 'New Wave of British Heavy Metal', 'singerandsongwriter': 'Singer-Songwriter',
    'drumcorps': 'Drum and Bugle Corps', 'dci': 'Drum and Bugle Corps',
    'romantic': 'Romanticism', 'romanticclassical': 'Romanticism', 'romanticera': 'Romanticism',
    'glam': 'Glam Rock', 'jazzyhiphop': 'Jazz Rap', 'jazzhiphop': 'Jazz Rap', 'chillhop': 'Lo-Fi Hip Hop',
}
# for a bracketed name, what to write instead (RYM tags releases with the leaf)
BRACKET_DISPLAY = {'Hardcore [Punk]': 'Hardcore Punk'}
BRACKET_RE = re.compile(r'\s*\[[^\]]+\]$')
FORBIDDEN = re.compile(r'[;/,]')


class Ambiguous(str):
    """A source tag that matches several bracketed RYM names."""
    variants = ()


class Hierarchy:
    def __init__(self, path):
        self.path = Path(path)
        self.names = {}                              # key -> RYM name
        self.groups = collections.defaultdict(set)   # RYM name -> top-level groups
        self.bracketed = collections.defaultdict(list)  # key of base name -> bracketed names
        section, stack = None, []
        for line in self.path.read_text(encoding='utf-8-sig').splitlines():
            if not line.strip():
                continue
            depth = (len(line) - len(line.lstrip(' '))) // 4
            text = line.strip()
            if depth == 0:
                section, stack = text, []
                continue
            if section not in ('Genres', 'Scenes & Movements'):
                continue
            name, _, field = text.partition('::')
            stack[depth - 1:] = [name]
            if field != 'genre' or FORBIDDEN.search(name):
                continue
            k = key(name)
            # "Hardcore [Punk]" and "Hardcore Punk" share a key: the plain one wins
            if k not in self.names or '[' in self.names[k]:
                self.names[k] = name
            top = stack[0] if section == 'Genres' else None
            if top:
                self.groups[name].add(top)
            if BRACKET_RE.search(name):
                self.bracketed[key(BRACKET_RE.sub('', name))].append(name)
        for name in list(self.groups):  # a bracketed leaf's display name inherits its groups
            if name in BRACKET_DISPLAY:
                self.groups[BRACKET_DISPLAY[name]] |= self.groups[name]
        for k, v in self.bracketed.items():
            self.bracketed[k] = sorted(set(v))

    def __len__(self):
        return len(set(self.names.values()))

    def lookup(self, raw):
        """RYM name for raw, an Ambiguous, or None."""
        k = key(raw)
        if k in ALIASES:
            k = key(ALIASES[k])
        variants = self.bracketed.get(k)
        if k in self.names and variants and self.names[k] not in variants:
            variants = variants + [self.names[k]]  # "Trap" (hip hop) vs "Trap [EDM]": decide by context
        elif k in self.names:
            return BRACKET_DISPLAY.get(self.names[k], self.names[k])
        if variants:
            if len(variants) == 1:
                return BRACKET_DISPLAY.get(variants[0], variants[0])
            amb = Ambiguous(raw)
            amb.variants = tuple(BRACKET_DISPLAY.get(v, v) for v in variants)
            return amb
        return None

    def settle(self, amb, scores):
        """Pick the variant whose top-level groups hold the most of the
        artist's other genre score; None when nothing points either way."""
        best, best_w = None, 0.0
        for v in amb.variants:
            w = sum(s for g, s in scores.items() if self.groups.get(g, set()) & self.groups.get(v, set()))
            if w > best_w:
                best, best_w = v, w
            elif w == best_w:
                best = None if w else best
        return best


def load(path):
    """path: a hierarchy file, '' for the bundled one, or 'off'."""
    if str(path).lower() in ('off', 'none', 'false', '0'):
        return None
    return Hierarchy(path or BUNDLED)
