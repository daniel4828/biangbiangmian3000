#!/usr/bin/env python3
"""数据库快照的保留规则（issue #1279）。

服务器 cron 每 6 小时往 data/backups/ 写一份 srs.db 的完整快照，原来从不删除，
三个半月攒到 37 GB。这个脚本按"越旧越稀疏"的规则清理：

- 最近 KEEP_ALL_DAYS（2）天：全部保留
- 到 DAILY_DAYS（14）天为止：每个日历日只留最新一份
- 到 WEEKLY_DAYS（60）天为止：每个 ISO 周只留最新一份
- 更早：删除
- 最新的一份永远保留（哪怕时钟出了问题）

只认 `srs_YYYY-MM-DD_HH-MM-SS.db`（以及压缩后的 `.db.zst` / `.db.gz`）。
手工做的一次性备份（`pre-728-*.db`、`audio_track_*_before_srt.sql` 等）
名字对不上，一律不碰——那些由 Daniel 自己决定。

默认是 dry-run（只打印计划），加 `--apply` 才真的删。`--compress` 会把保留下来、
还没压缩的 `.db` 用 zstd 压缩（约压到 1/4）。只依赖标准库 + 系统 zstd。

用法：
    python3 scripts/backup_retention.py --dir data/backups            # 只看计划
    python3 scripts/backup_retention.py --dir data/backups --apply --compress
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta

KEEP_ALL_DAYS = 2
DAILY_DAYS = 14
WEEKLY_DAYS = 60

_NAME_RE = re.compile(
    r"^srs_(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})\.db(\.zst|\.gz)?$")


def parse_timestamp(name: str) -> datetime | None:
    """快照文件名里的时间戳；不是自动快照的文件返回 None。"""
    m = _NAME_RE.match(name)
    if not m:
        return None
    try:
        return datetime(*(int(g) for g in m.groups()[:6]))
    except ValueError:
        return None


@dataclass
class Plan:
    keep: list[str] = field(default_factory=list)
    delete: list[str] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)  # 名字不匹配，不碰


def plan_retention(names: list[str], now: datetime, *,
                   keep_all_days: int = KEEP_ALL_DAYS,
                   daily_days: int = DAILY_DAYS,
                   weekly_days: int = WEEKLY_DAYS) -> Plan:
    """纯函数：给定文件名列表和当前时间，决定哪些留、哪些删。

    同一时间戳的多个文件（例如压缩中断留下的 .db 和 .db.zst）作为一组处理。
    """
    plan = Plan()
    by_ts: dict[datetime, list[str]] = {}
    for name in names:
        ts = parse_timestamp(name)
        if ts is None:
            plan.ignored.append(name)
        else:
            by_ts.setdefault(ts, []).append(name)

    if not by_ts:
        return plan

    keep_ts: set[datetime] = {max(by_ts)}  # 最新一份永远保留
    newest_in_bucket: dict[tuple, datetime] = {}
    for ts in by_ts:
        age = now - ts
        if age < timedelta(days=keep_all_days):  # 含"未来"的时间戳
            keep_ts.add(ts)
            continue
        if age < timedelta(days=daily_days):
            bucket = ("day", ts.date())
        elif age < timedelta(days=weekly_days):
            iso = ts.isocalendar()
            bucket = ("week", iso[0], iso[1])
        else:
            continue  # 太旧，删除
        if bucket not in newest_in_bucket or ts > newest_in_bucket[bucket]:
            newest_in_bucket[bucket] = ts
    keep_ts.update(newest_in_bucket.values())

    for ts in sorted(by_ts):
        target = plan.keep if ts in keep_ts else plan.delete
        target.extend(sorted(by_ts[ts]))
    return plan


def _size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _fmt(n: int) -> str:
    return f"{n / 1024 ** 3:.2f} GB" if n >= 1024 ** 3 else f"{n / 1024 ** 2:.1f} MB"


def compress_file(path: str) -> str:
    """zstd 压缩一个 .db：先写 .partial，校验通过再改名，最后才删原件。"""
    final = path + ".zst"
    tmp = final + ".partial"
    subprocess.run(["zstd", "-q", "-f", "-T0", "-3", path, "-o", tmp], check=True)
    subprocess.run(["zstd", "-q", "-t", tmp], check=True)
    os.replace(tmp, final)
    shutil.copystat(path, final)
    os.remove(path)
    return final


def run(backup_dir: str, *, apply: bool, compress: bool, now: datetime | None = None,
        out=None) -> Plan:
    out = out or sys.stdout
    now = now or datetime.now()
    names = sorted(n for n in os.listdir(backup_dir)
                   if os.path.isfile(os.path.join(backup_dir, n)))
    plan = plan_retention(names, now)
    mode = "APPLY" if apply else "DRY-RUN"

    freed = sum(_size(os.path.join(backup_dir, n)) for n in plan.delete)
    kept = sum(_size(os.path.join(backup_dir, n)) for n in plan.keep)
    print(f"[{now:%Y-%m-%d %H:%M:%S}] {mode} {backup_dir}: "
          f"保留 {len(plan.keep)} 个（{_fmt(kept)}），删除 {len(plan.delete)} 个（{_fmt(freed)}），"
          f"不匹配未动 {len(plan.ignored)} 个", file=out)
    for n in plan.delete:
        print(f"  删除 {n}", file=out)

    if apply:
        for n in plan.delete:
            os.remove(os.path.join(backup_dir, n))

    to_compress = [n for n in plan.keep if n.endswith(".db")
                   and (n + ".zst") not in plan.keep]
    if compress and to_compress:
        if shutil.which("zstd") is None:
            print("  zstd 不存在，跳过压缩", file=out)
        else:
            for n in to_compress:
                if apply:
                    compress_file(os.path.join(backup_dir, n))
                print(f"  压缩 {n}{'' if apply else '（dry-run）'}", file=out)
    return plan


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dir", default=os.path.join("data", "backups"))
    p.add_argument("--apply", action="store_true", help="真的删除/压缩（默认只打印计划）")
    p.add_argument("--compress", action="store_true", help="用 zstd 压缩保留下来的 .db")
    args = p.parse_args(argv)
    run(args.dir, apply=args.apply, compress=args.compress)
    return 0


if __name__ == "__main__":
    sys.exit(main())
