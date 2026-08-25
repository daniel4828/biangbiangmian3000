"""learning 卡必须排在 review 卡之前（议题 #920）。

两个原来会让 review 卡插到前面的原因：

1. `_still_learning()` 把 interval < learned_interval 的 review 卡也归进「learning
   组」，然后整组按 due 排序 —— 而 review 卡的 due 是纯日期 `2026-08-24`，learning
   卡是 `2026-08-24T09:00:00`，字符串比较下日期永远更小。
2. 有故事时顺序完全由叙事位置决定（#462），learning 卡对应第 30 句就得排在 29 张
   review 卡后面。

Daniel 2026-08-25 决定：learning/relearn 一律最先，故事顺序让位。
"""
import datetime
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import database
import database.core


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """必须打 database.core.DB_PATH（见 conftest.py 与议题 #615）。"""
    monkeypatch.setattr(database.core, "DB_PATH", str(tmp_path / "test_srs.db"))
    database.init_db()


def _add_entry(word: str) -> int:
    conn = database.core.get_db()
    cur = conn.execute(
        "INSERT INTO entries (word_zh, pinyin, definition, note_type, lang) "
        "VALUES (?, 'x', ?, 'vocabulary', 'zh')",
        (word, word),
    )
    conn.commit()
    wid = cur.lastrowid
    conn.close()
    return wid


def _set_interval(card_id: int, interval: int) -> None:
    conn = database.core.get_db()
    conn.execute("UPDATE cards SET interval = ? WHERE id = ?", (interval, card_id))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# learning_rank —— 纯函数
# ---------------------------------------------------------------------------

def test_rank_puts_learning_and_relearn_first():
    assert database.learning_rank({"state": "learning"}) == 0
    assert database.learning_rank({"state": "relearn"}) == 0
    assert database.learning_rank({"state": "review"}) == 1
    assert database.learning_rank({"state": "new"}) == 1


# ---------------------------------------------------------------------------
# 单类别队列：短间隔 review 卡不许插到 learning 卡前面
# ---------------------------------------------------------------------------

def test_short_interval_review_does_not_jump_ahead_of_learning(tmp_db):
    root = database.get_or_create_deck("All")
    daily = database.get_or_create_deck("Daily · test", parent_id=root)
    leaves = database.get_or_create_category_decks(daily, "Daily · test")

    today = database.anki_today().isoformat()
    stamp = f"{today}T09:00:00"

    # interval=2 < learned_interval(4)，所以它和 learning 卡分在同一组；
    # due 是纯日期，字符串比较下比 learning 卡的日期时间小 —— #920 的起因。
    review_id = database.insert_card(_add_entry("复习词"), "listening",
                                     leaves["listening"], state="review", due=today)
    _set_interval(review_id, 2)
    learning_id = database.insert_card(_add_entry("学习词"), "listening",
                                       leaves["listening"], state="learning", due=stamp)

    order = [c["id"] for c in database.get_due_cards(leaves["listening"], "listening")]
    assert order.index(learning_id) < order.index(review_id)


def test_learned_review_still_comes_after_short_interval_one(tmp_db):
    """learning 优先不能顺手把「短间隔 review 先于已学会 review」也弄没了。"""
    root = database.get_or_create_deck("All")
    daily = database.get_or_create_deck("Daily · test", parent_id=root)
    leaves = database.get_or_create_category_decks(daily, "Daily · test")
    today = database.anki_today().isoformat()

    short_id = database.insert_card(_add_entry("短间隔"), "listening",
                                    leaves["listening"], state="review", due=today)
    _set_interval(short_id, 2)
    long_id = database.insert_card(_add_entry("长间隔"), "listening",
                                   leaves["listening"], state="review", due=today)
    _set_interval(long_id, 30)

    order = [c["id"] for c in database.get_due_cards_any_cat(root)]
    assert order.index(short_id) < order.index(long_id)


# ---------------------------------------------------------------------------
# 有故事时同样成立
# ---------------------------------------------------------------------------

def test_learning_card_beats_story_order(tmp_db):
    root = database.get_or_create_deck("All")
    daily = database.get_or_create_deck("Daily · test", parent_id=root)
    leaves = database.get_or_create_category_decks(daily, "Daily · test")

    today = database.anki_today().isoformat()
    stamp = f"{today}T09:00:00"

    review_id = database.insert_card(_add_entry("复习词"), "listening",
                                     leaves["listening"], state="review", due=today)
    _set_interval(review_id, 30)
    learning_id = database.insert_card(_add_entry("学习词"), "listening",
                                       leaves["listening"], state="learning", due=stamp)

    # 故事把 review 卡放在第一句、learning 卡放在第二句
    sentences = [
        {"position": 0, "sentence_zh": "第一句",
         "word_ids": [database.get_card(review_id)["word_id"]]},
        {"position": 1, "sentence_zh": "第二句",
         "word_ids": [database.get_card(learning_id)["word_id"]]},
    ]
    database.create_story(today, "unified", root, sentences,
                          gen_params={"mode": "knowledge"}, lang="zh")

    order = [c["id"] for c in database.get_due_cards_any_cat(root)]
    assert order.index(learning_id) < order.index(review_id), (
        "有故事时 learning 卡被叙事顺序压到了 review 卡后面（#920 回归）"
    )


def test_order_by_story_puts_learning_first(tmp_db):
    """routes/review._order_by_story 是队列构建的最后一道重排，同样要 learning 优先。"""
    from routes import review as review_routes

    root = database.get_or_create_deck("All")
    daily = database.get_or_create_deck("Daily · test", parent_id=root)
    leaves = database.get_or_create_category_decks(daily, "Daily · test")

    today = database.anki_today().isoformat()
    review_id = database.insert_card(_add_entry("复习词"), "listening",
                                     leaves["listening"], state="review", due=today)
    learning_id = database.insert_card(_add_entry("学习词"), "listening",
                                       leaves["listening"], state="learning",
                                       due=f"{today}T09:00:00")
    rev = database.get_card(review_id)
    lrn = database.get_card(learning_id)

    database.create_story(today, "unified", root, [
        {"position": 0, "sentence_zh": "第一句", "word_ids": [rev["word_id"]]},
        {"position": 1, "sentence_zh": "第二句", "word_ids": [lrn["word_id"]]},
    ], gen_params={"mode": "briefing"}, lang="zh")   # 只有 briefing/news/paste 参与重排

    ordered = review_routes._order_by_story([rev, lrn], root, "unified", "zh")
    assert [c["id"] for c in ordered] == [learning_id, review_id]
