"""刚按 Again 的卡必须出现在 learning 计数里（议题 #844）。

按 Again 之后卡片进入 1m/10m 步骤，`due` 是几分钟后的时间戳。原来所有计数
函数的 `learning` 只数**此刻已到期**的卡，于是顶栏在按下 Again 的瞬间仍然显示
0 —— 看起来像卡片丢了。

修法是加一个 `learning_soon` 字段（分钟级、晚于此刻、早于明天日界点），由前端
和 `learning` 相加显示。**不能**直接用已有的 `learning_future`：那里面还有
1d/3d 跨日步骤的卡，它们属于明天。

同时钉死 `learning` 本身的语义不变 —— `due_notification_status()`（#701）
把它读作「此刻到期」。
"""

from datetime import datetime

import pytest

import database
import database.cards
import database.core

# 中午 12:00 —— 日界点之后，所以 Anki 日就是 8-16。
_FIXED_NOW = datetime(2026, 8, 16, 12, 0, 0)


class _FakeDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return _FIXED_NOW


@pytest.fixture
def frozen_noon(tmp_path, monkeypatch):
    # database.core.DB_PATH，不是 database.DB_PATH —— 见 conftest.py（#615）。
    monkeypatch.setattr(database.core, "DB_PATH", str(tmp_path / "test.db"))
    database.init_db()
    monkeypatch.setattr(database.core, "_day_cutoff_hour", 4)
    monkeypatch.setattr(database.core, "datetime", _FakeDatetime)
    monkeypatch.setattr(database.cards, "datetime", _FakeDatetime)
    assert database.anki_today().isoformat() == "2026-08-16"


def _card(deck_id: int, word: str, due: str, state: str = "learning") -> int:
    word_id = database.insert_word({"word_zh": word, "definition": word})
    card_id = database.insert_card(word_id, "listening", deck_id, state=state, due=due)
    conn = database.get_db()
    conn.execute("UPDATE cards SET state = ?, due = ? WHERE id = ?", (state, due, card_id))
    conn.commit()
    conn.close()
    return card_id


@pytest.fixture
def deck_with_three_shapes(frozen_noon):
    """一个牌组，三张学习卡：已到期 / 十分钟后回来 / 明天。"""
    deck_id = database.get_or_create_deck("SoonDeck")
    _card(deck_id, "已到期", "2026-08-16T11:50:00")
    _card(deck_id, "十分钟后", "2026-08-16T12:10:00", state="relearn")
    _card(deck_id, "明天", "2026-08-17")
    return deck_id


def test_count_due_splits_now_soon_and_tomorrow(deck_with_three_shapes):
    c = database.count_due(deck_with_three_shapes, "listening")
    assert c["learning"] == 1, "learning 仍然只表示此刻到期"
    assert c["learning_soon"] == 1, "十分钟后回来的卡必须进 learning_soon"
    assert c["learning_future"] == 2, "learning_future 的语义不变：一切尚未到期的学习卡"


def test_cross_day_step_is_not_soon(frozen_noon):
    """1d/3d 步骤的裸日期 due 属于明天，绝不能算作「今天稍后回来」。"""
    deck_id = database.get_or_create_deck("TomorrowOnly")
    _card(deck_id, "明天", "2026-08-17")
    c = database.count_due(deck_id, "listening")
    assert c["learning"] == 0
    assert c["learning_soon"] == 0
    assert c["learning_future"] == 1


def test_bulk_and_multi_agree_with_count_due(deck_with_three_shapes):
    """角标（count_due_all_decks）、聚合牌组（count_due_multi）与单牌组必须一致。"""
    single = database.count_due(deck_with_three_shapes, "listening")

    all_counts, _ = database.count_due_all_decks()
    bulk = all_counts[(deck_with_three_shapes, "listening")]
    assert bulk["learning_soon"] == single["learning_soon"]
    assert bulk["learning"] == single["learning"]

    multi = database.count_due_multi([deck_with_three_shapes], "listening")
    assert multi["learning_soon"] == single["learning_soon"]
    assert multi["learning"] == single["learning"]


