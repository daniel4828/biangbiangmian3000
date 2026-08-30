"""
Tests for podcast.py — Tingwu transcript parsing with paragraph timestamps (#543).

Fast tests, no credentials/network: feed _parse_tingwu_transcript synthetic
Tingwu-shaped JSON and assert the flattened text carries [MM:SS] prefixes.
"""

import podcast


def test_paragraph_level_start_becomes_timestamp():
    """Each paragraph is prefixed with its start time; past the hour the
    format grows to [H:MM:SS]."""
    result = {"Transcription": {"Paragraphs": [
        {"Text": "大家好欢迎收听", "Start": 0},
        {"Text": "今天聊一聊房价", "Start": 754000},   # 12:34
        {"Text": "最后总结", "Start": 3661000},        # 1:01:01
    ]}}
    assert podcast._parse_tingwu_transcript(result) == (
        "[00:00] 大家好欢迎收听 [12:34] 今天聊一聊房价 [1:01:01] 最后总结"
    )


def test_words_start_used_when_paragraph_has_no_text():
    """A paragraph given only as Words[] is joined, and its timestamp falls
    back to the first word's start."""
    result = {"Paragraphs": [
        {"Words": [{"Text": "再", "Start": 5000}, {"Text": "见", "Start": 5400}]},
    ]}
    assert podcast._parse_tingwu_transcript(result) == "[00:05] 再见"


def test_paragraph_without_timing_stays_plain():
    """No start field anywhere -> the paragraph is emitted without a prefix
    (never a broken '[..]')."""
    result = {"Paragraphs": [{"Text": "无时间戳的一段"}]}
    assert podcast._parse_tingwu_transcript(result) == "无时间戳的一段"


def test_unknown_shape_falls_back_to_recursive_text_collection():
    """An undocumented shape with no Paragraphs still degrades to the
    concatenated Text strings (the pre-#543 fallback, unchanged)."""
    result = {"Weird": {"Nested": [{"Text": "abc"}, {"Text": "def"}]}}
    assert podcast._parse_tingwu_transcript(result) == "abc def"


def test_fmt_timestamp_boundaries():
    assert podcast._fmt_timestamp(0) == "[00:00]"
    assert podcast._fmt_timestamp(59999) == "[00:59]"
    assert podcast._fmt_timestamp(60000) == "[01:00]"
    assert podcast._fmt_timestamp(3600000) == "[1:00:00]"


# ---------------------------------------------------------------------------
# Notification improvements (#631): subject line, Chinese summary
# ---------------------------------------------------------------------------

import ai
import database


EPISODE = {
    "id": 7,
    "video_id": "ep7",
    "channel_id": "https://feeds.example/show.xml",
    "title": "第 12 集：人工智能与就业",
    "youtube_url": "https://example/ep7",
    "spotify_url": "",
    # Since #708 the Chinese summary is a full translation of the German one
    # and carries the same <p>/<b> markup.
    "summary_zh": "<p><b>这集讨论人工智能对就业的影响。</b>嘉宾认为短期内影响有限。</p>",
    "summary_de": "<p><b>Es geht um KI.</b> Details folgen.</p>",
    "hsk_words": [{"word": "就业", "pinyin": "jiù yè", "definition_de": "Beschäftigung", "hsk": 5}],
    "transcript_de": [],
    "published_at": "2026-08-05T06:00:00+00:00",
}


def _send_email_capture(monkeypatch, episode, feed_title):
    """Run send_email against stubbed SMTP/database and return the sent MIME text."""
    for k, v in [("SMTP_HOST", "smtp.example"), ("SMTP_USERNAME", "u"),
                 ("SMTP_PASSWORD", "p"), ("SMTP_FROM", "from@example")]:
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(database, "get_podcast_config", lambda: {"email_to": "to@example"})
    monkeypatch.setattr(database, "get_feed_by_url",
                        lambda url: {"title": feed_title} if feed_title else None)

    sent = {}

    class FakeSMTP:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self): pass
        def login(self, *a): pass
        def sendmail(self, frm, to, msg): sent["msg"] = msg

    monkeypatch.setattr(podcast.smtplib, "SMTP", FakeSMTP)
    assert podcast.send_email(episode) is True
    return sent["msg"]


def _email_subject(raw: str) -> str:
    import email as email_mod
    from email.header import decode_header, make_header
    return str(make_header(decode_header(str(email_mod.message_from_string(raw)["Subject"]))))


def _email_body(raw: str) -> str:
    import email as email_mod
    msg = email_mod.message_from_string(raw)
    part = next(p for p in msg.walk() if p.get_content_type() == "text/html")
    return part.get_payload(decode=True).decode("utf-8", "replace")


def test_email_subject_is_podcast_name_dash_title(monkeypatch):
    raw = _send_email_capture(monkeypatch, EPISODE, "中文播客秀")
    assert _email_subject(raw) == "中文播客秀 - 第 12 集：人工智能与就业"


