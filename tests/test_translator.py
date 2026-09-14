"""translator.translate_batch 的分块与进度上报（#756/#758）。

回归背景：原来整批拼成一个字符串发出，长故事超过 Google 的 5000 字上限就
报错，回退成"每句一次请求"——228 句串行跑几分钟，而进度只在首尾更新，
界面永远显示 Translating… 0/228，看着像卡死。
"""
import translator


class FakeTranslator:
    """记录每次请求的输入；翻译 = 每行加前缀。"""

    def __init__(self):
        self.calls: list[str] = []

    def translate(self, text: str) -> str:
        self.calls.append(text)
        if len(text) > 5000:
            raise ValueError("Text length need to be between 0 and 5000 characters")
        return "\n".join("de:" + line for line in text.split("\n"))


def _patch(monkeypatch, fake):
    monkeypatch.setattr(translator, "_load", lambda source, target: fake)


def test_long_batch_is_split_into_several_requests(monkeypatch):
    fake = FakeTranslator()
    _patch(monkeypatch, fake)
    texts = [f"第{i}句话，内容足够长以便凑够字符数。" for i in range(228)]

    out = translator.translate_batch(texts, target="de")

    assert out == ["de:" + t for t in texts]
    assert 1 < len(fake.calls) < len(texts), "应分成几块，而不是一次或每句一次"
    assert all(len(c) <= 5000 for c in fake.calls)


def test_short_batch_stays_one_request(monkeypatch):
    fake = FakeTranslator()
    _patch(monkeypatch, fake)

    out = translator.translate_batch(["你好", "再见"], target="de")

    assert out == ["de:你好", "de:再见"]
    assert len(fake.calls) == 1


def test_progress_is_reported_per_chunk(monkeypatch):
    """进度必须在中途上报，不能从 0/N 直接跳到 N/N——那正是 #756 的症状。"""
    fake = FakeTranslator()
    _patch(monkeypatch, fake)
    monkeypatch.setattr(translator, "_CHUNK_CHAR_BUDGET", 10)
    seen: list[tuple[int, int]] = []

    texts = ["句一", "句二", "句三", "句四"]
    translator.translate_batch(texts, target="de",
                               on_progress=lambda d, t: seen.append((d, t)))

    assert seen, "必须至少上报一次"
    assert all(t == len(texts) for _, t in seen)
    assert [d for d, _ in seen] == sorted(d for d, _ in seen), "done 必须单调不减"
    assert seen[-1][0] == len(texts)
    assert len(seen) > 1, "中途要有进度，不能只报最后一次"


def test_failing_chunk_falls_back_only_for_itself(monkeypatch):
    """一块失败时只有该块逐句重试，其它块的结果照常保留。"""
    fake = FakeTranslator()

    def flaky(text: str) -> str:
        if "坏句" in text and "\n" in text:
            raise RuntimeError("boom")
        return "\n".join("de:" + line for line in text.split("\n"))

    fake.translate = flaky  # type: ignore[method-assign]
    _patch(monkeypatch, fake)
    monkeypatch.setattr(translator, "_CHUNK_CHAR_BUDGET", 10)

    out = translator.translate_batch(["好句一", "好句二", "坏句", "坏句尾"], target="de")

    assert out == ["de:好句一", "de:好句二", "de:坏句", "de:坏句尾"]


def test_over_long_single_text_still_returned(monkeypatch):
    """单句就超限时不能被丢掉——退回原文即可。"""
    fake = FakeTranslator()
    _patch(monkeypatch, fake)
    huge = "字" * 6000

    out = translator.translate_batch([huge, "短句"], target="de")

    assert len(out) == 2
    assert out[0] == huge  # 翻译失败 → 原样返回
    assert out[1] == "de:短句"


# ── HTTP transport (#890) ────────────────────────────────────────────────────
# Regression: deep-translator sent requests' default User-Agent, Google
# answered with a JS-only page carrying no div.result-container, and every
# translation in the app raised TranslationNotFound. translate_zh's
# "return the original on failure" contract hid it everywhere except the book
# reader, which 502s. These tests pin the parsing and the two error contracts;
# the User-Agent itself is asserted on because it is the whole fix.
import io

import pytest


