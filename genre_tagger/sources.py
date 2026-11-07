"""MusicBrainz, Discogs and Last.fm clients.

Each source has its own rate limiter. Transient failures (429, 5xx, network)
back off and retry, honouring Retry-After; after the last retry the error
propagates and nothing is cached, so the next run tries again.
"""
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .scan import fold


class SourceError(Exception):
    pass


class Limiter:
    def __init__(self, interval):
        self.interval = interval
        self.lock = threading.Lock()
        self.next = 0.0

    def wait(self):
        with self.lock:
            t = time.monotonic()
            if t < self.next:
                time.sleep(self.next - t)
            self.next = max(t, self.next) + self.interval


def get_json(url, headers, limiter, tries=6):
    """(http_status, parsed JSON or None)."""
    delay = 2.0
    for attempt in range(tries):
        limiter.wait()
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < tries - 1:
                retry_after = e.headers.get('Retry-After')
                time.sleep(float(retry_after) if retry_after and retry_after.isdigit() else delay)
                delay = min(delay * 2, 120)
                continue
            body = e.read()
            try:
                return e.code, json.loads(body)
            except ValueError:
                return e.code, None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt < tries - 1:
                time.sleep(delay)
                delay = min(delay * 2, 120)
                continue
            raise SourceError(f'{url}: {e}') from e
    raise SourceError(f'{url}: retries exhausted')


# ------------------------------------------------------------ MusicBrainz

class MusicBrainz:
    BASE = 'https://musicbrainz.org/ws/2'

    def __init__(self, user_agent):
        self.h = {'User-Agent': user_agent, 'Accept': 'application/json'}
        self.limit = Limiter(1.1)  # documented limit is 1 req/s per client

    def artist(self, mbid):
        status, data = get_json(f'{self.BASE}/artist/{mbid}?inc=genres+tags+url-rels+aliases&fmt=json',
                                self.h, self.limit)
        if status == 404:
            return 'notfound', None
        if status != 200:
            raise SourceError(f'MusicBrainz artist {mbid}: HTTP {status}')
        return 'ok', data

    def search(self, name):
        """Exact-name match only. 'ok' needs exactly one artist whose name or
        alias equals the query (case/dash/space-insensitive)."""
        q = name.replace('\\', '\\\\').replace('"', '\\"')
        query = urllib.parse.quote(f'artist:"{q}" OR alias:"{q}"')
        status, data = get_json(f'{self.BASE}/artist/?query={query}&limit=25&fmt=json', self.h, self.limit)
        if status != 200:
            raise SourceError(f'MusicBrainz search {name!r}: HTTP {status}')
        want = fold(name)
        exact = [a for a in data.get('artists', [])
                 if fold(a.get('name', '')) == want
                 or any(fold(al.get('name', '')) == want for al in a.get('aliases', []) or [])]
        candidates = [{'id': a['id'], 'name': a.get('name'), 'disambiguation': a.get('disambiguation'),
                       'country': a.get('country'), 'score': a.get('score')} for a in exact]
        if len(exact) == 1:
            return 'ok', {'mbid': exact[0]['id'], 'candidates': candidates}
        if not exact:
            return 'notfound', {'candidates': []}
        return 'ambiguous', {'candidates': candidates}

    def _credited(self, entity, title, name, ids):
        """Candidate IDs credited on a release-group/recording with this exact title."""
        esc = lambda s: s.replace('\\', '\\\\').replace('"', '\\"')
        field = 'releasegroup' if entity == 'release-group' else 'recording'
        q = urllib.parse.quote(f'{field}:"{esc(strip_edition(title))}" AND artist:"{esc(name)}"')
        status, data = get_json(f'{self.BASE}/{entity}/?query={q}&limit=25&fmt=json', self.h, self.limit)
        if status != 200:
            raise SourceError(f'MusicBrainz {entity} search {title!r}: HTTP {status}')
        found = set()
        for item in data.get(f'{entity}s', []):
            if fold(strip_edition(item.get('title', ''))) != fold(strip_edition(title)):
                continue
            for credit in item.get('artist-credit', []):
                aid = (credit.get('artist') or {}).get('id')
                if aid in ids:
                    found.add(aid)
        return found

    def disambiguate(self, name, candidates, albums, titles):
        """Pick among same-name candidates using the library's own evidence:
        album titles first (release groups), then track titles (recordings),
        which also covers compilation-only appearances. 'ok' only if exactly
        one candidate is matched. At most 3 searches of each kind."""
        ids = {c['id'] for c in candidates}
        evidence = {}
        for album in albums[:3]:
            for aid in self._credited('release-group', album, name, ids):
                evidence.setdefault(aid, []).append(f'album: {album}')
        if len(evidence) != 1:
            for title in titles[:3]:
                for aid in self._credited('recording', title, name, ids):
                    evidence.setdefault(aid, []).append(f'track: {title}')
        payload = {'candidates': candidates, 'albums_tried': albums[:3], 'titles_tried': titles[:3],
                   'evidence': evidence}
        if len(evidence) == 1:
            return 'ok', {**payload, 'mbid': next(iter(evidence)), 'via': 'evidence'}
        return 'ambiguous', payload


