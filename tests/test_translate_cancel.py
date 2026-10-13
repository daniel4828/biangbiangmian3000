"""#1265: Cancel must interrupt a hung translation within ~0.5 s polling."""
import threading
import time


import ai
import translator


def test_cancel_interrupts_a_blocked_translation(monkeypatch):
    release = threading.Event()

    def blocked(texts, target="de", source="zh-CN", on_progress=None):
        release.wait(5)
        return ["x"] * len(texts)

    monkeypatch.setattr(translator, "translate_batch", blocked)
    sentences = [{"sentence_zh": "你好"}]
    outcome = {}

    def run():
        try:
            ai._fill_translations(sentences, progress_key="k1265")
            outcome["result"] = "finished"
        except ai.StoryCancelled:
            outcome["result"] = "cancelled"

    th = threading.Thread(target=run)
    t0 = time.time()
    th.start()
    time.sleep(0.2)
    ai.request_cancel("k1265")
    th.join(2)
    try:
        assert not th.is_alive(), "cancel did not interrupt the translation"
        assert outcome["result"] == "cancelled"
        assert time.time() - t0 < 2
    finally:
        ai.clear_cancel("k1265")
        release.set()


def test_translation_fills_sentences_without_cancel(monkeypatch):
    monkeypatch.setattr(translator, "translate_batch",
                        lambda texts, target="de", source="zh-CN", on_progress=None:
                        [f"de:{t}" for t in texts])
    sentences = [{"sentence_zh": "你好"}, {"sentence_zh": "再见"}]

    ai._fill_translations(sentences, progress_key="k1265b")
    ai._story_progress.pop("k1265b", None)

    assert [s["sentence_de"] for s in sentences] == ["de:你好", "de:再见"]