_PAGE = ('<!DOCTYPE html><html><body><div class="header">x</div>'
         '<div class="result-container">Bonjour le monde.\nMerci.</div>'
         '<div class="footer">y</div></body></html>')
_JS_ONLY_PAGE = '<!DOCTYPE html><html><body><div class="header">x</div></body></html>'


def _fake_urlopen(body: str, seen: dict):
    def _open(req, timeout=None):
        seen["url"] = req.full_url
        seen["ua"] = req.get_header("User-agent")
        seen["timeout"] = timeout
        return io.BytesIO(body.encode("utf-8"))
    return _open


def _fresh(monkeypatch, body):
    """A translator with an empty cache and a stubbed HTTP layer."""
    seen: dict = {}
    monkeypatch.setattr(translator, "_translators", {})
    monkeypatch.setattr(translator.urllib.request, "urlopen", _fake_urlopen(body, seen))
    return seen


def test_translate_parses_result_container_and_keeps_line_count(monkeypatch):
    seen = _fresh(monkeypatch, _PAGE)

    out = translator.translate_strict("Hallo Welt.\nDanke.", target="fr", source="de")

    assert out == "Bonjour le monde.\nMerci."
    assert "sl=de" in seen["url"] and "tl=fr" in seen["url"]


def test_request_sends_a_browser_user_agent(monkeypatch):
    seen = _fresh(monkeypatch, _PAGE)

    translator.translate_strict("Hallo", target="fr", source="de")

    # python-requests/urllib defaults get a JS-only page back — see module docstring.
    assert "Mozilla/" in seen["ua"]
    assert "python" not in seen["ua"].lower()
    assert seen["timeout"] == translator._REQUEST_TIMEOUT_SECONDS


def test_missing_result_container_raises_for_strict(monkeypatch):
    _fresh(monkeypatch, _JS_ONLY_PAGE)

    with pytest.raises(Exception):
        translator.translate_strict("Hallo", target="fr", source="de")


def test_missing_result_container_returns_original_for_translate_zh(monkeypatch):
    _fresh(monkeypatch, _JS_ONLY_PAGE)

    # Deliberately lossy contract (see translate_zh's docstring): a missing
    # gloss must not sink a whole story.
    assert translator.translate_zh("Hallo", target="fr", source="de") == "Hallo"


# ── translate_strict 的瞬时失败重试（#895）──────────────────────────────────
# 背景：#890 那次全线失效暴露出的结构性弱点——严格版一次异常就直接抛，
# knowledge/rendition.py 于是整篇阅读版只剩一行 translation failed。


@pytest.fixture(autouse=True)
def _no_retry_delay(monkeypatch):
    """重试的退避对测试只是纯等待。"""
    monkeypatch.setattr(translator, "_STRICT_RETRY_DELAY_SECONDS", 0)


def _fake_urlopen_sequence(bodies: list, calls: list):
    """按顺序返回若干个响应体；元素是异常实例时改为抛出它。"""
    def _open(req, timeout=None):
        calls.append(req.full_url)
        body = bodies[min(len(calls) - 1, len(bodies) - 1)]
        if isinstance(body, Exception):
            raise body
        return io.BytesIO(body.encode("utf-8"))
    return _open


def _fresh_sequence(monkeypatch, bodies):
    calls: list = []
    monkeypatch.setattr(translator, "_translators", {})
    monkeypatch.setattr(translator.urllib.request, "urlopen",
                        _fake_urlopen_sequence(bodies, calls))
    return calls


def test_strict_retries_a_transient_failure(monkeypatch):
    calls = _fresh_sequence(monkeypatch, [OSError("connection reset"), _PAGE])

    out = translator.translate_strict("Hallo Welt.\nDanke.", target="fr", source="de")

    assert out == "Bonjour le monde.\nMerci."
    assert len(calls) > 1, "一次网络抖动必须重试，不能直接抛给上层"


def test_strict_retries_an_empty_result(monkeypatch):
    # 空结果原来直接返回空串，由上层判成 empty segment —— 白白丢掉一次可重试的机会。
    empty = ('<html><body><div class="result-container">   </div></body></html>')
    calls = _fresh_sequence(monkeypatch, [empty, _PAGE])

    assert translator.translate_strict("Hallo", target="fr", source="de").strip()
    assert len(calls) > 1


