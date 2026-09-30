"""Resolve a Spotify episode to its public full-length RSS enclosure (#1238).

Spotify previews/encrypted streams are deliberately never audio sources. Public
embed metadata identifies the episode; a matching RSS item supplies the audio.
"""
import json
import logging
import re
import unicodedata
import urllib.request
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urlencode, urlparse
from xml.etree import ElementTree as ET

import database

logger = logging.getLogger(__name__)
_MAX_BYTES = 16 * 1024 * 1024


class SpotifyError(ValueError):
    pass


def is_spotify_url(url):
    return urlparse(url).hostname in {'open.spotify.com', 'spotify.link', 'www.spotify.link'}


def _episode_id(url):
    parsed = urlparse(url)
    if parsed.scheme not in {'http', 'https'} or parsed.hostname != 'open.spotify.com':
        raise SpotifyError('Please share a Spotify episode link.')
    match = re.fullmatch(r'/(?:intl-[a-zA-Z-]+/)?(?:embed/)?episode/([A-Za-z0-9]{22})/?', parsed.path)
    if not match:
        raise SpotifyError('Please share a single Spotify episode, not a show, track or playlist.')
    return match.group(1)


class _SpotifyRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlparse(newurl).scheme != 'https' or not is_spotify_url(newurl):
            raise SpotifyError('Spotify short link redirected outside Spotify.')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def canonical_url(url):
    if urlparse(url).hostname in {'spotify.link', 'www.spotify.link'}:
        request = urllib.request.Request(url, headers={'User-Agent': 'biangbiangmian3000/1.0'})
        with urllib.request.build_opener(_SpotifyRedirect()).open(request, timeout=15) as response:
            url = response.geturl()
    return 'https://open.spotify.com/episode/' + _episode_id(url)


def _fetch(url):
    if urlparse(url).scheme not in {'http', 'https'}:
        raise SpotifyError('RSS audio discovery requires an HTTP(S) URL.')
    request = urllib.request.Request(url, headers={'User-Agent': 'biangbiangmian3000/1.0'})
    with urllib.request.urlopen(request, timeout=15) as response:
        data = response.read(_MAX_BYTES + 1)
    if len(data) > _MAX_BYTES:
        raise SpotifyError('Podcast metadata or RSS feed is too large.')
    return data


class _NextData(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active = False
        self.parts = []

    def handle_starttag(self, tag, attrs):
        self.active = tag == 'script' and dict(attrs).get('id') == '__NEXT_DATA__'

    def handle_endtag(self, tag):
        if tag == 'script':
            self.active = False

    def handle_data(self, data):
        if self.active:
            self.parts.append(data)


def _normalize(text):
    return ''.join(c for c in unicodedata.normalize('NFKC', text).casefold() if c.isalnum())


def _date(value):
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).date()
    except (ValueError, AttributeError):
        return None


def _matches(meta, item):
    if _normalize(meta['title']) != _normalize(item['title']):
        return False
    date1, date2 = _date(meta.get('published_at')), _date(item.get('published_at'))
    duration1, duration2 = meta.get('duration_seconds'), item.get('duration_seconds')
    # At least one independent guard is required, and every available guard
    # must agree. Dynamic adverts can change a recording's length slightly.
    if date1 and date2 and abs((date1 - date2).days) > 1:
        return False
    if duration1 and duration2:
        if min(duration1, duration2) / max(duration1, duration2) < .8:
            return False
        if abs(duration1 - duration2) > max(90, duration1 * .1):
            return False
    return bool((date1 and date2) or (duration1 and duration2))


def _feed_matches(feed_url, meta):
    import podcast
    root = ET.fromstring(_fetch(feed_url))
    channel = root.find('channel')
    if channel is None or _normalize(channel.findtext('title') or '') != _normalize(meta['show']):
        return []
    matches = []
    for element in channel.findall('item'):
        item = podcast._parse_feed_item(element, feed_url)
        if item and _matches(meta, item) and urlparse(item.get('audio_url') or '').scheme in {'http', 'https'}:
            matches.append(item)
    return matches


def resolve_episode(url):
    """Return RSS episode fields plus author; never subscribe or call AI."""
    episode_id = _episode_id(url)
    parser = _NextData()
    parser.feed(_fetch(f'https://open.spotify.com/embed/episode/{episode_id}').decode('utf-8'))
    try:
        entity = json.loads(''.join(parser.parts))['props']['pageProps']['state']['data']['entity']
        if entity.get('type') != 'episode' or entity.get('id') != episode_id:
            raise ValueError('not the requested episode')
        meta = {'title': entity.get('title') or entity.get('name'), 'show': entity.get('subtitle'),
                'published_at': (entity.get('releaseDate') or {}).get('isoString'),
                'duration_seconds': (entity.get('duration') or 0) / 1000}
        if not meta['title'] or not meta['show']:
            raise ValueError('missing episode/show title')
    except (ValueError, KeyError, TypeError) as exc:
        raise SpotifyError('Spotify episode metadata is unavailable.') from exc

    visited = set()
    def search(feeds):
        matches = {}
        for feed in feeds:
            if feed in visited:
                continue
            visited.add(feed)
            try:
                for item in _feed_matches(feed, meta):
                    matches[item['video_id']] = item
            except (OSError, ValueError, ET.ParseError) as exc:
                logger.info('Spotify RSS lookup failed for %s: %s', feed, exc)
        if len(matches) > 1:
            raise SpotifyError('Multiple RSS episodes match this Spotify link; refusing ambiguous audio.')
        return next(iter(matches.values()), None)

    # Skip known unrelated feeds; a few newly added feeds may lack a title.
    subscriptions = database.list_feeds()
    known = [f['url'] for f in subscriptions if _normalize(f.get('title') or '') == _normalize(meta['show'])]
    untitled = [f['url'] for f in subscriptions if not f.get('title')][:2]
    item = search((known + untitled)[:10])
    if item is None:
        terms = [meta['show']]
        short = re.split(r'[:|–—]', meta['show'], maxsplit=1)[0].strip()
        if short != meta['show']:
            terms.append(short)
        for term in terms:
            query = urlencode({'term': term, 'entity': 'podcast', 'limit': 20})
            try:
                directory = json.loads(_fetch('https://itunes.apple.com/search?' + query))
            except (OSError, ValueError) as exc:
                logger.info('Spotify podcast directory lookup failed: %s', exc)
                continue
            feeds = [r['feedUrl'] for r in directory.get('results', [])
                     if r.get('feedUrl') and _normalize(r.get('collectionName', '')) == _normalize(meta['show'])]
            item = search(feeds[:10])
            if item:
                break
    if item is None:
        # Subscription titles are editable. A small final fallback checks the
        # actual channel title when the directory did not find this show.
        item = search([f['url'] for f in subscriptions if f['url'] not in visited][:3])
    if item is None:
        raise SpotifyError('No matching public RSS audio found. This episode may be Spotify-exclusive, unavailable, or outside the public feed archive.')
    return {**item, 'author': meta['show']}
