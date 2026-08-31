"""Kontextsummary 模式测试（issue #1011）：News flow（briefing）改名 contextsummary，
素材来源从「自动抓取当日新闻」换成知识库素材多选。

核心保证：
- mode='contextsummary' 用 briefing 管线（ai.generate_briefing_sentences，带上下文句
  和事实核查），素材由 episode_ids 从知识库取，适配成 {url, title, text}。
- source_url 是应用内详情页链接（#790），不是外部链接——卡片上的来源行点开是那条素材。
- 没选素材时报明确错误，绝不静默降级成普通故事。
- 新故事生成拒绝旧标识符 mode='briefing' 和 mode='news'（照 #512/#654 的先例），
  但历史故事照常展示、照常做 Again 单句重生成。
- 新闻抓取整条已删除：代码里不许再出现 news_fetcher / summarize_news_items /
  /api/news/status。
"""
import json
import pathlib
import subprocess
from unittest.mock import patch

import pytest

pytest.importorskip("fastapi", reason="fastapi not installed")

from fastapi.testclient import TestClient
import database
import importer
import main
import routes.story as story_routes

client = TestClient(main.app)

ENTRY_你好 = {"type": "vocabulary", "simplified": "你好", "pinyin": "nǐ hǎo",
               "english": "hello", "pos": "intj", "hsk": "1"}


def write_yaml(tmp_path, name, entries):
    import yaml
    d = tmp_path / "Kouyu"
    d.mkdir(exist_ok=True)
    (d / name).write_text(yaml.dump({"entries": entries}, allow_unicode=True))


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(database.core, "DB_PATH", str(db_file))
    database.init_db()
    return db_file


@pytest.fixture
def populated_db(tmp_db, tmp_path):
    write_yaml(tmp_path, "words.yaml", [ENTRY_你好])
    importer.import_all(str(tmp_path))
    return next(d["id"] for d in database.get_all_decks() if d["name"] == "Kouyu")


@pytest.fixture
def article_episode():
    episode_id = database.create_pending_episode(
        "https://example.com/artikel", "example.com", "Ein Artikel", None,
        "https://example.com/artikel", kind="article")
    database.update_episode(episode_id, status="summarized",
                            transcript_zh="这是一篇文章的中文全文。",
                            summary_de="Eine deutsche Zusammenfassung.")
    return episode_id


def _fake_briefing_sentences(cards, articles, **kwargs):
    return [
        {"word_id": c["word_id"], "sentence_zh": f"{c['word_zh']}出现在这篇文章里。",
         "sentence_en": "", "target_word": c["word_zh"],
         "context_de": "Kontext.", "reasoning_zh": "上下文。",
         "source_url": (articles[0] or {}).get("url", "")}
        for c in cards
    ]


# ---------------------------------------------------------------------------
# 生成：知识库素材 → briefing 管线
# ---------------------------------------------------------------------------

def test_contextsummary_feeds_knowledge_material_into_the_briefing_pipeline(
        populated_db, article_episode):
    """整个 issue 的重点：走的是 generate_briefing_sentences（上下文句 + 事实核查
    那条），素材来自知识库而不是新闻抓取。"""
    deck_id = populated_db
    with patch("ai.generate_briefing_sentences",
               side_effect=_fake_briefing_sentences) as mock_gen:
        r = client.get(f"/api/story/{deck_id}/listening",
                       params={"mode": "contextsummary", "episode_ids": str(article_episode)})

    assert r.status_code == 200
    body = r.json()
    assert body is not None and not body.get("error")
    assert len(body["sentences"]) == 1
    mock_gen.assert_called_once()

    # 素材适配成 briefing 要的 {url, title, text}，text 是转录（#661），
    # url 是应用内详情页（#790）而不是外部链接。
    articles = mock_gen.call_args[0][1]
    assert len(articles) == 1
    assert articles[0]["title"] == "Ein Artikel"
    assert articles[0]["text"] == "这是一篇文章的中文全文。"
    assert articles[0]["url"] == f"/#knowledge-{article_episode}"

    story = database.get_active_story(database.anki_today().isoformat(), "listening", deck_id)
    gen_params = json.loads(story["gen_params"])
    assert gen_params["mode"] == "contextsummary"
    assert gen_params["episode_ids"] == [article_episode]
    # articles 存进 gen_params，Again 单句重生成才能复现同一份素材。
    assert gen_params["articles"][0]["text"] == "这是一篇文章的中文全文。"


def test_contextsummary_without_a_source_errors_instead_of_degrading(populated_db):
    """没选素材必须报错——静默生成一篇普通故事等于悄悄换了模式。"""
    deck_id = populated_db
    with patch("ai.generate_briefing_sentences") as mock_gen, \
         patch("ai.generate_story") as mock_story:
        r = client.get(f"/api/story/{deck_id}/listening", params={"mode": "contextsummary"})
    body = r.json()
    assert body["error"] is True
    assert "least one" in body["reason"]
    mock_gen.assert_not_called()
    mock_story.assert_not_called()


