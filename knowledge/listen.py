"""Build a "Listen" read-along track for a knowledge-base episode (#1074,
extracted from routes/podcast.py's _listen_thread in #1133 part 3 so the auto
crawl path (podcast.py's _maybe_prepare_listen) can reuse the exact same
logic instead of growing a second copy).

Downloads the episode's own audio (the RSS enclosure), aligns it against its
already-correct transcript (audio/anchored.py's difflib-based alignment), and
saves the result as an audio_tracks row. Two callers today: the "🎧 Listen"
button (routes/podcast.py, synchronous-from-Daniel's-perspective, runs in a
background thread he's actively waiting on) and the auto-process crawl path
(podcast.py, runs unattended after a new episode is summarized). Both need
identical behavior — same download dir, same cloud-first/local-fallback
order, same "never write a half-built track" guarantee — so this is the only
place that behavior is allowed to live.
"""
import logging
import os

import audio
import database
import knowledge.audio_fetch

logger = logging.getLogger(__name__)

# Where a "Listen" download's mp3 lives, kept (not cleaned up after the
# align) so a rebuild never has to download it a second time. Same parent
# knowledge/ingest.py's _AUDIOBOOK_AUDIO_DIR uses for downloaded audiobook
# audio — one place on disk for "source audio fetched for local processing".
_LISTEN_AUDIO_DIR = os.path.join("data", "audio", "source")


class ListenError(Exception):
    """Download or alignment failed — callers must surface the reason and
    write nothing (see build_and_save's docstring)."""


def build_and_save(episode_id: int, *, on_progress=None) -> dict:
    """Download this episode's own audio, align it against its already-
    correct transcript, and save the resulting track.

    Cloud first, local as a fallback (#1100) — NOT prefer_local=True. Cloud
    ASR (OpenAI/Groq) takes seconds and costs a few cents; local whisper.cpp
    is free but 4-10x slower. Since the alignment step only keeps the ASR
    run's TIMESTAMPS (the known-correct transcript replaces its text via
    difflib), cloud's slightly lower accuracy costs nothing here, so there is
    no reason to default to the slow path. If cloud ASR fails outright (no
    OPENAI_API_KEY/GROQ_API_KEY configured, or a transient API error), this
    falls back to local rather than failing the whole call — but the
    fallback is reported via `on_progress`, not silent: going from ~30s to
    several minutes without a word said about it is exactly the kind of
    surprise this codebase doesn't allow.

    `on_progress` is an optional `callable(str) -> None` forwarded to
    audio.build_track. 🔴 Only ever timestamps/chunk counters, never
    transcript text — see audio.build_track's own on_progress docstring for
    why.

    Raises ListenError (and writes nothing to the DB or a half-built track)
    when: the episode no longer exists, it's missing audio_url or
    transcript_zh, the download fails, or both the cloud and local alignment
    attempts fail. On success, saves an audio_tracks row
    (owner_kind='episode', lang='zh', variant='fulltext') and returns a
    small summary dict — callers that want to log more than "it worked" can
    read track fields off what they choose to keep, but nothing here forces
    them to.
    """
    episode = database.get_episode(episode_id)
    if not episode:
        raise ListenError("Episode no longer exists")

    audio_url = (episode.get("audio_url") or "").strip()
    transcript = (episode.get("transcript_zh") or "").strip()
    if not audio_url or not transcript:
        raise ListenError("Missing audio_url or transcript")

    ext = os.path.splitext(audio_url.split("?")[0])[1] or ".mp3"
    filename = f"episode-{episode_id}{ext}"
    try:
        local_path = knowledge.audio_fetch.download_episode_audio(
            audio_url, _LISTEN_AUDIO_DIR, filename)
    except knowledge.audio_fetch.AudioFetchError as e:
        logger.warning("knowledge.listen: download failed for episode %s: %s", episode_id, e)
        raise ListenError(str(e)) from e

    # The download is done; everything after this is ASR + alignment, which
    # is the slow part. Say so — without this the "Listen" button's progress
    # line and the #821 header task indicator both sit on "Downloading
    # audio…" for the entire transcription, which reads as a stuck download.
    if on_progress:
        on_progress("Transcribing + aligning (cloud, usually under a minute)…")
    try:
        track = audio.build_track(text=transcript, audio_path=local_path,
                                  lang="zh", prefer_local=False, on_progress=on_progress)
    except audio.AudioTrackError as cloud_err:
        logger.warning(
            "knowledge.listen: cloud ASR failed for episode %s (%s) — falling back to local "
            "whisper.cpp (roughly 10x slower)", episode_id, cloud_err)
        fallback_msg = "云端转录失败，改用本地转录（会慢很多）…"
        if on_progress:
            on_progress(fallback_msg)
        try:
            track = audio.build_track(text=transcript, audio_path=local_path,
                                      lang="zh", prefer_local=True, on_progress=on_progress)
        except audio.AudioTrackError as local_err:
            logger.warning(
                "knowledge.listen: local fallback also failed for episode %s: %s",
                episode_id, local_err)
            raise ListenError(
                f"Cloud ASR failed ({cloud_err}); local fallback also failed: {local_err}"
            ) from local_err

    database.save_audio_track(
        "episode", episode_id, "zh", "fulltext",
        track.audio_path, track.duration_ms,
        [c.to_dict() for c in track.cues],
        track.source, track.voice, source_text=track.source_text,
    )
    logger.info("knowledge.listen: track ready for episode %s (%d cues, source=%s)",
               episode_id, len(track.cues), track.source)
    return {"cues": len(track.cues), "source": track.source}
