"""Tests for conservative proper-noun correction in transcripts (#1215)."""

import ai


def test_correct_transcript_applies_only_exact_model_replacements(monkeypatch):
    transcript = "今天聊 Opic 的模型。有人把 Opic 和 OpenAI 比较。"

    def fake_call(model, messages, max_tokens, purpose, thinking=False):
        prompt = messages[0]["content"]
        if "声动早咖啡" not in prompt or "AI 公司" not in prompt:
            return "[]"
        return '[{"original":"Opic","corrected":"Anthropic"}]'

    monkeypatch.setattr(ai, "_call_api", fake_call)

    corrected = ai.correct_transcript_proper_nouns(
        transcript, title="AI 公司", source_name="声动早咖啡"
    )

    assert corrected == "今天聊 Anthropic 的模型。有人把 Anthropic 和 OpenAI 比较。"


def test_correct_transcript_ignores_invalid_or_nonmatching_replacements(monkeypatch):
    transcript = "普通句子保持不变，提到了 OpenAI。"
    monkeypatch.setattr(
        ai,
        "_call_api",
        lambda *args, **kwargs: """[
            {"original":"不存在","corrected":"Anthropic"},
            {"original":"OpenAI","corrected":""},
            {"original":"普通句子","corrected":"普通句子"},
            {"original":12,"corrected":"twelve"}
        ]""",
    )

    assert ai.correct_transcript_proper_nouns(transcript, title="标题") == transcript


def test_correct_transcript_keeps_original_on_bad_json(monkeypatch):
    transcript = "这里误写了 Opic。"
    monkeypatch.setattr(ai, "_call_api", lambda *args, **kwargs: "not json")

    assert ai.correct_transcript_proper_nouns(transcript, title="标题") == transcript


def test_correct_transcript_keeps_original_when_api_fails(monkeypatch):
    transcript = "这里误写了 Opic。"

    def fail(*args, **kwargs):
        raise RuntimeError("temporary failure")

    monkeypatch.setattr(ai, "_call_api", fail)

    assert ai.correct_transcript_proper_nouns(transcript, title="标题") == transcript