EDITION_RE = re.compile(r'\s*[\(\[][^\)\]]*[\)\]]\s*$')


def strip_edition(title):
    """'Lost Forever (Deluxe Edition) [Remastered]' -> 'Lost Forever'"""
    prev = None
    while prev != title:
        prev, title = title, EDITION_RE.sub('', title)
    return title.strip() or prev


def discogs_ids_from_mb(mb_payload):
    """Every Discogs artist linked from the MusicBrainz artist, in order. There
    can be several: Bob Dylan links his own page and aliases like Lucky Wilbury."""
    out = []
    for rel in (mb_payload or {}).get('relations', []):
        if rel.get('type') == 'discogs':
            m = re.search(r'/artist/(\d+)', rel.get('url', {}).get('resource', ''))
            if m and int(m.group(1)) not in out:
                out.append(int(m.group(1)))
    return out


# ---------------------------------------------------------------- Discogs

DISCOGS_SUFFIX = re.compile(r'\s*\(\d+\)$')


class Discogs:
    BASE = 'https://api.discogs.com'

    def __init__(self, token, user_agent):
        self.h = {'User-Agent': user_agent, 'Authorization': f'Discogs token={token}'}
        self.limit = Limiter(1.05)  # 60/min authenticated

    def _get(self, path, **params):
        status, data = get_json(f'{self.BASE}{path}?{urllib.parse.urlencode(params)}', self.h, self.limit)
        if status == 404:
            return None
        if status != 200:
            raise SourceError(f'Discogs {path}: HTTP {status}')
        return data

    def find_artist(self, name):
        data = self._get('/database/search', q=name, type='artist', per_page=25) or {}
        want = fold(name)
        exact = [r for r in data.get('results', []) if fold(DISCOGS_SUFFIX.sub('', r.get('title', ''))) == want]
        if len(exact) == 1:
            return 'ok', exact[0]['id']
        return ('notfound' if not exact else 'ambiguous'), [r['id'] for r in exact]

    def lookup(self, mb_payload, name, allow_search):
        """Genres/styles over the artist's masters (falling back to releases).
        The Discogs artist comes from the MusicBrainz URL relation when there
        is one; a name search is used only when allow_search is set. With
        several URL relations, the one whose Discogs name matches the
        MusicBrainz name or an alias wins, else the first."""
        ids, via = discogs_ids_from_mb(mb_payload), 'musicbrainz'
        if not ids:
            if not allow_search:
                return 'notfound', {'via': None}
            status, found = self.find_artist(name)
            if status != 'ok':
                return status, {'via': 'search', 'candidates': found}
            ids, via = [found], 'search'
        primary = {fold(n) for n in (name, (mb_payload or {}).get('name')) if n}
        aliases = {fold(a['name']) for a in (mb_payload or {}).get('aliases') or [] if a.get('name')}
        fetched = [(i, a) for i in ids if (a := self._get(f'/artists/{i}')) is not None]
        did = artist = None
        # the artist's own name beats an alias (Bob Dylan's aliases include Lucky Wilbury)
        for names in (primary, aliases, None):
            for i, a in fetched:
                if names is None or fold(DISCOGS_SUFFIX.sub('', a.get('name', ''))) in names:
                    did, artist = i, a
                    break
            if artist is not None:
                break
        if artist is None:
            return 'notfound', {'id': ids[0], 'via': via}
        dname = artist.get('name', name)
        items = []
        for kind in ('master', 'release'):
            # Discogs' search does not match the "(2)" disambiguation suffix,
            # but result titles carry it, so search plain and filter exact
            data = self._get('/database/search', artist=DISCOGS_SUFFIX.sub('', dname),
                             type=kind, per_page=100) or {}
            items = [{'title': r.get('title'), 'year': r.get('year'), 'genre': r.get('genre', []),
                      'style': r.get('style', [])}
                     for r in data.get('results', [])
                     if fold(r.get('title', '').split(' - ')[0]) == fold(dname)]
            if items:
                break
        return 'ok', {'id': did, 'name': dname, 'via': via, 'items': items}


# ---------------------------------------------------------------- Last.fm

class LastFM:
    BASE = 'https://ws.audioscrobbler.com/2.0/'

    def __init__(self, api_key, user_agent):
        self.key = api_key
        self.h = {'User-Agent': user_agent}
        self.limit = Limiter(0.25)

    def top_tags(self, mbid=None, name=None):
        params = {'method': 'artist.gettoptags', 'api_key': self.key, 'format': 'json', 'autocorrect': 0}
        params.update({'mbid': mbid} if mbid else {'artist': name})
        status, data = get_json(f'{self.BASE}?{urllib.parse.urlencode(params)}', self.h, self.limit)
        if data and data.get('error') == 6:
            return 'notfound', None
        if status != 200 or not data or 'error' in data:
            raise SourceError(f'Last.fm {mbid or name!r}: HTTP {status} {data}')
        return 'ok', {'tags': [{'name': t['name'], 'count': int(t.get('count', 0))}
                               for t in data.get('toptags', {}).get('tag', [])],
                      'by': 'mbid' if mbid else 'name'}
