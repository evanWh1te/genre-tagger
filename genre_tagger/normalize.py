"""Genre name normalization.

canon(raw) turns one raw tag from any source into zero or more canonical
genre names:
  1. true synonyms are mapped (R&B, Drum & Bass, Discogs catch-alls, ...)
  2. compound values are split on ; / , |  -- Navidrome splits genre on
     ; / , by default, so an output name must never contain them
  3. junk is dropped (seen live, favourites, decades, nationalities, ...)
  4. spelling/casing comes from the library's own most common spelling when
     the genre already exists there, otherwise a smart title case

Spelling variants that differ only by case, spaces or hyphens
("Post-Hardcore", "post hardcore", "posthardcore") share a key and merge.
"""
import collections
import re
import unicodedata

SPLIT_RE = re.compile(r'\s*(?:;|/|,|\|)\s*')
HIERARCHY_RE = re.compile(r'[^;,|]*→')
DECADE_PREFIX_RE = re.compile(r"^(?:(?:19|20)?\d0'?s)\s+(.+)$", re.I)  # "80s Punk" -> "Punk"


def key(s):
    """'Drum & Bass', 'drum and bass', 'Drum-and-Bass' -> 'drumandbass'"""
    s = unicodedata.normalize('NFKC', s).casefold().replace('&', ' and ')
    return re.sub(r'\W+', '', s).replace('_', '')


# key -> canonical name(s); [] drops the value
SYNONYMS = {
    'rnb': ['R&B'], 'randb': ['R&B'],
    'drumnbass': ['Drum & Bass'], 'dnb': ['Drum & Bass'], 'dandb': ['Drum & Bass'],
    'electronica': ['Electronic'], 'electronicmusic': ['Electronic'],
    'rocknroll': ['Rock & Roll'], 'rockandroll': ['Rock & Roll'],
    'altrock': ['Alternative Rock'], 'altmetal': ['Alternative Metal'],
    'soundtracks': ['Soundtrack'], 'originalsoundtrack': ['Soundtrack'], 'ost': ['Soundtrack'],
    'filmsoundtrack': ['Soundtrack'],
    'hiphoprap': ['Hip Hop', 'Rap'], 'singersongwriter': ['Singer-Songwriter'],
    # Discogs top-level genres that are catch-alls or contain separators
    'folkworldandcountry': [], 'stageandscreen': ['Soundtrack'], 'nonmusic': [],
    'funksoul': ['Funk', 'Soul'],
    'conscious': ['Conscious Hip Hop'],  # Discogs Hip Hop style
}

COUNTRIES = """american usa us unitedstates british uk english scottish irish welsh
canadian canada australian australia german germany deutsch french france swedish sweden
norwegian norway finnish finland danish denmark icelandic iceland dutch netherlands belgian
italian italy spanish spain portuguese portugal brazilian brazil mexican mexico argentinian
argentina japanese japan korean korea southkorea chinese china russian russia polish poland
ukrainian ukraine greek greece turkish turkey israeli indian india african southafrican
nigerian jamaican jamaica cuban newzealand austrian austria swiss czech hungarian estonian
latvian lithuanian serbian croatian romanian bulgarian colombian chilean peruvian filipino
indonesian thai vietnamese european scandinavian nordic latin""".split()
COUNTRIES = set(COUNTRIES) - {'latin'}  # Latin is a genre
# non-English nationality forms that show up in user tags
COUNTRIES |= {'américain', 'americain', 'français', 'francais', 'française', 'allemand', 'britannique',
              'canadien', 'japonais', 'suédois', 'norsk', 'svensk', 'suomi', 'español', 'italiano',
              'brasileiro', 'deutschland', 'nederlands', 'polska', 'russe'}

JUNK_KEYS = {
    'seenlive', 'favorite', 'favorites', 'favourite', 'favourites', 'love', 'loved',
    'awesome', 'beautiful', 'amazing', 'albumsiown', 'checkout', 'owned', 'vinyl', 'cd',
    'femalevocalists', 'malevocalists', 'femalevocalist', 'malevocalist', 'femalevocals',
    'malevocals', 'vocalists', 'live', 'cover', 'covers', 'all', 'other', 'misc', 'unknown',
    'music', 'genre', 'various', 'variousartists', 'none', 'spotify', 'seeninconcert',
    'tolisten', 'good', 'great', 'cool', 'chill', 'sexy', 'fun',
    'classicpopandrock', 'popandchart', 'rockandindie',  # BBC-import buckets on MusicBrainz
}
JUNK_RE = re.compile(r"""^(?:
      \d0s | \d{4}s | (?:19|20)\d\d.* | \d0 | \d{2}s        # decades / years, "2008 universal fire victim"
    | \d+(?:st|nd|rd|th)century                              # "20th century"
    | my(?:favou?rite|top|music|collection|library).* | bestof.*
    | favou?rite.* | under\d+.* | seenlive.*                 # personal tags
    )$""", re.X)

