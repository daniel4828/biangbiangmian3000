"""Single-flight lock for concurrent TTS generation (issue #1181).

The frontend fetches /api/tts-file for a whole batch of sentences at once,
while preload_all_async() may be generating the same batch server-side. Two
coroutines racing to generate the same uncached sentence used to share one
tmp filename: whichever renamed first left the other looking for a tmp file
that no longer existed, raising "edge-tts produced no output".

These tests exercise the real tts._ensure_cached — the autouse _no_real_tts
fixture in conftest.py replaces it wholesale for every other test file, so we
restore the real implementation here and stub edge_tts.Communicate one level
lower instead (same choke point _play/_ensure_cached itself uses).
"""
import asyncio
import os

import pytest

import tts

# Captured at collection time, before any test's monkeypatch (incl. conftest's
# autouse _no_real_tts) has run — this is the real implementation.
_REAL_ENSURE_CACHED = tts._ensure_cached


@pytest.fixture(autouse=True)
def _use_real_ensure_cached(monkeypatch):
    """Undo conftest's blanket _ensure_cached stub so the lock under test runs.

    conftest fixtures are instantiated before same-scope fixtures defined in
    the test module itself, so this reliably applies after (and overrides)
    _no_real_tts.
    """
    monkeypatch.setattr(tts, "_ensure_cached", _REAL_ENSURE_CACHED)


@pytest.fixture(autouse=True)
def _isolated_cache_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(tts, "TTS_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(tts, "_hot", set())


class _FakeCommunicate:
    """Stand-in for edge_tts.Communicate — records calls, writes a fake mp3."""

    calls = 0
    fail = False

    def __init__(self, text, voice):
        self.text = text
        self.voice = voice

    async def save(self, fname):
        type(self).calls += 1
        await asyncio.sleep(0.05)
        if type(self).fail:
            raise RuntimeError("simulated edge-tts failure")
        with open(fname, "wb") as f:
            f.write(b"ID3fake")


@pytest.fixture(autouse=True)
def _fake_edge_tts(monkeypatch):
    _FakeCommunicate.calls = 0
    _FakeCommunicate.fail = False
    monkeypatch.setattr(tts.edge_tts, "Communicate", _FakeCommunicate)


def test_concurrent_same_text_generates_once():
    async def run():
        text = "同一句"
        return await asyncio.gather(*[tts._ensure_cached(text) for _ in range(10)])

    results = asyncio.run(run())
    assert _FakeCommunicate.calls == 1
    assert len(set(results)) == 1
    assert os.path.exists(results[0])


def test_concurrent_different_texts_each_generate_once():
    async def run():
        return await asyncio.gather(
            tts._ensure_cached("甲"),
            tts._ensure_cached("乙"),
        )

    path1, path2 = asyncio.run(run())
    assert _FakeCommunicate.calls == 2
    assert path1 != path2
    assert os.path.exists(path1)
    assert os.path.exists(path2)


def test_failure_does_not_leave_tmp_files_or_wedge_the_lock():
    _FakeCommunicate.fail = True

    async def run_failure():
        with pytest.raises(RuntimeError):
            await tts._ensure_cached("会失败的句子")

    asyncio.run(run_failure())

    path = tts._cache_path("会失败的句子")
    assert path not in tts._hot
    assert not os.path.exists(path)
    leftover_tmp = [f for f in os.listdir(tts.TTS_CACHE_DIR) if f.endswith(".tmp")] \
        if os.path.isdir(tts.TTS_CACHE_DIR) else []
    assert leftover_tmp == []

    # The lock must not be wedged: a subsequent call should succeed normally.
    _FakeCommunicate.fail = False

    async def run_retry():
        return await tts._ensure_cached("会失败的句子")

    result = asyncio.run(run_retry())
    assert os.path.exists(result)
