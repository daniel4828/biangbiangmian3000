#!/usr/bin/env python3
"""本地转录任务的闲时 worker（issue #1053，总议题 #1047）。

whisper.cpp 在这台服务器（4 核 AMD EPYC，无 GPU）上跑 large-v3 大约**比
实时慢 1–3 倍**：一小时音频要跑一到三小时，而且三个核全满。所以它绝不能
在 HTTP 请求里同步发生，也不能想跑就跑——同一台机器上还有 web 应用、每
2 分钟一次的部署 cron、每 5 分钟一次的邮件/Signal 收件。

Daniel 2026-09-05 定的规则就是这个脚本的全部内容：**「把 whisper 的运行
放在我不用服务器的时候，实际上那是大部分时间。」**

每 5 分钟由 cron 触发一次，四道闸门任何一道不过就立刻退出：

    1. 没有 pending 任务
    2. `last_user_activity` 在 _IDLE_MINUTES 分钟以内（他在用）
    3. 当前时间落在早晨预生成窗口里（别和 scripts/morning_pregen.py 抢）
    4. 上一轮还没跑完（PID 锁）

跑起来之后仍然**边跑边看**活动时间戳：他一回来就 SIGTERM 掉 whisper.cpp，
把任务标回 pending，下一轮重来。转录是幂等的，重来不花钱也不丢东西——所以
被打断记 pending 而不是 error（见 audio.AudioTrackAborted 的 docstring）。

🔴 服务器时区是 `Asia/Shanghai`（UTC+8），不是德国时间（CLAUDE.md 记着这条，
2026-08-14 实测确认）。下面的时间窗口用 `datetime.now()` 即服务器本地时间；
按德国时间推算会让窗口整个错开六七个小时。

和 scripts/knowledge_mail_check.py 一样直接 `import database` +
`database.init_db()` 在本进程里干活，不走 HTTP。

用法：
    python scripts/audio_worker.py

环境变量（见 CLAUDE.md 环境变量表）：
    WHISPER_CPP_PATH    whisper.cpp 可执行文件，默认 whisper-cli
    WHISPER_CPP_MODEL   模型文件路径
    DB_PATH             数据库路径，默认 data/srs.db
"""
import fcntl
import os
import signal
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 他多久没动过服务器才算「不在用」。半小时足够跨过一次泡茶，又不至于让
# 队列在他真的走开之后还空等太久。
_IDLE_MINUTES = 30

# 早晨预生成窗口（服务器本地时间，见模块 docstring 里的时区警告）。
# scripts/morning_pregen.py 在这段时间里生成故事和 TTS，两边抢 CPU 的结果
# 是他早上打开应用时故事还没好——那比晚几小时拿到转录严重得多。
_QUIET_START = (5, 30)
_QUIET_END = (9, 30)

# 整轮硬上限。audio/asr_local.py 自己有 6 小时的上限，这里再高一点兜底：
# 万一卡在转码或别的地方，锁必须能释放，否则这个功能就永久停摆了
# （#991 的教训：一个永不返回的进程等于永久停掉这个功能）。
_WATCHDOG_SECONDS = 7 * 60 * 60

LOCK_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", ".audio_worker.lock")


def _log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


class _Timeout(Exception):
    pass


def _watchdog(signum, frame):
    raise _Timeout(f"整轮超过 {_WATCHDOG_SECONDS} 秒仍未完成，放弃本轮（锁已释放，下一轮重试）")


def _seconds_since_activity() -> float | None:
    """距离上一次「人」的请求过了多少秒；从来没记录过则返回 None。

    时间戳由 main.py 的 record_activity 中间件写入，它**刻意排除了前端的
    轮询端点**——不排除的话，一个一直开着的标签页就等于他一直在用，这个
    worker 永远轮不到。
    """
    import database
    raw = database.get_app_setting("last_user_activity")
    if not raw:
        return None
    try:
        return max(0.0, time.time() - float(raw))
    except ValueError:
        # 存进去的不是时间戳（不该发生）。当作「不知道他在不在用」处理，
        # 也就是**不跑**——宁可晚几小时转录，也不要在他正用着的时候占满 CPU。
        _log(f"last_user_activity 无法解析（{raw!r}），保守起见本轮跳过。")
        return 0.0


