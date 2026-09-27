#!/usr/bin/env python3
"""把一份已经校对好的 SRT 字幕 + 配套音频，直接导入成某个知识库素材
（`podcast_episodes` 行，`kind='audiobook'`）的听读轨道（issue #1228）。

正常的听读生成路径要么用 edge-tts（词级时间轴）要么跑 ASR 对齐（句级，
还可能出错）——但当手上已经有一份人工/别处校对过的 SRT，那些都是多余的：
文字是对的，时间戳是对的，唯一要做的是把 SRT 解析成 `audio_tracks.cues_json`
要的形状，并把同一份文字顺手存成这条素材的 `transcript_zh`，好让已有的
摘要流程（`podcast._process_episode`，会优先复用已存的 transcript_zh，见
其模块说明）直接摘要，不用再跑一次转录。

风格照抄 `scripts/fsrs_optimize.py`：sys.path 插入仓库根、`import database`、
`database.init_db()`、argparse、分步编号的中文进度提示。

默认是 dry run（只解析+校验+打印，不写库）；加 `--write` 才真正写入。

用法：
    python scripts/import_srt_track.py --episode-id 316 \\
        --audio data/audio/source/uploads/xxx.mp3 \\
        --srt "小王子.srt" [--lang zh] [--write]
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 章节标题独占一段的判定：SRT 里的行是无标点的短句，靠"第X章"这个形状识别，
# 不靠标点或长度——书籍目录里"第一章""第十二章""第一百章"都要认得出来。
_CHAPTER_RE = re.compile(r"^第[一二三四五六七八九十百零〇两\d]+章")

# SRT 时间戳：HH:MM:SS,mmm 或 HH:MM:SS.mmm（部分工具导出用句点）。
# re.match 只锚定开头，所以时间戳后面跟的定位信息（"align:middle line:90%"
# 这类 SRT v2 扩展）不会干扰解析。
_TIME_RE = re.compile(r"(\d{2}):(\d{2}):(\d{2})[.,](\d{3})")


def _parse_timestamp(raw: str) -> int:
    """'00:01:02,345' → 62345（毫秒）。格式不对直接抛错——SRT 解析失败不能
    悄悄产出一条时间轴全错的轨道。"""
    m = _TIME_RE.match(raw.strip())
    if not m:
        raise ValueError(f"无法解析的时间戳：{raw!r}")
    h, mnt, sec, ms = (int(x) for x in m.groups())
    return ((h * 3600 + mnt * 60 + sec) * 1000) + ms


def parse_srt(text: str) -> list[dict]:
    """SRT 全文 → 按 start_ms 排序的 [{start_ms, end_ms, text}, ...]。

    容错点：
    - 开头的 UTF-8 BOM
    - CRLF / 单独的 CR 换行
    - 每条字幕可以跨多行——多行拼成一句，中间用一个空格连接（SRT 的行内
      换行只是排版换行，不是句子的真实停顿）
    - 序号行是可选的（有些导出工具会省略）
    - 空文本块（只有时间戳没有内容）直接跳过，不产出哨兵 cue
    """
    text = text.lstrip("﻿")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n\s*\n", text.strip())

    cues: list[dict] = []
    for block in blocks:
        lines = [ln for ln in block.split("\n") if ln.strip() != ""]
        if not lines:
            continue

        idx = 0
        # 序号行：整行只有数字，可选，跳过它而不是当成文本的一部分。
        if re.match(r"^\d+$", lines[idx].strip()):
            idx += 1
        if idx >= len(lines) or "-->" not in lines[idx]:
            # 既不是时间戳行也不是可跳过的序号行——这个块格式不对，跳过
            # 而不是让一条坏块拖垮整份字幕。
            continue

        time_line = lines[idx]
        idx += 1
        parts = time_line.split("-->")
        if len(parts) != 2:
            continue
        try:
            start_ms = _parse_timestamp(parts[0])
            end_ms = _parse_timestamp(parts[1])
        except ValueError:
            continue

        cue_text = " ".join(ln.strip() for ln in lines[idx:] if ln.strip())
        if not cue_text:
            continue

        cues.append({"start_ms": start_ms, "end_ms": end_ms, "text": cue_text})

    cues.sort(key=lambda c: c["start_ms"])
    return cues


def build_source_text(cues: list[dict]) -> tuple[str, list[dict]]:
    """把逐条 cue 的短句拼成一份完整的 source_text，并给每条 cue 补上
    char_start/char_end——`source_text[c["char_start"]:c["char_end"]] ==
    c["text"]` 这个不变式必须对每一条都成立（见 audio/__init__.py 的模块
    说明：char_start/char_end 是前端切割已标注 HTML 的唯一依据，事后重新
    匹配文字会在重复句/有标注的地方错位）。

    拼接规则：相邻 cue 之间用一个空格连接；遇到形如"第X章"的章节标题，
    在它前面插入一个空段落（"\\n\\n"），标题本身自成一段后面也接
    "\\n\\n"。第一条 cue 前面没有任何分隔符。标题后面紧跟的那条 cue
    不再额外加空格——标题自己的尾随 "\\n\\n" 已经是分隔符了，否则会
    变成 "第一章\\n\\n 开篇" 这种"\\n\\n"后面多贴一个空格的错误形状。

    返回 (source_text, cues_with_offsets)——后者是原 cue 字典的浅拷贝，
    多了 char_start/char_end 两个键，顺序与输入一致。
    """
    parts: list[str] = []
    out_cues: list[dict] = []
    pos = 0
    prev_was_heading = False

    for i, cue in enumerate(cues):
        cue_text = cue["text"]
        is_heading = bool(_CHAPTER_RE.match(cue_text))

        if i == 0 or prev_was_heading:
            sep = ""
        elif is_heading:
            sep = "\n\n"
        else:
            sep = " "
        if sep:
            parts.append(sep)
            pos += len(sep)

        char_start = pos
        parts.append(cue_text)
        pos += len(cue_text)
        char_end = pos

        out_cue = dict(cue)
        out_cue["char_start"] = char_start
        out_cue["char_end"] = char_end
        out_cues.append(out_cue)

        if is_heading:
            parts.append("\n\n")
            pos += 2
        prev_was_heading = is_heading

    return "".join(parts), out_cues


def _to_repo_relative(path: str) -> str:
    """把用户传入的音频路径规整成仓库根相对路径——已有的 audio_path 存量
    值都是这种形状（如 data/audio/source/uploads/xxx.mp3），routes/audio.py
    按这个约定拼绝对路径。"""
    abs_path = os.path.abspath(path)
    if abs_path.startswith(REPO_ROOT + os.sep):
        return os.path.relpath(abs_path, REPO_ROOT)
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--episode-id", type=int, required=True)
    parser.add_argument("--audio", required=True, help="mp3 路径（相对仓库根或绝对路径）")
    parser.add_argument("--srt", required=True, help="SRT 字幕文件路径")
    parser.add_argument("--lang", default="zh")
    parser.add_argument("--write", action="store_true", help="不传只做 dry run，不写任何东西")
    args = parser.parse_args()

    import database
    database.init_db()

    print("== 步骤 1/4：读取并校验参数 ==")

    if not os.path.isfile(args.audio):
        print(f"错误：音频文件不存在：{args.audio}")
        sys.exit(1)
    if not os.path.isfile(args.srt):
        print(f"错误：SRT 文件不存在：{args.srt}")
        sys.exit(1)

    episode = database.get_episode(args.episode_id)
    if not episode:
        print(f"错误：找不到 episode_id={args.episode_id}")
        sys.exit(1)
    print(f"素材：#{episode['id']} 《{episode.get('title') or '(无标题)'}》"
          f"（kind={episode.get('kind')}, status={episode.get('status')}）")

    from audio.asr_cloud import _probe_duration_seconds
    try:
        duration_s = _probe_duration_seconds(args.audio)
    except Exception as e:
        print(f"错误：ffprobe 读取音频时长失败：{e}")
        sys.exit(1)
    duration_ms = int(round(duration_s * 1000))
    print(f"音频时长：约 {duration_s:.1f} 秒（{duration_ms} ms）")

    print("\n== 步骤 2/4：解析 SRT ==")
    with open(args.srt, "r", encoding="utf-8") as f:
        srt_text = f.read()
    cues_raw = parse_srt(srt_text)
    if not cues_raw:
        print("错误：SRT 里一条有效字幕都没解析出来，中止（绝不写入空轨道）")
        sys.exit(1)
    print(f"解析出 {len(cues_raw)} 条字幕")
    print(f"第一条：{cues_raw[0]['start_ms']} ms — {cues_raw[0]['text'][:30]!r}")
    print(f"最后一条：{cues_raw[-1]['start_ms']} ms – {cues_raw[-1]['end_ms']} ms"
          f" — {cues_raw[-1]['text'][:30]!r}")

    # 最后一条字幕的结束时间不能明显超出音频实际时长——那多半是传错了
    # 音频文件（比如另一个音质相仿的章节）。留 5 秒余量给字幕/音频两边
    # 各自的收尾误差。
    if cues_raw[-1]["end_ms"] > duration_ms + 5000:
        print(f"错误：最后一条字幕结束于 {cues_raw[-1]['end_ms']} ms，"
              f"超出音频时长 {duration_ms} ms 太多（>5s）——很可能是音频/字幕文件对不上，中止")
        sys.exit(1)

    print("\n== 步骤 3/4：拼接 source_text 并校验偏移量 ==")
    source_text, cues = build_source_text(cues_raw)
    print(f"source_text 长度：{len(source_text)} 字符")
    for c in cues:
        if source_text[c["char_start"]:c["char_end"]] != c["text"]:
            print("错误：char_start/char_end 与 cue 文本对不上（内部 bug，不应该发生），中止")
            sys.exit(1)
    print("偏移量校验通过")
    print(f"预览（前 200 字）：{source_text[:200]!r}")

    audio_path = _to_repo_relative(args.audio)

    if not args.write:
        print("\n== 步骤 4/4：dry run，未写入任何内容 ==")
        print(f"将会写入 audio_tracks: owner=(episode, {args.episode_id}), "
              f"lang={args.lang}, variant=fulltext, audio_path={audio_path}, "
              f"duration_ms={duration_ms}, cues={len(cues)} 条, source='srt'")
        print(f"将会更新 podcast_episodes #{args.episode_id}: "
              f"transcript_zh（{len(source_text)} 字符）, transcript_source='srt', "
              f"audio_url={audio_path}, duration_seconds={int(round(duration_s))}")
        if episode.get("status") in ("no_transcript", "error"):
            print(f"当前 status={episode.get('status')!r} → 将会改为 'pending'（可点 Process 摘要）")
        else:
            print(f"当前 status={episode.get('status')!r} → 不改动（只有 no_transcript/error 才会改）")
        print("加 --write 才会真正写入。")
        return

    print("\n== 步骤 4/4：写入数据库 ==")

    track_id = database.save_audio_track(
        "episode", args.episode_id, args.lang, "fulltext",
        audio_path, duration_ms, cues, "srt", None,
        source_text=source_text,
    )
    print(f"audio_tracks 写入完成，id={track_id}")

    update_fields = {
        "transcript_zh": source_text,
        "transcript_source": "srt",
        "audio_url": audio_path,
        "duration_seconds": int(round(duration_s)),
    }
    if episode.get("status") in ("no_transcript", "error"):
        update_fields["status"] = "pending"
    database.update_episode(args.episode_id, **update_fields)
    print(f"podcast_episodes #{args.episode_id} 已更新：{sorted(update_fields)}")

    import languages
    cleared = []
    for lang in languages.LANGUAGES:
        if database.delete_knowledge_fulltext(args.episode_id, lang):
            cleared.append(lang)
    if cleared:
        print(f"已清空旧的全文阅读缓存（knowledge_fulltexts）：{cleared}")
    else:
        print("没有需要清空的全文阅读缓存")

    print("\n完成。把以上全部输出发给 Claude。")


if __name__ == "__main__":
    main()