ACRONYMS = {'idm': 'IDM', 'ebm': 'EBM', 'edm': 'EDM', 'uk': 'UK', 'us': 'US', 'dj': 'DJ',
            'nwobhm': 'NWOBHM', 'dsbm': 'DSBM', 'aor': 'AOR', 'mpb': 'MPB', 'r&b': 'R&B',
            'ost': 'OST', 'nyhc': 'NYHC', 'ukg': 'UKG'}


def smart_title(s):
    s = re.sub(r'\s+', ' ', s.strip())
    parts = re.split(r'([ \-])', s)
    out = []
    for p in parts:
        if p in (' ', '-') or not p:
            out.append(p)
        elif p.lower() in ACRONYMS:
            out.append(ACRONYMS[p.lower()])
        elif p.lower() == 'and' and out:
            out.append('and')
        else:
            out.append(p[0].upper() + p[1:].lower())
    return ''.join(out)


def is_junk(raw, artist_name=None):
    k = key(raw)
    if not k or k in JUNK_KEYS or k in COUNTRIES or JUNK_RE.match(k):
        return True
    return artist_name is not None and k == key(artist_name)


class Normalizer:
    def __init__(self, vocabulary=(), fallback=(), hierarchy=None):
        """vocabulary: iterable of (spelling, count) already used in the library.
        fallback: spellings to use for genres the library doesn't have yet
        (MusicBrainz's curated genre names), ahead of whatever a source sent.
        hierarchy: a hierarchy.Hierarchy; when given, only its genres come out,
        spelled its way, and canon(dropped=[...]) collects the rest."""
        self.hierarchy = hierarchy
        best = collections.defaultdict(collections.Counter)
        for spelling, n in vocabulary:
            for part in SPLIT_RE.split(spelling):
                part = part.strip()
                if key(part):
                    best[key(part)][part] += n
        self.spelling = {k: c.most_common(1)[0][0] for k, c in best.items()}
        for name in fallback:
            self.spelling.setdefault(key(name), smart_title(name))

    def display(self, raw):
        # first spelling seen wins for a new genre, so "Hip-Hop" and "Hip Hop"
        # from different sources still become one genre
        return self.spelling.setdefault(key(raw), smart_title(raw))

    def canon(self, raw, artist_name=None, dropped=None):
        raw = unicodedata.normalize('NFKC', raw).strip()
        # Qobuz/AllMusic-style hierarchy "Pop/Rock→Rock→Metal": keep the leaf
        raw = HIERARCHY_RE.sub('', raw).strip()
        if not raw:
            return []
        k = key(raw)
        if self.hierarchy and k not in SYNONYMS and not SPLIT_RE.search(raw):
            # whole RYM names first, so "Hip Hop Rap"-style synonyms don't eat them
            hit = self.hierarchy.lookup(raw)
            if hit is not None and not is_junk(raw, artist_name):
                return [hit]
        if k in SYNONYMS:
            out = []
            for g in SYNONYMS[k]:
                for h in self._out(g, dropped):
                    if h not in out:
                        out.append(h)
            return out
        if SPLIT_RE.search(raw):
            out = []
            for part in SPLIT_RE.split(raw):
                for g in self.canon(part, artist_name, dropped):
                    if g not in out:
                        out.append(g)
            return out
        m = DECADE_PREFIX_RE.match(raw)
        if m:
            return self.canon(m.group(1), artist_name, dropped)
        if is_junk(raw, artist_name):
            return []
        return self._out(raw, dropped)

    def _out(self, name, dropped):
        if not self.hierarchy:
            return [self.display(name)]
        hit = self.hierarchy.lookup(name)
        if hit is None:
            if dropped is not None:
                dropped.append(self.display(name))
            return []
        return [hit]
