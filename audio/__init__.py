"""Time-aligned audio tracks (issue #1047 umbrella; this package implements
phase 1, issue #1048).

Karaoke-style read-along needs two things out of any audio source: the mp3
itself, and a list of "cues" — {start_ms, end_ms, text, char_start,
char_end} saying which slice of the SOURCE TEXT is being spoken at which
moment. char_start/char_end are positions in the original source text (the
one handed to whichever aligner produced this track), not in the
already-rendered/annotated HTML Daniel actually reads — that HTML lives in a
different table (knowledge_renditions / book_renditions). The frontend cuts
the rendered HTML at these boundaries to highlight the current span while
still showing inline glosses and keeping words tappable; re-matching cue
text against the rendered HTML after the fact would drift the moment
there's markup, a repeated phrase, or a skipped word.

Four ways to build a track are planned for #1047:
  1. TTS + WordBoundary (this phase, tts_track.py) — free, word-level,
     works for any plain text we already have. Implemented.
  2. Forced alignment (aeneas) — text + an existing recording.
  3. Cloud ASR (Groq) — a recording with no matching text.
  4. Local ASR (whisper.cpp) — same, offline.
Only #1 exists today. build_track() below is the single entry point all
four eventually share, so a caller never needs to know which path produced
a given Track — the other three raise NotImplementedError rather than a
half-written branch.
"""
import re
from dataclasses import dataclass


@dataclass
class Cue:
    start_ms: int
    end_ms: int
    text: str
    char_start: int
    char_end: int

    def to_dict(self) -> dict:
        return {
            "start_ms": self.start_ms, "end_ms": self.end_ms, "text": self.text,
            "char_start": self.char_start, "char_end": self.char_end,
        }


@dataclass
class Track:
    audio_path: str
    duration_ms: int | None
    cues: list          # sentence-level Cue objects — what audio_tracks.cues_json stores
    word_cues: list      # word-level Cue objects — kept for a future per-word highlight mode
    source: str           # 'tts' | 'forced' | 'asr_cloud' | 'asr_local'
    voice: str | None


class AudioTrackError(Exception):
    """Every failure in this package raises this — the same role
    knowledge/rendition.py's RenditionError plays there. Callers must
    surface the reason and write nothing: a half-built audio track wearing
    the label of a finished one is exactly what this codebase never allows."""


def build_track(*, text: str | None = None, audio_path: str | None = None,
                lang: str = "zh", voice: str | None = None) -> Track:
    """The single entry point for all four alignment paths described in the
    module docstring above.

    Only text-only (TTS + WordBoundary) is implemented. Every combination
    involving `audio_path` is a documented NotImplementedError, not a
    half-written branch — phases 4-6 of #1047 (aeneas forced alignment,
    Groq ASR, local whisper.cpp) have exactly one place to plug in.
    """
    if text and not audio_path:
        from . import tts_track  # lazy: avoids a circular import at package load
        return tts_track.build(text, lang=lang, voice=voice)
    if audio_path:
        raise NotImplementedError(
            "audio-based alignment is not implemented yet — see #1047 phases "
            "4 (aeneas forced alignment), 5 (Groq ASR) and 6 (local whisper.cpp)")
    raise AudioTrackError("build_track() needs at least `text` (or `audio_path`)")


# ---------------------------------------------------------------------------
# Stripping inline vocabulary glosses before synthesis
# ---------------------------------------------------------------------------
# "生态（shēngtài - Ökologie）" spoken aloud is nonsense; so is "mot
# (Bedeutung)" for a Romance-language reading. Both are bracket groups the
# app itself inserted (#638, #1001's inline gloss format), and both are
# conservatively detected the same way: a bracket group containing a
# ' - ' separator (our own annotation format) or any CJK text. An ordinary
# parenthetical a human actually wrote — "(ca. 12:30)" — has neither and is
# left alone.

_CJK_RE = re.compile(r"[一-鿿㐀-䶿豈-﫿]")
# No nesting expected (annotations never nest brackets); the optional
# leading space also swallows " (Gloss)" so stripping it doesn't leave a
# double space behind.
_BRACKET_RE = re.compile(r"[ ]?[(（][^()（）]*[)）]")


def _should_strip(bracket_group: str) -> bool:
    inner = bracket_group.strip()
    if inner and inner[0] in "(（" and inner[-1] in ")）":
        inner = inner[1:-1]
    return " - " in inner or bool(_CJK_RE.search(inner))


def strip_annotations(text: str) -> tuple[str, list[int]]:
    """Remove inline vocabulary glosses before this text is sent to TTS.

    Returns (clean_text, offset_map) where offset_map[i] is the index in the
    ORIGINAL `text` that clean_text[i] came from. This is what lets a cue
    computed against clean_text be translated back into a position in the
    text Daniel actually reads — see the module docstring for why that has
    to be an exact character range rather than a re-match after the fact.
    """
    clean_chars: list[str] = []
    offset_map: list[int] = []
    i = 0
    n = len(text)
    while i < n:
        m = _BRACKET_RE.match(text, i)
        if m and _should_strip(m.group(0)):
            i = m.end()
            continue
        clean_chars.append(text[i])
        offset_map.append(i)
        i += 1
    return "".join(clean_chars), offset_map
