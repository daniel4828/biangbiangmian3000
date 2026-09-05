"""Time-aligned audio tracks (issue #1047 umbrella; this package implements
phase 1 (#1048), phase 2 (#1051) and phase 3 (#1052)).

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
  1. TTS + WordBoundary (#1048, tts_track.py) — free, word-level,
     works for any plain text we already have. Implemented.
  2. Text-anchored ASR alignment (#1051, anchored.py) — text + an existing
     recording. Originally planned as forced alignment via `aeneas`, which
     turned out not to install on Python 3.14 (last released 2017, and its
     own build script fails outright) and additionally needs a system-level
     `espeak` binary — both dealbreakers for this project. Implemented
     instead as: run the cloud ASR (#3 below) to get timestamps, then align
     its (possibly typo-ridden) transcript against the known-correct text
     with difflib.SequenceMatcher and transfer the timestamps across. See
     anchored.py's module docstring for the full algorithm.
  3. Cloud ASR (Groq, #1052, asr_cloud.py) — a recording with no matching
     text. Implemented.
  4. Local ASR (whisper.cpp, #1053, asr_local.py) — same, but running on this
     machine's own CPU cores instead of paying Groq: free, but 1-3x slower
     than realtime with no GPU, so it can never run synchronously inside an
     HTTP request. build_track()'s `prefer_local` flag selects it instead of
     asr_cloud.py; the actual scheduling decision (only transcribe while
     Daniel is away from the server, see scripts/audio_worker.py) lives
     entirely outside this module — build_track() itself has no notion of
     "queue this for later", it just picks which ASR implementation to call
     right now, synchronously, whichever caller decided that was fine.
build_track() below is the single entry point all four share, so a caller
never needs to know which path produced a given Track.
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
    source: str           # 'tts' | 'anchored' | 'asr_cloud' | 'asr_local'
    voice: str | None
    # The exact text every cue's char_start/char_end indexes into (#1049's
    # audio_tracks.source_text). The frontend cuts the rendered, annotated
    # HTML at those offsets, so it needs the same string the aligner saw —
    # for the TTS path that's the caller's text, for the ASR paths it's the
    # transcript the model produced. Reconstructing it from the cues after
    # the fact is not the same thing: anything between cues (dropped words,
    # unmatched sentences) is gone.
    source_text: str | None = None


class AudioTrackError(Exception):
    """Every failure in this package raises this — the same role
    knowledge/rendition.py's RenditionError plays there. Callers must
    surface the reason and write nothing: a half-built audio track wearing
    the label of a finished one is exactly what this codebase never allows."""


class AudioTrackAborted(AudioTrackError):
    """The caller's `should_abort` callback asked to stop mid-run (#1053).

    Deliberately its own class, not a plain AudioTrackError: being asked to
    step aside is NOT a failure. scripts/audio_worker.py catches this one to
    put the job back in 'pending' (whisper.cpp is idempotent — re-running it
    tomorrow costs nothing), while a real AudioTrackError marks the job
    'error'. Collapsing the two would mean every time Daniel sat down at his
    laptop, whichever transcription was running got permanently written off
    as broken."""


def build_track(*, text: str | None = None, audio_path: str | None = None,
                lang: str = "zh", voice: str | None = None,
                prefer_local: bool = False, should_abort=None) -> Track:
    """The single entry point for all four alignment paths described in the
    module docstring above.

    `prefer_local` (#1053) only matters when `audio_path` is involved: True
    routes to whisper.cpp (audio/asr_local.py, free but slow, meant to be
    called from a background worker during quiet hours — see
    scripts/audio_worker.py), False (the default) routes to Groq
    (audio/asr_cloud.py, paid but runs in seconds). This function does NOT
    decide when it's okay to run the slow local path — it just dispatches to
    whichever ASR implementation the caller already decided to use; the
    "don't do this while Daniel is at the keyboard" logic belongs entirely to
    the caller (the worker script), never here.

    Deliberately NOT implemented here: falling back from cloud to local (or
    vice versa) automatically. Local ASR can take hours, and that must never
    happen synchronously inside a request just because Groq had a bad day —
    a fallback belongs in the queueing decision, not in this dispatcher.

    `should_abort` (#1053) is an optional zero-argument callback polled while
    the slow local path runs; returning True makes it stop and raise
    AudioTrackAborted. Only the local path honours it — the other three
    finish in seconds, so there is nothing to interrupt.
    """
    if text and not audio_path:
        from . import tts_track  # lazy: avoids a circular import at package load
        return tts_track.build(text, lang=lang, voice=voice)
    if audio_path and not text:
        if prefer_local:
            from . import asr_local  # lazy: avoids a circular import at package load
            return asr_local.build(audio_path, lang=lang, should_abort=should_abort)
        from . import asr_cloud  # lazy: avoids a circular import at package load
        return asr_cloud.build(audio_path, lang=lang)
    if text and audio_path:
        from . import anchored  # lazy: avoids a circular import at package load
        return anchored.build(text, audio_path, lang=lang, prefer_local=prefer_local,
                              should_abort=should_abort)
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
