"""Tests for scripts/import_srt_track.py (issue #1228): SRT parsing and the
source_text-with-offsets builder, which is where this script can silently
produce a broken read-along track (drifted char_start/char_end -> the
frontend highlights the wrong span, or misses inserting the empty-paragraph
break before a chapter heading).

Only the two pure functions are under test here — no DB, no subprocess, no
real book text (invented short Chinese sentences instead).
"""
import importlib.util
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

_SCRIPT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "scripts", "import_srt_track.py")
_spec = importlib.util.spec_from_file_location("import_srt_track", _SCRIPT_PATH)
import_srt_track = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(import_srt_track)

parse_srt = import_srt_track.parse_srt
build_source_text = import_srt_track.build_source_text


# ── parse_srt ────────────────────────────────────────────────────────────

def test_parse_srt_basic():
    srt = (
        "1\n"
        "00:00:01,000 --> 00:00:04,500\n"
        "你好世界\n"
        "\n"
        "2\n"
        "00:00:05,000 --> 00:00:07,250\n"
        "这是第二句\n"
    )
    cues = parse_srt(srt)
    assert cues == [
        {"start_ms": 1000, "end_ms": 4500, "text": "你好世界"},
        {"start_ms": 5000, "end_ms": 7250, "text": "这是第二句"},
    ]


def test_parse_srt_bom_and_crlf():
    srt = (
        "﻿1\r\n"
        "00:00:00,000 --> 00:00:02,000\r\n"
        "带BOM的字幕\r\n"
        "\r\n"
        "2\r\n"
        "00:00:02,000 --> 00:00:03,000\r\n"
        "第二条\r\n"
    )
    cues = parse_srt(srt)
    assert len(cues) == 2
    assert cues[0]["text"] == "带BOM的字幕"
    assert cues[0]["start_ms"] == 0
    assert cues[1]["text"] == "第二条"


def test_parse_srt_multiline_cue_joined_with_space():
    srt = (
        "1\n"
        "00:00:01,000 --> 00:00:03,000\n"
        "第一行\n"
        "第二行\n"
    )
    cues = parse_srt(srt)
    assert cues == [{"start_ms": 1000, "end_ms": 3000, "text": "第一行 第二行"}]


def test_parse_srt_dot_timestamp_variant():
    srt = "1\n00:00:01.000 --> 00:00:02.000\n句子\n"
    cues = parse_srt(srt)
    assert cues == [{"start_ms": 1000, "end_ms": 2000, "text": "句子"}]


def test_parse_srt_no_index_line():
    # Some exporters omit the numeric index line entirely.
    srt = "00:00:01,000 --> 00:00:02,000\n没有序号行\n"
    cues = parse_srt(srt)
    assert cues == [{"start_ms": 1000, "end_ms": 2000, "text": "没有序号行"}]


def test_parse_srt_skips_empty_text_blocks():
    srt = (
        "1\n"
        "00:00:01,000 --> 00:00:02,000\n"
        "\n"
        "2\n"
        "00:00:03,000 --> 00:00:04,000\n"
        "有内容\n"
    )
    cues = parse_srt(srt)
    assert cues == [{"start_ms": 3000, "end_ms": 4000, "text": "有内容"}]


def test_parse_srt_sorted_by_start_ms():
    srt = (
        "1\n00:00:05,000 --> 00:00:06,000\n后出现的\n\n"
        "2\n00:00:01,000 --> 00:00:02,000\n先出现的\n"
    )
    cues = parse_srt(srt)
    assert [c["text"] for c in cues] == ["先出现的", "后出现的"]


# ── build_source_text ────────────────────────────────────────────────────

def _assert_offsets_hold(source_text, cues):
    for c in cues:
        assert source_text[c["char_start"]:c["char_end"]] == c["text"]


def test_build_source_text_offset_invariant_plain():
    cues = [
        {"start_ms": 0, "end_ms": 1000, "text": "你好"},
        {"start_ms": 1000, "end_ms": 2000, "text": "世界"},
        {"start_ms": 2000, "end_ms": 3000, "text": "再见"},
    ]
    source_text, out = build_source_text(cues)
    _assert_offsets_hold(source_text, out)
    assert source_text == "你好 世界 再见"


def test_build_source_text_first_cue_has_no_leading_separator():
    cues = [{"start_ms": 0, "end_ms": 1000, "text": "开头"}]
    source_text, out = build_source_text(cues)
    assert source_text == "开头"
    assert out[0]["char_start"] == 0
    assert out[0]["char_end"] == 2


def test_build_source_text_chapter_heading_gets_own_paragraph():
    cues = [
        {"start_ms": 0, "end_ms": 1000, "text": "序言"},
        {"start_ms": 1000, "end_ms": 2000, "text": "第一章"},
        {"start_ms": 2000, "end_ms": 3000, "text": "很久以前"},
        {"start_ms": 3000, "end_ms": 4000, "text": "有一个王子"},
        {"start_ms": 4000, "end_ms": 5000, "text": "第十二章"},
        {"start_ms": 5000, "end_ms": 6000, "text": "结尾"},
    ]
    source_text, out = build_source_text(cues)
    _assert_offsets_hold(source_text, out)
    assert source_text == (
        "序言\n\n第一章\n\n很久以前 有一个王子\n\n第十二章\n\n结尾"
    )


def test_build_source_text_heading_as_first_cue_has_no_leading_break():
    cues = [
        {"start_ms": 0, "end_ms": 1000, "text": "第一章"},
        {"start_ms": 1000, "end_ms": 2000, "text": "开篇"},
    ]
    source_text, out = build_source_text(cues)
    _assert_offsets_hold(source_text, out)
    assert source_text == "第一章\n\n开篇"
    assert out[0]["char_start"] == 0


def test_build_source_text_cue_dict_keeps_original_fields():
    cues = [{"start_ms": 0, "end_ms": 1000, "text": "词"}]
    _, out = build_source_text(cues)
    assert out[0]["start_ms"] == 0
    assert out[0]["end_ms"] == 1000
    assert out[0]["text"] == "词"


def test_build_source_text_empty_list():
    source_text, out = build_source_text([])
    assert source_text == ""
    assert out == []


def test_build_source_text_offset_invariant_roundtrip_from_parsed_srt():
    srt = (
        "1\n00:00:00,000 --> 00:00:02,000\n小王子\n\n"
        "2\n00:00:02,000 --> 00:00:04,000\n第一章\n\n"
        "3\n00:00:04,000 --> 00:00:06,000\n我六岁的时候\n\n"
        "4\n00:00:06,000 --> 00:00:08,000\n看到了一幅精彩的插画\n"
    )
    cues = parse_srt(srt)
    source_text, out = build_source_text(cues)
    _assert_offsets_hold(source_text, out)