# ---------------------------------------------------------------------------
# 旧标识符：新生成拒绝，历史故事照常
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("old_mode", ["briefing", "news"])
def test_new_story_rejects_the_retired_news_identifiers(populated_db, old_mode):
    deck_id = populated_db
    r = client.get(f"/api/story/{deck_id}/listening", params={"mode": old_mode})
    assert r.status_code == 200  # 错误以 JSON error dict 返回
    body = r.json()
    assert body["error"] is True
    assert old_mode in body["reason"] and "contextsummary" in body["reason"]


def test_historical_briefing_story_still_displays(populated_db):
    deck_id = populated_db
    card = story_routes._get_cards_for_story(deck_id, "listening")[0]
    today = database.anki_today().isoformat()
    database.create_story(
        today, "listening", deck_id,
        [{"position": 0, "sentence_zh": "你好出现在今天的新闻里。", "sentence_en": "",
          "word_ids": [card["word_id"]]}],
        prompt_text="briefing mode — 3 articles",
        gen_params={"mode": "briefing", "max_hsk": 3, "model": None,
                    "articles": [{"url": "https://example.com/n1", "title": "N1",
                                  "text": "..."}], "lang": "zh"},
        lang="zh")

    r = client.get(f"/api/story/{deck_id}/listening")
    assert r.status_code == 200
    body = r.json()
    assert len(body["sentences"]) == 1
    assert body["sentences"][0]["sentence_zh"] == "你好出现在今天的新闻里。"


def test_historical_briefing_story_again_regen_still_works(populated_db):
    """历史 briefing 故事的 Again 重生成仍走 news 家族分支，并保留新闻措辞
    （generic=False）——那些故事当初确实是新闻。"""
    deck_id = populated_db
    card = story_routes._get_cards_for_story(deck_id, "listening")[0]
    gen_params = {"mode": "briefing", "max_hsk": 3, "model": None,
                  "articles": [{"url": "https://example.com/n1", "title": "N1", "text": "..."}]}
    with patch("ai.generate_news_sentences",
               return_value=[{"word_id": card["word_id"], "sentence_zh": "新句子。",
                              "sentence_en": "", "target_word": card["word_zh"]}]) as mock_gen, \
         patch("ai.resolve_briefing_model", return_value="gpt-5.6-luna"):
        sentence = story_routes.generate_sentence_for_word(card, gen_params)
    assert sentence["sentence_zh"] == "新句子。"
    assert mock_gen.call_args.kwargs["generic"] is False


def test_contextsummary_again_regen_uses_the_generic_framing(populated_db):
    """新模式的素材不是新闻，所以用通用的内容摘要措辞（generic=True），
    模型也用故事存下来的那一个。"""
    deck_id = populated_db
    card = story_routes._get_cards_for_story(deck_id, "listening")[0]
    gen_params = {"mode": "contextsummary", "max_hsk": 3, "model": "deepseek-v4-flash",
                  "articles": [{"url": "/#knowledge-1", "title": "Ein Artikel", "text": "..."}]}
    with patch("ai.generate_news_sentences",
               return_value=[{"word_id": card["word_id"], "sentence_zh": "新句子。",
                              "sentence_en": "", "target_word": card["word_zh"]}]) as mock_gen:
        story_routes.generate_sentence_for_word(card, gen_params)
    assert mock_gen.call_args.kwargs["generic"] is True
    assert mock_gen.call_args.kwargs["model"] == "deepseek-v4-flash"


# ---------------------------------------------------------------------------
# 队列排序 / 预生成
# ---------------------------------------------------------------------------

def test_queue_order_follows_the_contextsummary_story(populated_db, article_episode):
    """#454 的按故事顺序排队对新模式同样成立——否则读的顺序和故事对不上。"""
    deck_id = populated_db
    today = database.anki_today().isoformat()
    card = story_routes._get_cards_for_story(deck_id, "listening")[0]
    database.create_story(
        today, "listening", deck_id,
        [{"position": 0, "sentence_zh": "你好出现在这篇文章里。", "sentence_en": "",
          "word_ids": [card["word_id"]]}],
        prompt_text="", gen_params={"mode": "contextsummary"}, lang="zh")
    assert database.get_story_position_map(deck_id, "listening", today) == {card["word_id"]: 0}


def test_contextsummary_is_not_pregeneratable():
    """选素材是一次性的人工动作，和 knowledge/book 一样不进早晨预生成；
    briefing 也必须一并消失，否则 06:00 会去生成一个已经被拒绝的模式。"""
    assert "contextsummary" not in story_routes._PREGEN_MODES
    assert "briefing" not in story_routes._PREGEN_MODES


# ---------------------------------------------------------------------------
# 新闻抓取整条删除
# ---------------------------------------------------------------------------

def test_news_fetching_is_gone_from_the_codebase():
    """#1011 删的是整条管线，不是把入口藏起来——留着死代码下次就会有人接回去。"""
    assert not pathlib.Path("news_fetcher.py").exists()
    hits = subprocess.run(
        ["git", "grep", "-l", "-E",
         r"news_fetcher|summarize_news_items|/api/news/status|get_today_used_article_urls",
         "--", "*.py", "*.js", "*.html"],
        capture_output=True, text=True).stdout.split()
    # 本测试文件自己提到这些名字，其余都得没有。
    assert [h for h in hits if h != "tests/test_contextsummary.py"] == []
