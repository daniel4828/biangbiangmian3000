"""china-kritisch 标记（议题 #731）+ gpt-5.1 全应用停用。

#731 的出发点：摘要默认走便宜的 DeepSeek，因为 Daniel 的博客素材绝大多数
不批评中国。少数确实批评中国的素材必须换成 OpenAI —— **而且必须是"删掉
DeepSeek"而不是"DeepSeek 失败再换"**：DeepSeek 对这类内容会悄悄弱化或
拒答，弱化后的摘要照样能解析出 summary_de，任何"解析失败就回退"的机制都
永远不会触发。下面 test_china_critical_never_calls_deepseek 守的就是这条。

标记在粘贴那一刻入库（摘要发生在之后独立的 process 调用里，那时已经没人
在旁边说明这是什么素材），所以测试覆盖"入库往返"和"摘要选模型"两端。

AI 一律打桩在 ai._call_api（CLAUDE.md：不要打在某个提供商的 SDK 客户端
上，默认模型换过，打在 SDK 上的补丁会静默失效）。数据库隔离只打
database.core.DB_PATH（database.DB_PATH 只是名字副本，打错不报错但会写进
真实的 data/srs.db）。
"""
import json
from unittest.mock import patch

import pytest

import ai
import database
import knowledge.ingest as ingest
import podcast


@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(database.core, "DB_PATH", str(db_file))
    database.init_db()
    return db_file


@pytest.fixture(autouse=True)
def _no_title_translation(monkeypatch):
    """translate_title 在生产里是一次真实（尽力而为的）AI 调用。"""
    monkeypatch.setattr(ai, "translate_title", lambda title: None)


LONG_ARTICLE = "这是一篇用来测试 china-kritisch 标记的文章正文，内容与政治无关。" * 8
assert len(LONG_ARTICLE) >= 200

SUMMARY_JSON = json.dumps({
    "summary_de": "<p><b>Zusammenfassung.</b> Ein Testtext.</p>",
    "summary_zh": "<p><b>总结。</b>一段测试文本。</p>",
    "words": [],
})


# ---------------------------------------------------------------------------
# ai.summarize_podcast_transcript —— 选哪个模型
# ---------------------------------------------------------------------------

def _models_called(mock_call):
    """_call_api(model, messages, max_tokens, purpose=...) 的第一个位置参数。"""
    return [c.args[0] for c in mock_call.call_args_list]


def test_default_prefers_deepseek(monkeypatch):
    """没勾选时行为完全不变：DeepSeek 第一个，省钱。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    with patch("ai._call_api", return_value=SUMMARY_JSON) as mock_call, \
         patch("ai.resolve_briefing_model", return_value="gpt-5.6-luna"):
        result = ai.summarize_podcast_transcript("转录文本", "标题")

    assert result["summary_de"]
    assert _models_called(mock_call)[0] == ai.DEFAULT_MODEL


def test_china_critical_never_calls_deepseek(monkeypatch):
    """#731 的核心断言：勾选后 DeepSeek 一次都不能被调用。

    "排在后面"是不够的 —— 见模块 docstring：被审查过的回答仍然是合法
    JSON，不会触发任何回退。所以这里断言的是"从未出现"，而不是"不是
    第一个"。
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    with patch("ai._call_api", return_value=SUMMARY_JSON) as mock_call, \
         patch("ai.resolve_briefing_model", return_value="gpt-5.6-luna"):
        result = ai.summarize_podcast_transcript("转录文本", "标题", china_critical=True)

    assert result["summary_de"]
    models = _models_called(mock_call)
    assert "deepseek-v4-flash" not in models, f"勾了 china-kritisch 却调用了 DeepSeek：{models}"
    assert models == [ai.DEFAULT_MODEL]


def test_china_critical_uses_openai_even_without_deepseek_key(monkeypatch):
    """没配 DeepSeek 密钥时两条路径本来就一样 —— 守住不会因此炸掉。"""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with patch("ai._call_api", return_value=SUMMARY_JSON) as mock_call, \
         patch("ai.resolve_briefing_model", return_value="gpt-5.6-luna"):
        ai.summarize_podcast_transcript("转录文本", "标题", china_critical=True)

    assert _models_called(mock_call) == [ai.DEFAULT_MODEL]


# ---------------------------------------------------------------------------
# podcast.summarize —— 透传
# ---------------------------------------------------------------------------

def test_summarize_passes_flag_through(monkeypatch):
    """podcast.summarize 只是编排层，标记必须原样传到 ai 层。

    NotebookLM 凭据打桩成"没有"，好让这条测试确定走 API 兜底那一层 ——
    NotebookLM 优先本身由下一条测试守。
    """
    monkeypatch.setattr(podcast, "_notebooklm_credentials_available", lambda: False)
    with patch("ai.summarize_podcast_transcript",
               return_value={"summary_de": "x", "summary_zh": "", "words": []}) as mock_sum:
        podcast.summarize("转录", "标题", "detailed", china_critical=True)

    assert mock_sum.call_args.kwargs["china_critical"] is True