def _in_quiet_window(now: datetime | None = None) -> bool:
    now = now or datetime.now()   # 服务器本地时间 = Asia/Shanghai，见模块 docstring
    minutes = now.hour * 60 + now.minute
    return _QUIET_START[0] * 60 + _QUIET_START[1] <= minutes < _QUIET_END[0] * 60 + _QUIET_END[1]


def _run_one_job(job: dict) -> int:
    """跑一条任务，返回进程退出码。"""
    import audio
    import database

    job_id = job["id"]
    _log(f"步骤 4/4：开始转录任务 #{job_id}"
         f"（{job['owner_kind']} {job['owner_id']}，lang={job['lang']}，"
         f"{'有正确文本 → 文本锚定对齐' if job.get('text_hint') else '纯 ASR'}）"
         f"——一小时音频约需一到三小时，期间他一回来就会中止")

    def _should_abort() -> bool:
        idle = _seconds_since_activity()
        return idle is not None and idle < _IDLE_MINUTES * 60

    started = time.time()
    try:
        track = audio.build_track(
            text=job.get("text_hint") or None,
            audio_path=job["audio_path"],
            lang=job["lang"],
            prefer_local=True,
            should_abort=_should_abort,
        )
    except audio.AudioTrackAborted as e:
        # 不是失败：他回来了，让位而已。标回 pending，下一轮从头再来。
        database.requeue_audio_job(job_id)
        _log(f"任务 #{job_id} 让位中止（{e}），已标回 pending，下一轮重试。")
        return 0
    except audio.AudioTrackError as e:
        database.finish_audio_job(job_id, error=str(e))
        _log(f"任务 #{job_id} 失败：{e}")
        return 1

    database.save_audio_track(
        job["owner_kind"], job["owner_id"], job["lang"], job["variant"],
        track.audio_path, track.duration_ms, [c.to_dict() for c in track.cues],
        track.source, track.voice, source_text=track.source_text,
    )
    database.finish_audio_job(job_id)
    mins = (time.time() - started) / 60
    _log(f"任务 #{job_id} 完成：{len(track.cues)} 个 cue，耗时 {mins:.0f} 分钟。")
    return 0


def main() -> int:
    signal.signal(signal.SIGALRM, _watchdog)
    signal.alarm(_WATCHDOG_SECONDS)

    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    lock_file = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        # 8 GB 内存放不下两个 whisper，这道锁不是优化是必需品。
        _log("上一轮转录仍在进行，本轮跳过。")
        return 0

    try:
        lock_file.write(str(os.getpid()))
        lock_file.flush()

        import database
        database.init_db()

        _log("步骤 1/4：看有没有排队的转录任务")
        pending = database.list_audio_jobs(statuses=("pending",))
        if not pending:
            _log("没有排队的任务，退出。")
            return 0
        _log(f"排队中：{len(pending)} 条")

        _log(f"步骤 2/4：看他是不是在用服务器（{_IDLE_MINUTES} 分钟内有动作就让位）")
        idle = _seconds_since_activity()
        if idle is not None and idle < _IDLE_MINUTES * 60:
            _log(f"{idle / 60:.0f} 分钟前还有动作，本轮跳过。")
            return 0
        _log("没有人在用" if idle is None else f"已闲置 {idle / 60:.0f} 分钟")

        _log(f"步骤 3/4：避开早晨预生成窗口（{_QUIET_START[0]:02d}:{_QUIET_START[1]:02d}–"
             f"{_QUIET_END[0]:02d}:{_QUIET_END[1]:02d}，服务器本地时间）")
        if _in_quiet_window():
            _log(f"当前 {datetime.now():%H:%M} 在窗口内，本轮跳过。")
            return 0

        job = database.claim_next_audio_job()
        if not job:
            # 上面查过有 pending，到这里却拿不到 —— 说明另一个进程刚抢走。
            # 锁本该挡住这种情况，但不假设它一定成立。
            _log("任务已被其它进程取走，本轮跳过。")
            return 0
        return _run_one_job(job)
    except _Timeout as e:
        _log(f"看门狗：{e}")
        return 1
    except Exception as e:
        _log(f"转录 worker 异常：{e}")
        return 1
    finally:
        signal.alarm(0)
        try:
            fcntl.flock(lock_file, fcntl.LOCK_UN)
        except OSError:
            pass
        lock_file.close()


if __name__ == "__main__":
    sys.exit(main())
