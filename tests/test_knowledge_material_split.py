"""knowledge / book 模式按调用次数切分素材（issue #1038）。

背景：每个分块过去都拿到**整份**转录，而提示词第 6 条要求「事实分散在素材的
开头、中间和结尾，每个话题至少覆盖一条」——于是 N 次调用各自从头覆盖一遍，
句子重复同一批开头的事实，后半段素材只是白花输入 token。#1029 已在
contextsummary 那条管线上修过同一个问题。

覆盖：
- 一份素材 + 多个分块 → 每次调用拿到一段连续的、互不重叠的素材，拼回去等于原文
- 每段带 section_label，提示词里因此出现「只覆盖这一段」的规则
- 切不出那么多段（整段无分隔）→ 整体回落成每块拿全文，且不带 section_label
- 默认的单次调用行为逐字节不变（没有 section_label，material 是完整素材）

AI 一律打桩在 ai._call_api 上（打在提供商客户端上会随默认模型变化静默失效）；
隔离数据库只打 database.core.DB_PATH 这个补丁。
"""
import json

import pytest
from unittest.mock import patch

pytest.importorskip("fastapi", reason="fastapi not installed")

from fastapi.testclient import TestClient
import ai
import database
import importer
import main
import routes.story as story_routes

client = TestClient(main.app)

_WORDS = ["苹果", "香蕉", "橙子", "葡萄", "西瓜", "草莓", "桃子", "梨"]

# 八个段落，长度相近——切成 4 段应当正好每段两个。
_PARAGRAPHS = [f"第{i}段的正文内容，讲的是一件具体的事情。" for i in range(1, 9)]
_MATERIAL = "\n\n".join(_PARAGRAPHS)


def _write_yaml(tmp_path):
    import yaml
    d = tmp_path / "Kouyu"
    d.mkdir(exist_ok=True)
    entries = [{"type": "vocabulary", "simplified": w, "pinyin": w,
                "english": w, "pos": "n", "hsk": "1"} for w in _WORDS]
    (d / "words.yaml").write_text(yaml.dump({"entries": entries}, allow_unicode=True))


@pytest.fixture
def deck_id(tmp_path, monkeypatch):
    monkeypatch.setattr(database.core, "DB_PATH", str(tmp_path / "test.db"))
    database.init_db()
    _write_yaml(tmp_path)
    importer.import_all(str(tmp_path))
    return next(d["id"] for d in database.get_all_decks() if d["name"] == "Kouyu")


def _create_episode(transcript, title="素材甲"):
    eid = database.create_pending_episode(
        "vid1", "https://example.com/feed.xml", title, None,
        "https://example.com/vid1", kind="podcast")
    database.update_episode(eid, status="summarized", transcript_zh=transcript)
    return eid


def _generate(deck_id, episode_id, **params):
    """跑一次 knowledge 生成，返回每次 AI 调用收到的 source dict。"""
    sources = []

    def _capturing(cards, source, **kwargs):
        sources.append(source)
        return [
            {"word_id": c["word_id"], "sentence_zh": f"{c['word_zh']}出现在这一集里。",
             "sentence_en": "", "target_word": c["word_zh"]}
            for c in cards
        ], "假提示词"

    with patch("ai.generate_podcast_sentences", side_effect=_capturing):
        r = client.get(f"/api/story/{deck_id}/listening",
                       params={"mode": "knowledge", "episode_ids": str(episode_id),
                               **params})
    assert r.status_code == 200
    assert not r.json().get("error")
    return sources


def test_material_divided_between_calls(deck_id):
    """8 词 / batch_size=2 → 4 次调用，各拿一段连续素材，拼回去等于原文。"""
    eid = _create_episode(_MATERIAL)
    sources = _generate(deck_id, eid, batch_size=2)

    assert len(sources) == 4
    materials = [s["material"] for s in sources]
    assert len(set(materials)) == 4          # 四段互不相同
    assert "\n\n".join(materials) == _MATERIAL
    assert [s["section_label"] for s in sources] == [
        "·第1/4段", "·第2/4段", "·第3/4段", "·第4/4段"]


def test_single_call_keeps_full_material_and_no_label(deck_id):
    """默认（一份素材一次调用）行为不变：整份素材，没有 section_label。"""
    eid = _create_episode(_MATERIAL)
    sources = _generate(deck_id, eid)

    assert len(sources) == 1
    assert sources[0]["material"] == _MATERIAL
    assert "section_label" not in sources[0]


def test_undividable_material_falls_back_to_full_source(deck_id):
    """整段无分隔、切不出四段 → 每次调用照旧拿全文，且不加 section_label
    （谎称"这只是一段"会让模型主动漏掉大半素材）。"""
    blob = "整段材料没有任何段落或句子分隔符"
    eid = _create_episode(blob)
    sources = _generate(deck_id, eid, batch_size=2)

    assert len(sources) == 4
    assert all(s["material"] == blob for s in sources)
    assert all("section_label" not in s for s in sources)


# ── 提示词侧：section_label → 「只覆盖这一段」规则 ──────────────────────────

def _prompt_for(source):
    """跑一次 generate_podcast_sentences，返回实际发出去的提示词。"""
    cards = [{"word_id": 1, "word_zh": "苹果", "pinyin": "píng guǒ",
              "definition": "apple"}]
    reply = json.dumps([{"reasoning_zh": "", "sentence_zh": "苹果很好吃。",
                         "target_word": "苹果"}], ensure_ascii=False)
    with patch("ai._call_api", return_value=reply):
        _, prompt = ai.generate_podcast_sentences(cards, source)
    return prompt


def test_prompt_names_the_section_when_material_is_a_slice():
    prompt = _prompt_for({"title": "素材甲", "material": _PARAGRAPHS[0],
                          "url": "u", "section_label": "·第2/4段"})
    assert "只是原素材的其中一段" in prompt
    assert "·第2/4段" in prompt


def test_prompt_unchanged_without_section_label():
    prompt = _prompt_for({"title": "素材甲", "material": _MATERIAL, "url": "u"})
    assert "只是原素材的其中一段" not in prompt
