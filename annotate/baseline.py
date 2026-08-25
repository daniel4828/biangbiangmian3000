"""Static baseline vocabulary — words Daniel knew before this app existed (#922).

The annotators answer one question over and over: "does Daniel already know this
word?" Until now the answer came only from what he had studied here
(entries.word_zh / entry_forms) plus what he had clicked ✓ Known on
(known_words). That leaves every ordinary beginner word — `manger`, `maison` —
flagged as new, drowning a French rendition in parentheses.

These lists are the missing floor: French CEFR A1-A2 and Chinese HSK 3.0 1-4.
They are a static linguistic fact about a proficiency level, not a personal
per-word mark, which is why they live in the repo rather than in the known_words
table:

  * offline sync only merges cards + review_log, so 17k rows written to the
    server database would never reach the laptop instance; files ship with the
    code;
  * the production database needs no write at all.

Nothing here ever becomes a card. This is "stop showing me this word", the exact
opposite of adding one.

Same failure posture as annotate.romance.stopwords(): an unreadable file degrades
to an empty set. A missing baseline means noisier annotation, which is where we
already were; raising here would cost the whole summary.
"""
import logging
import os

logger = logging.getLogger(__name__)

_cache: dict[str, frozenset[str]] = {}


def baseline_words(lang: str) -> frozenset[str]:
    """Known-by-default vocabulary for `lang`, loaded once per process.

    Chinese entries are stored as-is; every other language's are lowercased,
    matching how the romance annotator builds its lookup key. A language with no
    baseline file (Spanish, for now) simply gets an empty set."""
    if lang in _cache:
        return _cache[lang]
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"baseline_{lang}.txt")
    words: set[str] = set()
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    words.add(line if lang == "zh" else line.lower())
    except FileNotFoundError:
        logger.debug("annotate.baseline: no baseline list for lang=%s", lang)
    except Exception as e:
        logger.warning("annotate.baseline: cannot read %s — %s", path, e)
    _cache[lang] = frozenset(words)
    return _cache[lang]
