"""Merge word-level cues into sentence-level cues (#1048).

Word-level highlighting is too granular for a read-along — Daniel is
tracking "which sentence am I on", not "which syllable". Sentence cues are
what gets stored in audio_tracks.cues_json; word_cues stay on the Track for
a future per-word highlight mode (see audio/__init__.py's Track dataclass).
"""
from . import Cue

# Sentence-final punctuation, Chinese and Western. Semicolons are included —
# knowledge-base rendered text uses them as clause separators as often as a
# period, and a sentence cue spanning past one reads oddly on screen.
_SENTENCE_END = set("。！？；…" + ".!?;")
# Every quote character toggles "inside a quote" — we don't try to pair
# opening/closing marks (straight quotes don't distinguish them anyway), just
# track parity, which is enough to keep "他说“好。”然后走了" from being cut
# at the period inside the quotation.
#
# Deliberately EXCLUDES ' and ’ — Daniel is also learning French (CEFR B1),
# and French elision apostrophes (l'eau, qu'il, d'accord) are extremely
# common. Toggling "in quote" on every one of those would flip parity
# constantly across a French text, so most periods would land "inside a
# quote" by accident and the whole document would merge into one giant
# sentence cue. ’ is both a right single quote AND an apostrophe with no way
# to tell them apart from the character alone, so it has to go too. The cost
# is that an English 'single-quoted phrase' containing a period won't get
# its sentence break suppressed — acceptable, since English isn't a language
# this app teaches. Do not add these back without re-solving the French case.
_QUOTE_CHARS = set("\"“”「」『』")


def _sentence_boundaries(text: str) -> set[int]:
    """Character indices in `text` that are sentence-ending punctuation NOT
    inside a quoted span."""
    boundaries: set[int] = set()
    in_quote = False
    for i, ch in enumerate(text):
        if ch in _QUOTE_CHARS:
            in_quote = not in_quote
        elif ch in _SENTENCE_END and not in_quote:
            boundaries.add(i)
    return boundaries


def to_sentences(word_cues: list[Cue], text: str) -> list[Cue]:
    """Group `word_cues` (in reading order, char_start/char_end positions
    into `text`) into sentence-level cues.

    Sentence punctuation is not itself a spoken "word" (TTS doesn't emit a
    WordBoundary for "。"), so it always falls in the gap between two word
    cues rather than inside one — the boundary check below looks at exactly
    that gap, not at the cues' own text. The merged cue's char_end is pushed
    past the punctuation itself (not just the last word), so the sentence
    text Daniel sees actually ends with its period — dropping it would read
    as if every sentence were missing its last character.
    """
    if not word_cues:
        return []

    boundaries = _sentence_boundaries(text)
    sentences: list[Cue] = []
    current: list[Cue] = []
    for i, cue in enumerate(word_cues):
        current.append(cue)
        gap_end = word_cues[i + 1].char_start if i + 1 < len(word_cues) else len(text)
        gap_boundaries = [b for b in boundaries if cue.char_end <= b < gap_end]
        if gap_boundaries:
            # Several punctuation marks can sit back to back ("……", "?!") —
            # take the last one so the whole run ends up inside the sentence.
            end_pos = max(gap_boundaries) + 1
            sentences.append(_merge(current, text, end_pos))
            current = []
    if current:
        sentences.append(_merge(current, text, current[-1].char_end))
    return sentences


def _merge(cues: list[Cue], text: str, end_pos: int) -> Cue:
    first = cues[0]
    return Cue(
        start_ms=first.start_ms, end_ms=cues[-1].end_ms,
        text=text[first.char_start:end_pos],
        char_start=first.char_start, char_end=end_pos,
    )
