import os

import database
from offline import is_offline
from .queue_manager import QueueManager

# Set DISABLE_AI=1 in run.dev.sh to skip story generation during development.
_DISABLE_AI_ENV = os.getenv("DISABLE_AI", "").lower() in ("1", "true", "yes")


def ai_disabled() -> bool:
    """True when no AI call may be made.

    A function rather than a constant because LOCAL_MODE decides this at
    request time: the laptop has AI while the Wi-Fi is up and loses it when
    the network goes away, with no restart in between (#625).
    """
    return _DISABLE_AI_ENV or is_offline()

# Pseudo language of the cross-language "🌐 Languages" session (#1378): every
# enabled language reviewed together with one story. Travels as the `lang`
# query param. Same literal as database.cards.MULTI_LANG.
MULTI_LANG = "multi"

# Shared singleton — imported by review.py and browse.py
queue_mgr = QueueManager()


def leaf_ids(deck_id: int, category: str, lang: str | None = None) -> list[int]:
    """If deck is a parent (no category), return descendant leaf IDs (optionally lang-filtered); else [deck_id].

    Lang filtering only applies to descendant expansion — a direct category-leaf deck
    is always returned as-is even if its own lang differs from the requested lang.
    """
    deck = database.get_deck(deck_id)
    if deck["category"] is None:
        return database.get_descendant_leaf_deck_ids(deck_id, category, lang=lang)
    return [deck_id]