def test_strict_gives_up_after_the_last_attempt(monkeypatch):
    calls = _fresh_sequence(monkeypatch, [OSError("connection reset")])

    with pytest.raises(Exception) as exc:
        translator.translate_strict("Hallo", target="fr", source="de")

    assert "connection reset" in str(exc.value), "要保留最后一次失败的原因"
    # #1140 起每次尝试会依次走完几条通道，所以不再是"尝试次数 = 请求数"，
    # 但仍然必须有上限——不能没完没了地重试。
    assert translator._STRICT_ATTEMPTS <= len(calls) <= \
        translator._STRICT_ATTEMPTS * (len(translator._TRANSPORTS) + 1)


def test_translate_zh_still_fails_fast(monkeypatch):
    # 吞异常返回原文的契约不变，也不该为它重试三次（调用点极多）。
    # 几条通道各试一次是可以的（#1140），但绝不进 _STRICT_ATTEMPTS 那个循环。
    calls = _fresh_sequence(monkeypatch, [OSError("connection reset")])

    assert translator.translate_zh("Hallo", target="fr", source="de") == "Hallo"
    assert len(calls) <= len(translator._TRANSPORTS) + 1


# ── 失败块的逐句重试必须有上限（#1140）──────────────────────────────────────
# 背景：一块失败往往是因为免费端点在限流我们（生词标注曾对一整篇转录逐词
# 请求，几百个请求打下去必被限）。这时再逐句重试等于朝着同一个拒绝多发几十
# 次请求，限流更久、还要等好几分钟。

def test_item_fallback_stops_after_repeated_failures(monkeypatch):
    fake = FakeTranslator()

    def always_fail(text: str) -> str:
        raise RuntimeError("429 rate limited")

    fake.translate = always_fail  # type: ignore[method-assign]
    monkeypatch.setattr(translator, "_load", lambda source, target: fake)
    calls: list[str] = []
    monkeypatch.setattr(translator, "translate_zh",
                        lambda text, target, source: (calls.append(text), text)[1])

    texts = [f"第{i}句" for i in range(40)]
    out = translator.translate_batch(texts, target="de")

    assert out == texts, "失败契约不变：原样返回"
    assert len(calls) <= translator._ITEM_FALLBACK_MAX_MISSES, \
        f"逐句重试必须刹车，实际发了 {len(calls)} 次"


def test_item_fallback_still_rescues_one_bad_sentence(monkeypatch):
    """整块失败只是因为其中一句超长时，其余句子照样要救回来。"""
    fake = FakeTranslator()
    _patch(monkeypatch, fake)
    huge = "字" * 6000
    texts = [huge] + [f"第{i}句" for i in range(10)]

    out = translator.translate_batch(texts, target="de")

    assert out[0] == huge          # 这一句救不了
    assert out[1:] == ["de:" + t for t in texts[1:]]   # 其余全部翻出来了


# ── 多通道降级（#1140）────────────────────────────────────────────────────────
# 背景：谷歌开始拦机房 IP（/m 返回 "Sorry..." 拦截页），于是全应用的翻译
# 同时静音——行内词释义、译 按钮、知识库 rendition、书籍页面。只有一条通道
# 的模块必然会有这一天，#890 那次（User-Agent）已经演过一遍。

_SORRY_PAGE = ('<html><head><title>Sorry...</title></head><body>'
               '<div>Our systems have detected unusual traffic</div></body></html>')
_GTX_JSON = '[[["Bonjour le monde.","Hallo Welt.",null,null,1]],null,"de"]'
_MS_JSON = '[{"translations":[{"text":"Bonjour le monde.","to":"fr"}]}]'


def _route(monkeypatch, handlers: dict):
    """按 URL 子串分派的 urlopen 桩；值是响应体字符串或要抛的异常。
    记录每次请求的 (url, body, headers)。"""
    seen: list = []

    def _open(req, timeout=None):
        url = req.full_url
        seen.append({"url": url,
                     "body": (req.data or b"").decode("utf-8"),
                     "auth": req.get_header("Authorization")})
        for needle, answer in handlers.items():
            if needle in url:
                if isinstance(answer, Exception):
                    raise answer
                return io.BytesIO(answer.encode("utf-8"))
        raise AssertionError(f"没有为这个地址准备响应：{url}")

    monkeypatch.setattr(translator, "_translators", {})
    monkeypatch.setattr(translator, "_ms_token", {"value": "", "issued_at": 0.0})
    monkeypatch.setattr(translator.urllib.request, "urlopen", _open)
    return seen


