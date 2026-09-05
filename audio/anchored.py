"""Phase 2 of #1047 (issue #1051): text-anchored ASR alignment.

`text` + `audio_path` together means we already know the CORRECT transcript
and just need to know WHEN each part of it is spoken. The original plan was
forced alignment via `aeneas`, but it doesn't install on Python 3.14 (last
released 2017, and its own build script fails outright) and additionally
needs a system-level `espeak` binary — not a dependency this project can
take on. Don't re-propose it without re-solving that.

Instead: run the cloud ASR we already have (audio/asr_cloud.py) to get
TIMESTAMPS (its transcript may have typos — Chinese names/places are
especially error-prone, e.g. "浙江" misheard as "折江"), then align that
transcript against the KNOWN-CORRECT text with difflib.SequenceMatcher
(stdlib, no new dependency) and transfer the ASR timestamps onto the correct
text. The result is "correct text + accurate timing" — what forced alignment
would have given us, without a single new dependency.

Algorithm (see audio/__init__.py's module docstring for how this sits
alongside the other three planned alignment paths):
  1. asr_cloud.build() gets absolute-time, sentence-grained ASR cues.
  2. Those cues are stitched into one ASR transcript string, with a
     per-character time computed by linearly interpolating within each
     cue's [start_ms, end_ms] span — treating a whole 10-second segment as
     one timestamp would make sentence boundaries landing inside it wildly
     wrong.
  3. Both the ASR transcript and the target text are normalized (punctuation
     and whitespace stripped, Latin case folded) before diffing — ASR output
     has no punctuation and target text does, so diffing the raw strings
     would never line up. difflib.SequenceMatcher(autojunk=False) finds the
     matching runs; autojunk MUST be off, see the comment at the call site.
  4. The target text is split into sentences (audio.segment.sentence_spans,
     not re-derived here).
  5. Every sentence's cue timing comes from the EARLIEST and LATEST matched
     character inside its span, transferred back through the ASR timeline.
     A sentence with zero matched characters gets NO cue at all — never a
     guess borrowed from a neighbor (#1048's rule: a missing highlight beats
     a wrong one).
  6. Two sanity checks gate the whole result: sentence coverage and total
     duration drift. Failing either raises AudioTrackError rather than
     silently handing back a track that LOOKS synced but isn't — see the
     constants below for the actual thresholds.
"""
import bisect
import difflib
import logging

from . import AudioTrackError, Cue, Track
from . import asr_cloud
from .segment import sentence_spans

logger = logging.getLogger(__name__)

# Below this fraction of target sentences getting a cue, something is
# fundamentally wrong (missing chapter, extra intro, wrong recording
# entirely) rather than just a few unlucky mismatches.
_MIN_SENTENCE_COVERAGE = 0.6

# If the last cue's end time is off from the ASR track's own measured
# duration by more than this fraction, the alignment drifted badly even if
# individual sentences looked plausible in isolation. Only meaningful on
# longer audio, though — see _MIN_DRIFT_FLOOR_MS below.
_MAX_DURATION_DRIFT = 0.15

# The relative threshold above catches "the text only covers half the
# audio" — real mismatches. It must NOT catch normal trailing silence, outro
# music, or a narrator's closing pause, all of which are completely
# ordinary and often outsized on short recordings: 0.5s of trailing silence
# is 17% of a 3-second clip but only 2.5% of a 10-minute one — the same
# percentage means something totally different at each end. So any drift
# under this absolute floor is allowed regardless of the fraction it
# represents — no real "text doesn't match this audio" case is ever off by
# only a few seconds.
_MIN_DRIFT_FLOOR_MS = 10_000


def _normalize(text: str) -> tuple[str, list[int]]:
    """Strip everything that isn't a letter/digit and fold Latin case, so
    ASR output (no punctuation) and the target text (has punctuation) can be
    diffed against each other. Returns (normalized_text, offset_map) where
    offset_map[i] is the index in the ORIGINAL `text` that
    normalized_text[i] came from — same shape as audio.strip_annotations'
    offset_map, for the same reason: a position computed against the
    stripped string is meaningless without a way back to the original.

    str.isalnum() is true for CJK ideographs (Unicode general category Lo),
    so Chinese text needs no special-casing here.
    """
    chars: list[str] = []
    offset_map: list[int] = []
    for i, ch in enumerate(text):
        if ch.isalnum():
            chars.append(ch.lower())
            offset_map.append(i)
    return "".join(chars), offset_map


def _asr_char_times(cues: list) -> tuple[str, list[int]]:
    """Reconstruct the ASR track's joined transcript and, for every one of
    its characters, an interpolated absolute time in milliseconds.

    Mirrors asr_cloud.build()'s own char_start/char_end scheme (a single
    join space between consecutive cues, none before the first) so the
    string built here lines up with how those cues were produced — not that
    this function relies on cue.char_start/char_end directly, it just
    re-derives the same string by walking the cues in order.

    Within one cue, character i of n is timed at
    start_ms + (end_ms - start_ms) * i / n — a straight-line guess across
    the segment, which matters a lot: a 10-second ASR segment might contain
    two sentences, and treating the whole segment as one timestamp would put
    both sentence boundaries in the same wrong place.
    """
    chars: list[str] = []
    times: list[int] = []
    for i, cue in enumerate(cues):
        if i > 0:
            chars.append(" ")
            times.append(cue.start_ms)
        text = cue.text
        n = len(text)
        span = cue.end_ms - cue.start_ms
        for j, ch in enumerate(text):
            t = cue.start_ms + (span * j / n if n else 0)
            chars.append(ch)
            times.append(round(t))
    return "".join(chars), times


