"""统一故事不为已停用的类别取词（议题 #871）。

Daniel 在预设里关掉 creating 之后，复习队列确实不再发那些卡，但 All 牌组的
统一故事仍在为它们生成句子——付了钱、永远看不到，还挤占了真正到期的词的篇幅。
判定必须和复习队列（cards._leaf_decks_with_category）出自同一条规则。
"""
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


def _set_category_enabled(deck_id: int, column: str, value: int) -> None:
    """把该牌组的预设复制成独占的一份再改，免得动到共享的默认预设。"""
    conn = database.core.get_db()
    preset_id = conn.execute(
        "SELECT preset_id FROM decks WHERE id = ?", (deck_id,)).fetchone()["preset_id"]
    conn.execute(f"UPDATE deck_presets SET {column} = ? WHERE id = ?", (value, preset_id))
    conn.commit()
    conn.close()


def _build_deck_with_due_cards():
    """All → Daily · test → 三个类别叶子，每个类别各一张到期新卡。"""
    root = database.get_or_create_deck("All")
    daily = database.get_or_create_deck("Daily · test", parent_id=root)
    leaves = database.get_or_create_category_decks(daily, "Daily · test")
    today = database.anki_today().isoformat()
    for cat in ("listening", "reading", "creating"):
        wid = _add_entry(f"词_{cat}")
        database.insert_card(wid, cat, leaves[cat], state="new", due=today)
    return root, leaves


def test_disabled_category_is_left_out_of_the_unified_story(tmp_db):
    root, leaves = _build_deck_with_due_cards()
    # reading 在默认预设里本来就是关的，所以先显式打开：三个类别都开着时三张卡
    # 都该进词表。没有这条基线，下面的断言就算过了也说明不了问题——可能它一
    # 开始就取不到 creating。
    _set_category_enabled(leaves["reading"], "reading_enabled", 1)
    cats = {c["category"] for c in database.get_due_cards_unified(root)}
    assert cats == {"listening", "reading", "creating"}

    _set_category_enabled(leaves["creating"], "creating_enabled", 0)

    cards = database.get_due_cards_unified(root)
    assert {c["category"] for c in cards} == {"listening", "reading"}
    assert all(c["category"] != "creating" for c in cards), (
        "统一故事仍在为已停用的 creating 取词（议题 #871 回归）"
    )


def test_all_categories_disabled_yields_empty_list(tmp_db):
    """全关掉时返回空列表，不抛异常——调用方（_get_cards_for_story）
    对空列表已有处理，异常会把整次生成变成 500。"""
    root, leaves = _build_deck_with_due_cards()
    for cat, col in (("listening", "listening_enabled"),
                     ("reading", "reading_enabled"),
                     ("creating", "creating_enabled")):
        _set_category_enabled(leaves[cat], col, 0)

    assert database.get_due_cards_unified(root) == []


def test_filter_also_applies_when_called_with_a_category_leaf_as_root(tmp_db):
    """从类别叶子牌组直接进来同样要过滤，否则那条分支绕过整个检查。"""
    root, leaves = _build_deck_with_due_cards()
    _set_category_enabled(leaves["creating"], "creating_enabled", 0)

    assert database.get_due_cards_unified(leaves["creating"]) == []
