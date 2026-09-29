"""下拉框里的模型必须都在后端白名单里（议题 #721）。

#721 的 bug：index.html 加了 gpt-5.6-luna/terra/sol 三个选项，但
routes/story.py 的 ALLOWED_MODELS 忘了同步。_validated_model() 对白名单外
的值静默回落到默认模型 —— 界面照常显示生成成功，用的却是 DeepSeek。
选择被静默丢弃，所以三个月没人发现。

这条测试直接从 HTML 里解析两个模型下拉框的 option value，与白名单对比，
让下次加模型时忘掉任何一边都会立刻变红。
"""
import re
from pathlib import Path

import pytest

from routes.story import ALLOWED_MODELS

INDEX_HTML = Path(__file__).resolve().parent.parent / "static" / "index.html"
MODEL_SELECT_IDS = ["setup-model", "story-error-model"]


def _select_options(html: str, select_id: str) -> list[str]:
    """解析 <select id="..."> ... </select> 里所有 option 的 value。"""
    m = re.search(rf'<select[^>]*id="{select_id}"[^>]*>(.*?)</select>', html, re.S)
    assert m, f"index.html 里找不到 <select id={select_id}>"
    return re.findall(r'<option[^>]*value="([^"]+)"', m.group(1))


@pytest.mark.parametrize("select_id", MODEL_SELECT_IDS)
def test_dropdown_models_are_whitelisted(select_id):
    options = _select_options(INDEX_HTML.read_text(encoding="utf-8"), select_id)
    assert options, f"{select_id} 一个 option 都没解析出来 —— 选择器多半失效了"

    missing = [o for o in options if o not in ALLOWED_MODELS]
    assert not missing, (
        f"#{select_id} 里的这些模型不在 routes.story.ALLOWED_MODELS 里，"
        f"选中后会被静默换成默认模型：{missing}"
    )


@pytest.mark.parametrize("select_id", MODEL_SELECT_IDS)
def test_dropdown_models_have_pricing(select_id):
    """选得到却算不出钱的模型，会在成本页显示为未知 —— 同样是静默的。"""
    from database.stats import _lookup_pricing

    options = _select_options(INDEX_HTML.read_text(encoding="utf-8"), select_id)
    unpriced = [o for o in options if _lookup_pricing(o) is None]
    assert not unpriced, f"{select_id} 里的这些模型没有价格表条目：{unpriced}"


def test_gpt_56_models_whitelisted():
    """#721 的三个具体模型 —— 回归锚点。"""
    for model in ("gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"):
        assert model in ALLOWED_MODELS


def test_gpt_6_models_whitelisted_and_priced():
    from database.stats import _lookup_pricing
    for model in ("gpt-6-luna", "gpt-6-sol", "gpt-6-astra"):
        assert model in ALLOWED_MODELS
        assert _lookup_pricing(model) is not None


def test_gpt_6_luna_is_backend_and_story_default():
    import ai
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert ai.DEFAULT_MODEL == "gpt-6-luna"
    assert re.search(r'<option value="gpt-6-luna" selected>', html)


def test_validated_model_falls_back_and_warns(caplog):
    from routes.story import _validated_model

    assert _validated_model("gpt-5.6-luna") == "gpt-5.6-luna"

    with caplog.at_level("WARNING"):
        result = _validated_model("no-such-model-9000")
    assert result != "no-such-model-9000"
    assert "no-such-model-9000" in caplog.text, "静默回落是 #721 的根因，必须留日志"
