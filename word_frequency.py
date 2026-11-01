"""Exact whole-word ranks from the bundled SUBTLEX-CH snapshot."""
import json
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def _ranks() -> dict[str, int]:
    path = Path(__file__).parent / 'resources' / 'subtlex-ch-ranks.json'
    return json.loads(path.read_text(encoding='utf-8'))


def _lexicon_rank(lemma: str, lang: str) -> int | None:
    import lexicon
    info = lexicon.lemma_info(lemma, lang)
    rank = info.get('rank') if info else None
    return rank if isinstance(rank, int) and not isinstance(rank, bool) else None


def frequency_rank(word: str, lang: str) -> int | None:
    """zh: SUBTLEX-CH whole-word rank. Other languages: lemma rank from the
    bundled lexicon (#1386); None when the language has no lexicon or the word
    is unlisted. Irregular form cards ("tener · Indefinido") use the lemma
    before " · "; reflexive infinitives fall back to the stem without "se"."""
    if lang == 'zh':
        return _ranks().get(word)
    try:
        word = (word or '').strip()
        if not word:
            return None
        candidates = [word]
        if ' · ' in word:
            candidates.append(word.split(' · ', 1)[0].strip())
        for cand in list(candidates):
            if len(cand) > 4 and cand.lower().endswith('se'):
                candidates.append(cand[:-2])
        for cand in candidates:
            rank = _lexicon_rank(cand, lang)
            if rank is not None:
                return rank
    except Exception:
        return None
    return None
