#!/usr/bin/env python3
"""List the TTS files an offline session actually needs (issue #612).

Run on the laptop against the freshly pulled data/offline.db; prints one
`<sha256>.mp3` filename per line for rsync --files-from.

Syncing all of data/tts/ means moving ~120 MB of audio accumulated over
months, most of it for words that aren't due. Over a slow link before a
flight that's the difference between a minute and an hour. The frontend only
ever asks for two kinds of text (see _ttsUrl / _storyAudioUrl in app.js):

    story_sentences.sentence_zh   — listening fronts and "play full story"
    entries.word_zh               — the fallback when a card has no sentence

so the manifest is exactly those two, scoped to what's reachable in the next
few days.

    python scripts/offline_tts_manifest.py data/offline.db [--days-ahead 14]
"""

import argparse
import datetime
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import languages  # noqa: E402
import tts  # noqa: E402

# Stories older than this are not reachable in a review session, but a couple
# of days of slack costs almost nothing and covers a pull made after midnight.
STORY_LOOKBACK_DAYS = 3


def _filename(text: str, lang: str) -> str | None:
    if not text or not text.strip():
        return None
    voice = tts.voice_for(lang)
    return os.path.basename(tts._cache_path(text, voice))


def collect(db_path: str, days_ahead: int) -> list[str]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    today = datetime.date.today()
    since = (today - datetime.timedelta(days=STORY_LOOKBACK_DAYS)).isoformat()
    until = (today + datetime.timedelta(days=days_ahead)).isoformat()

    names: set[str] = set()

    # Story sentences — stories carry their own lang ('zh' for legacy NULL rows).
    for row in conn.execute(
        "SELECT ss.sentence_zh AS text, COALESCE(s.lang, 'zh') AS lang "
        "FROM story_sentences ss JOIN stories s ON s.id = ss.story_id "
        "WHERE s.date >= ?", (since,)
    ):
        name = _filename(row["text"], row["lang"])
        if name:
            names.add(name)

    # Words on cards that can come up in the session. Suspended cards are
    # excluded; buried ones are not, since burial lifts the next day.
    for row in conn.execute(
        "SELECT e.word_zh AS text, COALESCE(e.lang, 'zh') AS lang "
        "FROM cards c JOIN entries e ON e.id = c.word_id "
        "WHERE c.deleted_at IS NULL AND c.state != 'suspended' AND c.due <= ?",
        (until,)
    ):
        name = _filename(row["text"], row["lang"])
        if name:
            names.add(name)

    conn.close()
    return sorted(names)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("db", help="path to the pulled offline database")
    parser.add_argument("--days-ahead", type=int, default=14,
                        help="include words due within this many days (default 14)")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        sys.exit(f"ERROR: database not found: {args.db}")
    for name in collect(args.db, args.days_ahead):
        print(name)


if __name__ == "__main__":
    main()