def test_email_subject_falls_back_to_title_without_dead_prefix(monkeypatch):
    """No feed name on record: the episode title stands alone. The old
    'Neue Podcast-Folge' prefix must not come back as a fallback."""
    raw = _send_email_capture(monkeypatch, EPISODE, None)
    subject = _email_subject(raw)
    assert subject == "第 12 集：人工智能与就业"
    assert "Neue Podcast" not in subject


def test_email_body_leads_with_chinese_summary(monkeypatch):
    """The Chinese summary must appear, and appear before the German one."""
    raw = _send_email_capture(monkeypatch, EPISODE, "中文播客秀")
    body = _email_body(raw)
    assert "这集讨论人工智能对就业的影响" in body
    assert body.index("这集讨论") < body.index("Es geht um KI")


def test_email_links_to_the_website_above_the_summary(monkeypatch):
    """#710: the website link has to be reachable before reading, not only in
    the footer — it's how Daniel gets to the page where he can add the words."""
    body = _email_body(_send_email_capture(monkeypatch, EPISODE, "中文播客秀"))
    link = "/#podcast-7"
    assert link in body
    assert body.index(link) < body.index("这集讨论")


def test_email_without_chinese_summary_renders_nothing_extra(monkeypatch):
    ep = dict(EPISODE, summary_zh="")
    body = _email_body(_send_email_capture(monkeypatch, ep, "中文播客秀"))
    assert "Es geht um KI" in body
    assert podcast._summary_zh_html("") == ""


def test_summary_zh_html_escapes_markup():
    """Only the structural tags of the summary contract are allowed through;
    a model that returns anything else must not get to inject it into the mail."""
    out = podcast._summary_zh_html("<script>alert(1)</script>")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_summary_zh_html_keeps_paragraph_and_bold_tags():
    """#708: the Chinese summary mirrors the German one's <p>/<b> structure,
    so those tags must survive into the mail."""
    out = podcast._summary_zh_html("<p><b>首句。</b>细节。</p><p><b>第二段。</b>更多。</p>")
    assert out.count("<p>") == 2
    assert "<b>首句。</b>" in out
    assert "&lt;p&gt;" not in out


def test_summary_zh_html_wraps_legacy_plain_text():
    """Episodes summarized before #708 hold plain text with blank lines
    between paragraphs — those must still become real <p> tags."""
    out = podcast._summary_zh_html("第一段。\n\n第二段。")
    assert out.count("<p ") == 2
    assert "第一段。" in out and "第二段。" in out


def test_feed_title_lookup(monkeypatch):
    monkeypatch.setattr(database, "get_feed_by_url", lambda url: {"title": "某播客"})
    assert podcast._feed_title(EPISODE) == "某播客"

    monkeypatch.setattr(database, "get_feed_by_url", lambda url: None)
    assert podcast._feed_title(EPISODE) is None

    monkeypatch.setattr(database, "get_feed_by_url", lambda url: {"title": ""})
    assert podcast._feed_title(EPISODE) is None

    assert podcast._feed_title({"channel_id": None}) is None


def test_signal_message_leads_with_chinese_summary(monkeypatch):
    monkeypatch.setenv("SIGNAL_ACCOUNT", "+490000")
    monkeypatch.setattr(database, "get_feed_by_url", lambda url: {"title": "中文播客秀"})

    captured = {}

    class Result:
        returncode = 0
        stderr = b""

    def fake_run(cmd, **kw):
        captured["text"] = cmd[cmd.index("-m") + 1]
        return Result()

    monkeypatch.setattr(podcast.subprocess, "run", fake_run)
    assert podcast.send_signal(EPISODE) is True

    text = captured["text"]
    assert "这集讨论人工智能对就业的影响" in text
    # Chinese intro comes before the German summary, and after the title line.
    assert text.index("第 12 集") < text.index("这集讨论") < text.index("Es geht um KI")
    # Signal is plain text: the summary's HTML tags must be stripped (#708).
    assert "<p>" not in text and "<b>" not in text


# --- prompt & parser --------------------------------------------------------

def test_prompt_asks_for_chinese_summary_and_a_german_only_german_one():
    prompt = ai.build_podcast_summary_prompt("转录文本", "标题", "detailed")
    assert "summary_zh" in prompt
    assert "HSK 4-5 level" in prompt
    # #979: the German summary carries no pinyin/汉字 asides any more — asking
    # for them again would refill the knowledge base with them.
    assert "GERMAN ONLY" in prompt
    assert "pinyin/汉字" not in prompt


def test_prompt_asks_for_chinese_summary_as_full_translation():
    """#708: the Chinese summary is the German one translated — same
    paragraphs, same <b> lead sentences — not a shorter teaser."""
    prompt = ai.build_podcast_summary_prompt("转录文本", "标题", "detailed")
    assert "Translate that German summary into Chinese" in prompt
    assert "Same number of" in prompt
    assert "<b>" in prompt


def test_parse_summary_json_reads_chinese_summary():
    raw = '{"summary_zh": "简短总结。", "summary_de": "<p>Text</p>", "words": []}'
    out = ai.parse_podcast_summary_json(raw)
    assert out["summary_zh"] == "简短总结。"
    assert out["summary_de"] == "<p>Text</p>"


