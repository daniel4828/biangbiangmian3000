"""Tests for POST /api/translate-sentences (issue #1111) — the whole-block
German translation the gloss-on overlay uses in place of per-word glosses.

translator.translate_batch is stubbed (**kwargs-tolerant per this repo's
convention, see tests/test_api.py) — no real network call, ever.
"""
import pytest

pytest.importorskip("fastapi", reason="fastapi not installed")

from fastapi.testclient import TestClient
from unittest.mock import patch

import database
import main
import translator

client = TestClient(main.app)


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Fresh temp database — the endpoint doesn't touch it, but the whole app
    boots through database.init_db() the same way every other test does."""
    monkeypatch.setattr(database.core, "DB_PATH", str(tmp_path / "test.db"))
    database.init_db()
    return tmp_path


def test_translates_each_text_in_order(tmp_db):
    def fake_batch(texts, target="en", source="zh-CN", **kwargs):
        return [f"[{target}] {t}" for t in texts]

    with patch.object(translator, "translate_batch", side_effect=fake_batch):
        r = client.post("/api/translate-sentences", json={
            "texts": ["今天天气很好。", "我喜欢学中文。", "第三句话。"],
            "lang": "zh", "target": "de",
        })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["translations"] == [
        "[de] 今天天气很好。",
        "[de] 我喜欢学中文。",
        "[de] 第三句话。",
    ]


def test_translation_identical_to_source_becomes_empty_string(tmp_db):
    """translate_batch's documented failure contract is "return the input
    unchanged" — passing that straight through would show Chinese text
    labelled as a German translation. It must come back as "" instead so the
    frontend renders nothing (see routes/knowledge.py's ::after CSS rule)."""
    def fake_batch(texts, target="en", source="zh-CN", **kwargs):
        # First text "translates"; second one silently failed and came back
        # as itself (case/whitespace only differs, still counts as identical).
        return ["Guten Tag.", "  " + texts[1] + "  "]

    with patch.object(translator, "translate_batch", side_effect=fake_batch):
        r = client.post("/api/translate-sentences", json={
            "texts": ["你好。", "第二句话。"],
            "lang": "zh", "target": "de",
        })
    assert r.status_code == 200, r.text
    assert r.json()["translations"] == ["Guten Tag.", ""]


def test_empty_texts_list_is_not_an_error(tmp_db):
    r = client.post("/api/translate-sentences", json={"texts": [], "lang": "zh"})
    assert r.status_code == 200, r.text
    assert r.json()["translations"] == []


def test_translate_batch_exception_returns_empty_strings_not_500(tmp_db):
    with patch.object(translator, "translate_batch", side_effect=RuntimeError("boom")):
        r = client.post("/api/translate-sentences", json={
            "texts": ["一", "二", "三"], "lang": "zh", "target": "de",
        })
    assert r.status_code == 200, r.text
    assert r.json()["translations"] == ["", "", ""]


def test_too_many_texts_is_rejected(tmp_db):
    r = client.post("/api/translate-sentences", json={
        "texts": ["x"] * 301, "lang": "zh", "target": "de",
    })
    assert r.status_code == 400, r.text


# ── "nothing came back" must say so (#1140) ─────────────────────────────────
# Regression: a throttled endpoint returned "" for every block, the reader
# cached those empties and the 译 button became a permanent no-op with no
# explanation anywhere. The endpoint still never 500s — it reports.

def test_all_empty_translations_are_reported_as_an_error(tmp_db):
    """整批一个都没翻出来 = 端点在拒绝我们，不是"这段翻译成它自己"。"""
    with patch.object(translator, "translate_batch",
                      side_effect=lambda texts, **kw: list(texts)):
        r = client.post("/api/translate-sentences", json={
            "texts": ["这是第一段。", "这是第二段。"], "lang": "zh", "target": "de"})
    assert r.status_code == 200
    body = r.json()
    assert body["translations"] == ["", ""]
    assert body.get("error"), "前端要靠它显示原因，而不是静静地什么都不做"


def test_exception_is_reported_as_an_error_too(tmp_db):
    with patch.object(translator, "translate_batch", side_effect=RuntimeError("boom")):
        r = client.post("/api/translate-sentences", json={
            "texts": ["这是第一段。"], "lang": "zh", "target": "de"})
    assert r.status_code == 200
    assert r.json().get("error")


def test_partial_success_is_not_an_error(tmp_db):
    """有一段翻出来了就不算失败——另一段可能本来就没什么可译的。"""
    def fake_batch(texts, **kwargs):
        return [f"de:{texts[0]}"] + list(texts[1:])

    with patch.object(translator, "translate_batch", side_effect=fake_batch):
        r = client.post("/api/translate-sentences", json={
            "texts": ["这是第一段。", "这是第二段。"], "lang": "zh", "target": "de"})
    body = r.json()
    assert body["translations"][0].startswith("de:")
    assert "error" not in body