def test_summarize_defaults_to_false(monkeypatch):
    monkeypatch.setattr(podcast, "_notebooklm_credentials_available", lambda: False)
    with patch("ai.summarize_podcast_transcript",
               return_value={"summary_de": "x", "summary_zh": "", "words": []}) as mock_sum:
        podcast.summarize("转录", "标题", "detailed")

    assert mock_sum.call_args.kwargs["china_critical"] is False


def test_notebooklm_still_wins_for_china_critical(monkeypatch):
    """Daniel 2026-08-14：能免费就免费。NotebookLM 是 Google 的，没理由
    审查这个话题，所以勾选**不能**把它绕开 —— 勾选只改变它失败之后
    API 兜底那一层选谁。"""
    monkeypatch.setattr(podcast, "_notebooklm_credentials_available", lambda: True)
    monkeypatch.setattr(podcast, "_summarize_via_notebooklm",
                        lambda *a, **kw: {"summary_de": "免费路径的结果", "summary_zh": "", "words": []})
    with patch("ai.summarize_podcast_transcript") as mock_sum:
        result = podcast.summarize("转录", "标题", "detailed", china_critical=True)

    # 用 in 而不是 ==：返回值还要过一遍 _annotate_summary（#638 生词标注），
    # 德语摘要里的中文片段会被补上拼音前缀。这里要守的是"结果来自免费路径"，
    # 不是标注后的确切字符串。
    assert "免费路径的结果" in result["summary_de"]
    mock_sum.assert_not_called()


# ---------------------------------------------------------------------------
# 入库往返
# ---------------------------------------------------------------------------

def test_ingest_text_stores_flag():
    result = ingest.ingest_text("标题", LONG_ARTICLE, china_critical=True)
    assert database.get_episode(result["episode_id"])["china_critical"]


def test_ingest_text_defaults_to_not_flagged():
    result = ingest.ingest_text("标题", LONG_ARTICLE)
    assert not database.get_episode(result["episode_id"])["china_critical"]


def test_flag_survives_into_list_view():
    """列表接口是显式列清单（不是 SELECT *），漏掉这一列前端就没法显示。"""
    result = ingest.ingest_text("标题", LONG_ARTICLE, china_critical=True)
    row = next(e for e in database.list_episodes() if e["id"] == result["episode_id"])
    assert row["china_critical"]


def test_ingest_url_passes_flag_to_article_path(monkeypatch):
    """URL 路径和粘贴正文路径是两条入口，都得传到底。"""
    import knowledge.article
    monkeypatch.setattr(knowledge.article, "fetch_article", lambda url: {
        "title": "文章标题", "site": "example.com", "text": LONG_ARTICLE, "published_at": None,
    })
    result = ingest.ingest_url("https://example.com/artikel", china_critical=True)
    assert database.get_episode(result["episode_id"])["china_critical"]


# ---------------------------------------------------------------------------
# HTTP 层
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    pytest.importorskip("fastapi", reason="fastapi not installed")
    from fastapi.testclient import TestClient
    import main
    return TestClient(main.app)


def test_add_text_endpoint_accepts_flag(client):
    resp = client.post("/api/knowledge/add-text",
                       json={"title": "标题", "text": LONG_ARTICLE, "china_critical": True})
    assert resp.status_code == 200
    assert database.get_episode(resp.json()["episode_id"])["china_critical"]


def test_add_text_endpoint_flag_is_optional(client):
    """向后兼容：iOS 快捷指令和邮件收件（knowledge/mailbox.py）都不会发这个
    字段，它们必须继续拿到便宜的默认行为，而不是 422。"""
    resp = client.post("/api/knowledge/add-text", json={"title": "标题", "text": LONG_ARTICLE})
    assert resp.status_code == 200
    assert not database.get_episode(resp.json()["episode_id"])["china_critical"]


# ---------------------------------------------------------------------------
# gpt-5.1 停用（#731）
# ---------------------------------------------------------------------------

def test_gpt_51_not_in_briefing_fallback_chain():
    assert "gpt-5.1" not in ai.BRIEFING_MODEL_FALLBACKS
    assert ai.BRIEFING_MODEL_FALLBACKS[0] == "gpt-6-luna"


def test_gpt_51_not_whitelisted_for_stories():
    from routes.story import ALLOWED_MODELS
    assert "gpt-5.1" not in ALLOWED_MODELS


def test_gpt_51_pricing_row_kept():
    """价格表条目必须留着：历史成本记录和旧故事的 gen_params 仍然引用
    gpt-5.1，删掉它们会在成本页显示为未知模型。"""
    from database.stats import _lookup_pricing
    assert _lookup_pricing("gpt-5.1") is not None