def test_due_notification_still_sees_only_cards_due_now(deck_with_three_shapes):
    """#701 把 counts['learning'] 读作「此刻到期」—— 这条语义不许被本次改动动到。"""
    status = database.due_notification_status()
    assert status["due_now"] == 1
    assert status["later_today"] == 1
    assert status["ready"] is False, "还有卡稍后回来时不该发提醒"


@pytest.fixture
def deck_with_two_soon_cards(frozen_noon):
    """一个牌组两张「稍后回来」的学习卡（3 分钟后 / 25 分钟后），外加一张已到期
    和一张明天的卡，用来验证 first/last 分钟数（#1182）。"""
    deck_id = database.get_or_create_deck("TwoSoonDeck")
    _card(deck_id, "已到期", "2026-08-16T11:50:00")
    _card(deck_id, "三分钟后", "2026-08-16T12:03:00", state="relearn")
    _card(deck_id, "二十五分钟后", "2026-08-16T12:25:00", state="learning")
    _card(deck_id, "明天", "2026-08-17")
    return deck_id


def test_first_and_last_minutes(deck_with_two_soon_cards):
    """「第一张几分钟后回来、最后一张几分钟后回来」的分钟数（#1182）。"""
    c = database.count_due(deck_with_two_soon_cards, "listening")
    assert c["learning_soon"] == 2
    assert c["learning_soon_first_min"] == 3
    assert c["learning_soon_last_min"] == 25


def test_bulk_and_multi_agree_on_minutes(deck_with_two_soon_cards):
    """count_due_multi / count_due_all_decks 的分钟数必须与 count_due 一致（#1182）。"""
    single = database.count_due(deck_with_two_soon_cards, "listening")

    all_counts, _ = database.count_due_all_decks()
    bulk = all_counts[(deck_with_two_soon_cards, "listening")]
    assert bulk["learning_soon_first_min"] == single["learning_soon_first_min"]
    assert bulk["learning_soon_last_min"] == single["learning_soon_last_min"]

    multi = database.count_due_multi([deck_with_two_soon_cards], "listening")
    assert multi["learning_soon_first_min"] == single["learning_soon_first_min"]
    assert multi["learning_soon_last_min"] == single["learning_soon_last_min"]


def test_no_soon_cards_means_none(frozen_noon):
    """没有稍后回来的卡时，两个分钟字段必须是 None，不是 0。"""
    deck_id = database.get_or_create_deck("NoSoonDeck")
    _card(deck_id, "已到期", "2026-08-16T11:50:00")
    _card(deck_id, "明天", "2026-08-17")
    c = database.count_due(deck_id, "listening")
    assert c["learning_soon"] == 0
    assert c["learning_soon_first_min"] is None
    assert c["learning_soon_last_min"] is None


def test_minutes_round_up(frozen_noon):
    """不足一分钟也要向上取整成 1 分钟，不能显示「0 分钟后」。"""
    deck_id = database.get_or_create_deck("RoundUpDeck")
    _card(deck_id, "半分钟后", "2026-08-16T12:00:30", state="relearn")
    c = database.count_due(deck_id, "listening")
    assert c["learning_soon_first_min"] == 1
    assert c["learning_soon_last_min"] == 1


def test_cross_day_step_does_not_block_the_reminder(frozen_noon):
    """1d/3d 步骤的卡到期在明天，不该算进 later_today —— 否则提醒几乎永远发不出去。

    原来的 `due > now AND due < tomorrow_cutoff` 是纯字符串比较，
    `'2026-08-17' < '2026-08-17T04:00:00'` 为真，裸日期的跨日步骤全被算进去了。
    """
    deck_id = database.get_or_create_deck("ReminderDeck")
    _card(deck_id, "已到期", "2026-08-16T11:50:00")
    _card(deck_id, "明天", "2026-08-17")

    status = database.due_notification_status()
    assert status["due_now"] == 1
    assert status["later_today"] == 0
    assert status["ready"] is True