def build(text: str, audio_path: str, lang: str = "zh", prefer_local: bool = False,
          should_abort=None) -> Track:
    """text + audio_path -> Track, source='anchored' (#1051).

    Raises AudioTrackError when the ASR step fails (propagated straight
    from whichever of asr_cloud.build/asr_local.build ran, not re-wrapped) or
    when the alignment's own quality checks (coverage / duration drift) don't
    pass. Never returns a track that "looks" synced but isn't — see the
    module docstring for why an unmatched sentence is dropped rather than
    guessed at.

    `prefer_local` (#1053) selects whisper.cpp instead of Groq for the ASR
    timestamps this alignment is built on — same flag, same meaning, as
    build_track()'s. The alignment step itself (SequenceMatcher against the
    known-correct text) is identical either way; only where the timestamps
    came from differs.
    """
    if prefer_local:
        from . import asr_local  # lazy: avoids a circular import at package load
        asr_track = asr_local.build(audio_path, lang=lang, should_abort=should_abort)
    else:
        asr_track = asr_cloud.build(audio_path, lang=lang)

    asr_text, asr_times = _asr_char_times(asr_track.cues)
    norm_asr, asr_offset_map = _normalize(asr_text)
    norm_target, target_offset_map = _normalize(text)

    if not norm_asr or not norm_target:
        raise AudioTrackError(
            "text-anchored alignment: nothing left to align after stripping "
            "punctuation/whitespace")

    # autojunk=False is required, not optional: the default autojunk
    # heuristic treats any character appearing in more than 1% of a long
    # sequence as "popular" and ignores it when finding matches. Ordinary
    # Chinese text blows way past that 1% threshold for its most common
    # characters — with autojunk left on, alignment quality degrades in a
    # way that's very hard to diagnose after the fact. Do not remove this.
    matcher = difflib.SequenceMatcher(None, norm_asr, norm_target, autojunk=False)

    # (target text char index, ASR-derived time in ms), in increasing order
    # of target index — SequenceMatcher's matching blocks are strictly
    # increasing in both sequences, so no separate sort is needed.
    matched_pairs: list[tuple[int, int]] = []
    for a, b, size in matcher.get_matching_blocks():
        for k in range(size):
            asr_char_idx = asr_offset_map[a + k]
            target_char_idx = target_offset_map[b + k]
            matched_pairs.append((target_char_idx, asr_times[asr_char_idx]))

    spans = sentence_spans(text)
    if not spans:
        raise AudioTrackError("text-anchored alignment: target text has no sentences")

    target_indices = [p[0] for p in matched_pairs]

    cues: list[Cue] = []
    last_end_ms = 0
    for start, end in spans:
        lo = bisect.bisect_left(target_indices, start)
        hi = bisect.bisect_left(target_indices, end)
        window = matched_pairs[lo:hi]
        if not window:
            # No matched character anywhere in this sentence — the ASR
            # transcript and the target text simply don't overlap here.
            # Dropping the cue (not borrowing a neighbor's timing) is the
            # whole point of #1048's "missing beats wrong" rule.
            continue
        times = [t for _, t in window]
        start_ms = max(min(times), last_end_ms)  # enforce monotonic sequence
        end_ms = max(max(times), start_ms)
        cues.append(Cue(start_ms=round(start_ms), end_ms=round(end_ms),
                        text=text[start:end], char_start=start, char_end=end))
        last_end_ms = end_ms

    total_sentences = len(spans)
    coverage = len(cues) / total_sentences if total_sentences else 0.0
    if coverage < _MIN_SENTENCE_COVERAGE:
        raise AudioTrackError(
            f"text-anchored alignment only matched {len(cues)}/{total_sentences} "
            f"sentences ({coverage:.0%}, need >= {_MIN_SENTENCE_COVERAGE:.0%}) — "
            "the text and audio likely don't correspond to each other")

    if cues and asr_track.duration_ms:
        drift_ms = abs(cues[-1].end_ms - asr_track.duration_ms)
        allowed_ms = max(_MAX_DURATION_DRIFT * asr_track.duration_ms, _MIN_DRIFT_FLOOR_MS)
        if drift_ms > allowed_ms:
            raise AudioTrackError(
                f"text-anchored alignment's last cue ends {drift_ms:.0f}ms away from "
                f"the audio's actual duration (allowed <= {allowed_ms:.0f}ms) — "
                "the text and audio likely don't correspond to each other")

    if not cues:
        raise AudioTrackError("text-anchored alignment produced no usable cues")

    # The CORRECT text, not the ASR transcript: every cue's char_start/
    # char_end was computed against it, and it is also what the reader sees
    # on screen. Storing the ASR text here would put the typos this whole
    # module exists to remove back into the frontend's alignment anchor.
    return Track(audio_path=audio_path, duration_ms=asr_track.duration_ms,
                cues=cues, word_cues=[], source="anchored", voice=None,
                source_text=text)
