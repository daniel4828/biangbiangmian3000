"""故事词表必须和复习队列一样受父牌组新卡上限约束（议题 #883）。

现象：顶栏显示 `0 new / 40 learning / 37 review`（77 张），Story setup 弹窗却说
"This story will have 305 sentences."。

原因：到期卡有两条路径，故事那条漏了 `root_deck_id`。复习队列传它，于是父牌组的
`new_per_day` 是所有叶子牌组的**合并**上限（Anki 行为）；故事不传，于是每个日期
叶子牌组各自放行自己那份配额，几十个叶子累加就是几百个新词。

这不只是数字难看：`_get_cards_for_story()` 的返回值就是真正发给 AI 的词表，多出来
的都是队列永远不会发出来的词——付了钱，永远看不到。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import database
import database.core
from routes.story import _get_cards_for_story


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


NEW_PER_DAY = 5
DAILY_DECKS = 4
NEW_PER_DECK = 10


@pytest.fixture
def tree(tmp_db):
    """一棵 All 牌组树，4 个日期叶子牌组各 10 张新卡，父牌组上限 5 张/天。"""
    root = database.get_or_create_deck("All")
    # 所有牌组共用默认预设，所以每个叶子牌组自己的上限也是 NEW_PER_DAY —— 不传
    # root_deck_id 时它们各放行一份，合计 DAILY_DECKS × NEW_PER_DAY。
    database.update_preset(database.get_preset_for_deck(root)["id"],
                           {"new_per_day": NEW_PER_DAY})
    for d in range(DAILY_DECKS):
        daily = database.get_or_create_deck(f"Daily · d{d}", parent_id=root)
        leaves = database.get_or_create_category_decks(daily, f"Daily · d{d}")
        today = database.anki_today().isoformat()
        for i in range(NEW_PER_DECK):
            wid = _add_entry(f"词{d}_{i}")
            database.insert_card(wid, "listening", leaves["listening"],
                                 state="new", due=today)
    return root


def test_story_word_list_respects_parent_new_card_cap(tree):
    """故事词表不得超过父牌组的合并新卡上限（#883 回归）。"""
    cards = _get_cards_for_story(tree, "listening")
    assert len(cards) == NEW_PER_DAY, (
        f"故事拿到 {len(cards)} 个新词，父牌组上限只有 {NEW_PER_DAY} —— "
        "多出来的词队列永远不会发出，却已经付钱生成了句子"
    )


def test_story_word_list_matches_review_queue(tree):
    """故事词表与复习队列必须逐张一致——两个数字说两套话正是本议题的表象。"""
    ids = database.get_descendant_leaf_deck_ids(tree, "listening")
    queue = database.get_due_cards_multi(ids, "listening", root_deck_id=tree)
    story = _get_cards_for_story(tree, "listening")
    assert {c["id"] for c in story} == {c["id"] for c in queue}


def test_unified_story_respects_parent_new_card_cap(tree):
    """unified 故事走 database.get_due_cards_unified()，有同一个缺陷（#883）。"""
    cards = database.get_due_cards_unified(tree)
    assert len(cards) == NEW_PER_DAY
