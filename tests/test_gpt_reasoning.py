"""gpt-5 系列的 reasoning_effort 选择（议题 #724）。

_call_api 的 thinking 参数原来只有 DeepSeek 和 GLM 分支尊重，gpt 分支硬编码
reasoning_effort="low"。实测生成故事句子时 low 要烧 403 个推理 token，比关掉
贵 6.2 倍、慢一倍，输出质量却看不出差别。

难点在于两代模型的最低档名字互不兼容（2026-08-12 实测）：
  gpt-5 / gpt-5-mini   支持 minimal, low, medium, high    —— 发 'none' 报 400
  gpt-5.1 / gpt-5.6-*  支持 none, low, medium, high, xhigh —— 5.6 发 'minimal' 报 400

gpt-5-mini 是 briefing（新闻简报）的实际默认模型，发错值整条新闻流程会 400。
"""
import pytest

import ai


@pytest.mark.parametrize("model,expected", [
    # gpt-5 那一代：最低是 minimal，不认 none
    ("gpt-5", "minimal"),
    ("gpt-5-mini", "minimal"),
    # gpt-5.1 起支持 none
    ("gpt-5.1", "none"),
    ("gpt-5.6-luna", "none"),
    ("gpt-5.6-terra", "none"),
    ("gpt-5.6-sol", "none"),
    ("gpt-6-luna", "none"),
    ("gpt-6-sol", "none"),
    ("gpt-6-astra", "low"),
])
def test_min_effort_per_model(model, expected):
    assert ai._gpt_reasoning_effort(model, thinking=False) == expected


@pytest.mark.parametrize("model", [
    "gpt-5", "gpt-5-mini", "gpt-5.1", "gpt-5.6-luna", "gpt-6-not-released-yet",
])
def test_thinking_true_uses_low(model):
    """thinking=True 一律 low —— 各代都接受这个值。"""
    assert ai._gpt_reasoning_effort(model, thinking=True) == "low"


def test_unknown_model_falls_back_to_safe_value():
    """表里没有的新模型必须走 low：任何一代都接受，绝不会 400。

    猜错档位名的代价是整条流程挂掉，多花点钱的代价只是钱。
    """
    assert ai._gpt_reasoning_effort("gpt-7-brandnew", thinking=False) == "low"
    assert ai._gpt_reasoning_effort("gpt-5.9-whatever", thinking=False) == "low"


def test_dated_snapshot_suffix_is_stripped():
    """OpenAI 会返回带日期的 id，不能因此掉进未知回落。"""
    assert ai._gpt_reasoning_effort("gpt-5.1-2026-04-14", thinking=False) == "none"
    assert ai._gpt_reasoning_effort("gpt-5-mini-20260414", thinking=False) == "minimal"


def test_call_api_sends_the_resolved_effort(monkeypatch):
    """端到端：_call_api 真的把查出来的值发给了 OpenAI。"""
    sent = {}

    class _FakeUsage:
        prompt_tokens = 10
        completion_tokens = 5
        prompt_tokens_details = None

    class _FakeMessage:
        content = "好"
        reasoning_content = None

    class _FakeChoice:
        message = _FakeMessage()
        finish_reason = "stop"

    class _FakeResp:
        choices = [_FakeChoice()]
        usage = _FakeUsage()

    class _FakeCompletions:
        def create(self, **kwargs):
            sent.update(kwargs)
            return _FakeResp()

    class _FakeClient:
        chat = type("C", (), {"completions": _FakeCompletions()})()

    monkeypatch.setattr(ai, "_openai_client", lambda model: _FakeClient())
    monkeypatch.setattr(ai.database, "log_api_call", lambda **kw: None)

    msgs = [{"role": "user", "content": "hi"}]

    ai._call_api("gpt-5.6-luna", msgs, 100, purpose="test")
    assert sent["reasoning_effort"] == "none"
    # gpt-5 系列必须用 max_completion_tokens，不是 max_tokens
    assert sent["max_completion_tokens"] == 100
    assert "max_tokens" not in sent

    ai._call_api("gpt-5-mini", msgs, 100, purpose="test")
    assert sent["reasoning_effort"] == "minimal", "gpt-5-mini 收到 none 会 400"

    ai._call_api("gpt-5.6-luna", msgs, 100, purpose="test", thinking=True)
    assert sent["reasoning_effort"] == "low"


def test_non_gpt_models_unaffected(monkeypatch):
    """DeepSeek 分支不该出现 reasoning_effort。"""
    sent = {}

    class _FakeUsage:
        prompt_tokens = 10
        completion_tokens = 5
        prompt_cache_hit_tokens = 0
        prompt_tokens_details = None

    class _FakeMessage:
        content = "好"
        reasoning_content = None

    class _FakeChoice:
        message = _FakeMessage()
        finish_reason = "stop"

    class _FakeResp:
        choices = [_FakeChoice()]
        usage = _FakeUsage()

    class _FakeCompletions:
        def create(self, **kwargs):
            sent.clear()
            sent.update(kwargs)
            return _FakeResp()

    class _FakeClient:
        chat = type("C", (), {"completions": _FakeCompletions()})()

    monkeypatch.setattr(ai, "_openai_client", lambda model: _FakeClient())
    monkeypatch.setattr(ai.database, "log_api_call", lambda **kw: None)

    ai._call_api("deepseek-v4-flash", [{"role": "user", "content": "hi"}], 100, purpose="test")
    assert "reasoning_effort" not in sent
    assert sent["extra_body"] == {"thinking": {"type": "disabled"}}