def test_parse_summary_json_tolerates_missing_chinese_summary():
    """summary_zh is a bonus — a reply without it still yields a usable
    German summary rather than failing the whole episode."""
    out = ai.parse_podcast_summary_json('{"summary_de": "<p>Text</p>", "words": []}')
    assert out["summary_zh"] == ""
    assert out["summary_de"] == "<p>Text</p>"


def test_parse_summary_json_failure_still_has_chinese_key():
    """Callers read result['summary_zh'] unconditionally; the key must exist
    even on a totally unparseable reply."""
    out = ai.parse_podcast_summary_json("not json at all")
    assert out["summary_zh"] == ""
    assert out["summary_de"] == ""
    assert out["words"] == []


# ---------------------------------------------------------------------------
# Reel title suggestion (#781): AI-suggested titles may only overwrite a
# placeholder title (Instagram's "Video by <uploader>" / bare shortcode),
# never a real podcast/YouTube/article title.
# ---------------------------------------------------------------------------


def test_parse_summary_json_reads_title_suggestion():
    raw = ('{"summary_de": "<p>Text</p>", "words": [], '
           '"title_suggestion": "为什么利率会上升"}')
    out = ai.parse_podcast_summary_json(raw)
    assert out["title_suggestion"] == "为什么利率会上升"


def test_parse_summary_json_tolerates_missing_title_suggestion():
    """title_suggestion is a bonus like summary_zh — an older model reply
    without it must not fail the summary."""
    out = ai.parse_podcast_summary_json('{"summary_de": "<p>Text</p>", "words": []}')
    assert out["title_suggestion"] == ""


import pytest


@pytest.mark.parametrize("title,expected", [
    ("Video by thefreepress", True),
    ("video by thefreepress", True),
    ("Reel by some.account", True),
    ("Post by another_user", True),
    ("", True),
    ("   ", True),
    ("(untitled)", True),
    ("(Untitled)", True),
    ("DM4x_kLpQ2b", True),          # bare Instagram shortcode
    ("abcDEF123", True),           # shortcode-shaped, no spaces
    ("为什么利率会上升", False),
    ("第 12 集：人工智能与就业", False),
    ("How AI Changed My Job Search", False),
    ("Video killed the radio star", False),  # "Video" but not "Video by ..."
])
def test_is_placeholder_title(title, expected):
    assert podcast._is_placeholder_title(title) is expected


def _summary_result(title_suggestion="为什么利率会上升"):
    return {
        "summary_zh": "简短总结。",
        "summary_de": "<p><b>Text</b></p>",
        "words": [],
        "title_suggestion": title_suggestion,
    }


def test_regenerate_summary_replaces_placeholder_title(monkeypatch):
    """Reel stuck with 'Video by thefreepress' gets the AI's real title on
    regenerate_summary — the only path Daniel can trigger by hand to fix the
    existing backlog (#781)."""
    episode = {
        "id": 42,
        "title": "Video by thefreepress",
        "transcript_zh": "一些转录文本" * 20,
        "china_critical": False,
    }
    monkeypatch.setattr(database, "get_episode", lambda eid: episode)
    monkeypatch.setattr(database, "get_podcast_config", lambda: {"detail_level": "detailed"})
    monkeypatch.setattr(podcast, "summarize", lambda *a, **kw: _summary_result())
    monkeypatch.setattr(podcast, "filter_new_words", lambda words: words)
    monkeypatch.setattr(ai, "translate_title", lambda t: "Why Interest Rates Are Rising")
    # #937: the title suggestion now also asks whether Daniel edited the title
    # by hand. This test has no database at all, so answer for it.
    monkeypatch.setattr(database, "is_manual", lambda eid, field: False)

    captured = {}
    def fake_update_episode(eid, **fields):
        captured.update(fields)
    monkeypatch.setattr(database, "update_episode", fake_update_episode)

    result = podcast.regenerate_summary(42)
    assert result["regenerated"] is True
    assert captured["title"] == "为什么利率会上升"
    assert captured["title_en"] == "Why Interest Rates Are Rising"


def test_regenerate_summary_keeps_real_title(monkeypatch):
    """A real podcast title must never be overwritten by the AI's guess,
    even if a title_suggestion comes back (#781's whole point)."""
    episode = {
        "id": 7,
        "title": "第 12 集：人工智能与就业",
        "transcript_zh": "一些转录文本" * 20,
        "china_critical": False,
    }
    monkeypatch.setattr(database, "get_episode", lambda eid: episode)
    monkeypatch.setattr(database, "get_podcast_config", lambda: {"detail_level": "detailed"})
    monkeypatch.setattr(podcast, "summarize", lambda *a, **kw: _summary_result())
    monkeypatch.setattr(podcast, "filter_new_words", lambda words: words)
    monkeypatch.setattr(ai, "translate_title", lambda t: "should not be called")

    captured = {}
    def fake_update_episode(eid, **fields):
        captured.update(fields)
    monkeypatch.setattr(database, "update_episode", fake_update_episode)

    result = podcast.regenerate_summary(7)
    assert result["regenerated"] is True
    assert "title" not in captured
    assert "title_en" not in captured
