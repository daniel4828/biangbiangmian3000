"""Exact whole-word ranks from the bundled SUBTLEX-CH snapshot."""
import json
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def _ranks() -> dict[str, int]:
    path = Path(__file__).parent / 'resources' / 'subtlex-ch-ranks.json'
    return json.loads(path.read_text(encoding='utf-8'))


def frequency_rank(word: str, lang: str) -> int | None:
    """Unknown words and other languages have no Chinese frequency rank."""
    return _ranks().get(word) if lang == 'zh' else None
