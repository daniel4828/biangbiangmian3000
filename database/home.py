"""Read-only homepage selections, using the visitor's local day (#1212)."""
from collections import defaultdict
from datetime import datetime, timezone
import random

from .core import get_db


def home_discovery(now: datetime) -> dict:
    conn = get_db()
    try:
        rows = [dict(row) for row in conn.execute("""
            SELECT e.id, e.title, e.kind, e.channel_id, e.author, e.published_at,
                   f.title AS feed_title,
                   ((TRIM(COALESCE(e.transcript_zh, '')) != '' AND
                     TRIM(COALESCE(e.audio_url, '')) != '') OR EXISTS (
                       SELECT 1 FROM audio_tracks a WHERE a.owner_kind = 'episode'
                       AND a.owner_id = e.id AND a.lang = 'zh' AND a.variant = 'fulltext'
                   )) AS can_listen,
                   (COALESCE(e.transcript_zh, '') != '' OR
                    COALESCE(e.summary_zh, '') != '' OR
                    COALESCE(e.summary_de, '') != '') AS readable
            FROM podcast_episodes e
            LEFT JOIN podcast_feeds f ON f.url = e.channel_id
            WHERE e.archived_at IS NULL
            ORDER BY e.id
        """)]
    finally:
        conn.close()

    today = now.date().isoformat()

    def published_local(row):
        value = row['published_at'] or ''
        try:
            if len(value) == 10:
                return value, value
            stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
            stamp = stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp
            local = stamp.astimezone(now.tzinfo)
            return local.date().isoformat(), local.isoformat()
        except ValueError:
            return None, ''

    coffee = [row for row in rows if row['kind'] == 'podcast'
              and any('声动早咖啡' in (row[key] or '')
                      for key in ('feed_title', 'title', 'author'))
              and published_local(row)[0] == today]
    podcast = max(coffee, key=lambda row: (published_local(row)[1], row['id']), default=None)
    recommendations = []
    if now.hour >= 10:
        # A daily shuffled, interleaved deck keeps refreshes stable and moves
        # through the library rather than drawing three random repeats each time.
        rng = random.Random(today)
        groups = defaultdict(list)
        for row in rows:
            if row['readable'] and (podcast is None or row['id'] != podcast['id']):
                groups[(row['kind'], row['author'] or row['channel_id'])].append(row)
        piles = list(groups.values())
        rng.shuffle(piles)
        for pile in piles:
            rng.shuffle(pile)
        deck = []
        while piles:
            for pile in piles:
                deck.append(pile.pop())
            piles = [pile for pile in piles if pile]
        if deck:
            start = ((now.hour - 10) // 2 * 3) % len(deck)
            recommendations = [deck[(start + i) % len(deck)] for i in range(min(3, len(deck)))]

    def public(row):
        return {key: row[key] for key in ('id', 'title', 'kind', 'channel_id')} if row else None

    coffee_public = public(podcast)
    if coffee_public:
        coffee_public['can_listen'] = bool(podcast['can_listen'])
    return {'date': today, 'podcast': coffee_public,
            'recommendations': [public(row) for row in recommendations],
            'recommendations_visible': now.hour >= 10}