def test_blocked_mobile_page_falls_through_to_the_json_endpoint(monkeypatch):
    seen = _route(monkeypatch, {"translate.google.com/m": _SORRY_PAGE,
                                "translate_a/single": _GTX_JSON})

    out = translator.translate_strict("Hallo Welt.", target="fr", source="de")

    assert out == "Bonjour le monde."
    assert any("translate_a/single" in c["url"] for c in seen)


def test_both_google_doors_blocked_falls_through_to_microsoft(monkeypatch):
    seen = _route(monkeypatch, {"translate.google.com/m": _SORRY_PAGE,
                                "translate_a/single": _SORRY_PAGE,
                                "edge.microsoft.com": "header.payload.signature",
                                "cognitive": _MS_JSON})

    out = translator.translate_strict("Hallo Welt.", target="fr", source="de")

    assert out == "Bonjour le monde."
    ms = [c for c in seen if "cognitive" in c["url"]][0]
    assert ms["auth"] == "Bearer header.payload.signature"
    assert "zh" not in ms["url"] and "from=de" in ms["url"] and "to=fr" in ms["url"]


def test_microsoft_sends_one_element_per_line_and_keeps_the_line_count(monkeypatch):
    """批量翻译靠换行切分结果。微软这条路把行数交给协议本身保证，不靠端点
    恰好保留换行。"""
    three = '[{"translations":[{"text":"un","to":"fr"}]},' \
            '{"translations":[{"text":"deux","to":"fr"}]},' \
            '{"translations":[{"text":"trois","to":"fr"}]}]'
    seen = _route(monkeypatch, {"translate.google.com/m": OSError("blocked"),
                                "translate_a/single": OSError("blocked"),
                                "edge.microsoft.com": "a.b.c",
                                "cognitive": three})

    out = translator.translate_strict("eins\nzwei\ndrei", target="fr", source="de")

    assert out == "un\ndeux\ntrois"
    body = [c for c in seen if "cognitive" in c["url"]][0]["body"]
    assert body.count('"Text"') == 3


def test_chinese_is_sent_to_microsoft_as_zh_Hans(monkeypatch):
    seen = _route(monkeypatch, {"translate.google.com/m": _SORRY_PAGE,
                                "translate_a/single": _SORRY_PAGE,
                                "edge.microsoft.com": "a.b.c",
                                "cognitive": '[{"translations":[{"text":"Hallo","to":"de"}]}]'})

    translator.translate_strict("你好", target="de", source="zh-CN")

    assert "from=zh-Hans" in [c for c in seen if "cognitive" in c["url"]][0]["url"]


def test_error_names_every_transport_that_failed(monkeypatch):
    """报错就是诊断：到底是被拦了、还是格式变了，只能从这句话里看出来。"""
    _route(monkeypatch, {"translate.google.com/m": _SORRY_PAGE,
                         "translate_a/single": _SORRY_PAGE,
                         "edge.microsoft.com": OSError("connection reset"),
                         "cognitive": _SORRY_PAGE})

    with pytest.raises(Exception) as exc:
        translator.translate_strict("Hallo", target="fr", source="de")

    message = str(exc.value)
    assert "google-mobile" in message and "google-json" in message
    assert "Sorry..." in message, "拦截页的标题就是全部诊断，必须带出来"


def test_the_working_transport_is_remembered(monkeypatch):
    """谷歌在拦这台机器时，每句话之前都白付两次失败请求会让阅读页面变成分钟级。"""
    seen = _route(monkeypatch, {"translate.google.com/m": _SORRY_PAGE,
                                "translate_a/single": _GTX_JSON})

    t = translator._load("de", "fr")
    t.translate("Hallo Welt.")
    first_round = len(seen)
    t.translate("Hallo Welt.")

    assert first_round == 2
    assert len(seen) == 3, "第二句应该直接走已经证明可用的那条通道"
    assert "translate_a/single" in seen[-1]["url"]
