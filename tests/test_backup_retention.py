"""数据库快照保留规则 + cron 安装（issue #1279）。"""
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import backup_retention as br  # noqa: E402

NOW = datetime(2026, 10, 23, 15, 0, 0)


def snap(ts: datetime, suffix: str = "") -> str:
    return f"srs_{ts:%Y-%m-%d_%H-%M-%S}.db{suffix}"


def every_6h(days: int) -> list[str]:
    """模拟 cron：从 NOW 往回每 6 小时一份。"""
    base = NOW.replace(hour=12, minute=0, second=2)
    return [snap(base - timedelta(hours=6 * i)) for i in range(days * 4)]


# ── 纯规则 ──────────────────────────────────────────────

def test_parse_timestamp_only_matches_auto_snapshots():
    assert br.parse_timestamp("srs_2026-10-23_12-00-02.db") == datetime(2026, 10, 23, 12, 0, 2)
    assert br.parse_timestamp("srs_2026-10-23_12-00-02.db.zst") is not None
    assert br.parse_timestamp("pre-728-fix-20260813-083803.db") is None
    assert br.parse_timestamp("audio_track_316_before_srt.sql") is None
    assert br.parse_timestamp("pre_tuning_20260808_1058.db-wal") is None
    assert br.parse_timestamp(".srs_2026-10-23_12-00-02.db.partial") is None
    assert br.parse_timestamp("srs_2026-13-40_12-00-02.db") is None  # 非法日期


def test_last_two_days_all_kept():
    names = every_6h(2)
    plan = br.plan_retention(names, NOW)
    assert sorted(plan.keep) == sorted(names)
    assert plan.delete == []


def test_daily_then_weekly_then_gone():
    names = every_6h(120)  # 4 个月
    plan = br.plan_retention(names, NOW)
    kept = [br.parse_timestamp(n) for n in plan.keep]

    recent = [t for t in kept if NOW - t < timedelta(days=2)]
    daily = [t for t in kept if timedelta(days=2) <= NOW - t < timedelta(days=14)]
    weekly = [t for t in kept if timedelta(days=14) <= NOW - t < timedelta(days=60)]
    old = [t for t in kept if NOW - t >= timedelta(days=60)]

    assert len(recent) == 8                         # 2 天 × 4
    assert len({t.date() for t in daily}) == len(daily)  # 每天至多一份
    assert 11 <= len(daily) <= 13
    weeks = [t.isocalendar()[:2] for t in weekly]
    assert len(set(weeks)) == len(weeks)            # 每周至多一份
    assert 6 <= len(weekly) <= 8
    assert old == []
    assert len(plan.keep) + len(plan.delete) == len(names)


def test_keeps_newest_in_each_day():
    day = datetime(2026, 10, 15)
    names = [snap(day.replace(hour=h)) for h in (0, 6, 12, 18)]
    plan = br.plan_retention(names, NOW)
    assert plan.keep == [snap(day.replace(hour=18))]


def test_newest_always_kept_even_if_ancient():
    old = snap(NOW - timedelta(days=400))
    plan = br.plan_retention([old], NOW)
    assert plan.keep == [old] and plan.delete == []


def test_one_off_backups_never_touched():
    names = every_6h(90) + ["pre-728-fix-20260813-083803.db", "audio_track_316_before_srt.sql"]
    plan = br.plan_retention(names, NOW)
    assert set(plan.ignored) == {"pre-728-fix-20260813-083803.db", "audio_track_316_before_srt.sql"}
    assert not set(plan.ignored) & set(plan.delete)


def test_db_and_zst_with_same_timestamp_treated_together():
    ts = NOW - timedelta(days=100)
    newest = snap(NOW - timedelta(hours=1))
    plan = br.plan_retention([snap(ts), snap(ts, ".zst"), newest], NOW)
    assert set(plan.delete) == {snap(ts), snap(ts, ".zst")}


def test_future_timestamps_kept():
    future = snap(NOW + timedelta(days=3))
    plan = br.plan_retention([future, snap(NOW - timedelta(days=1))], NOW)
    assert future in plan.keep


# ── 真文件：dry-run / apply / compress ─────────────────

def _make_dir(tmp_path, names):
    d = tmp_path / "backups"
    d.mkdir()
    for n in names:
        (d / n).write_bytes(b"x" * 1000)
    return d


def test_dry_run_deletes_nothing(tmp_path, capsys):
    names = every_6h(90) + ["pre-728-move-20260814-034808.db"]
    d = _make_dir(tmp_path, names)
    plan = br.run(str(d), apply=False, compress=True, now=NOW)
    assert sorted(os.listdir(d)) == sorted(names)
    assert plan.delete
    assert "DRY-RUN" in capsys.readouterr().out


def test_apply_deletes_exactly_the_plan(tmp_path):
    names = every_6h(90) + ["pre-728-move-20260814-034808.db"]
    d = _make_dir(tmp_path, names)
    plan = br.run(str(d), apply=True, compress=False, now=NOW)
    left = set(os.listdir(d))
    assert left == set(plan.keep) | {"pre-728-move-20260814-034808.db"}


@pytest.mark.skipif(shutil.which("zstd") is None, reason="zstd not installed")
def test_compress_replaces_db_with_valid_zst(tmp_path):
    name = snap(NOW - timedelta(hours=1))
    d = tmp_path / "b"
    d.mkdir()
    db = d / name
    con = sqlite3.connect(db)
    con.execute("create table t(x)")
    con.execute("insert into t values (42)")
    con.commit()
    con.close()

    br.run(str(d), apply=True, compress=True, now=NOW)
    assert os.listdir(d) == [name + ".zst"]
    out = tmp_path / "restored.db"
    subprocess.run(["zstd", "-q", "-d", str(d / (name + ".zst")), "-o", str(out)], check=True)
    assert sqlite3.connect(out).execute("select x from t").fetchone() == (42,)


# ── backup_db.sh 端到端 ─────────────────────────────────

@pytest.mark.skipif(shutil.which("sqlite3") is None, reason="sqlite3 CLI not installed")
def test_backup_db_script_end_to_end(tmp_path):
    db = tmp_path / "srs.db"
    con = sqlite3.connect(db)
    con.execute("create table t(x)")
    con.commit()
    con.close()
    bdir = tmp_path / "backups"
    bdir.mkdir()
    (bdir / "pre-728-fix-20260813-083803.db").write_bytes(b"keep me")
    ancient = bdir / "srs_2020-01-01_00-00-00.db"
    ancient.write_bytes(b"old")

    env = {**os.environ, "BACKUP_DB": str(db), "BACKUP_DIR": str(bdir),
           "BACKUP_PYTHON": sys.executable}
    r = subprocess.run(["bash", os.path.join(ROOT, "scripts", "backup_db.sh")],
                       env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr + r.stdout
    files = sorted(os.listdir(bdir))
    assert "pre-728-fix-20260813-083803.db" in files
    assert not ancient.exists()
    snaps = [f for f in files if f.startswith("srs_")]
    assert len(snaps) == 1
    if shutil.which("zstd"):
        assert snaps[0].endswith(".db.zst")
    assert not [f for f in files if f.endswith(".partial")]


# ── install_cron.sh：用假的 crontab 命令 ────────────────

FAKE_CRONTAB = """#!/bin/bash
STORE="$FAKE_CRON_STORE"
if [ "$1" = "-l" ]; then
  if [ -f "$STORE" ]; then cat "$STORE"; else echo "no crontab for test" >&2; exit 1; fi
elif [ "$1" = "-" ]; then
  cat > "$STORE"; echo x >> "$STORE.writes"
else
  exit 2
fi
"""

LEGACY = ('0 */6 * * * sqlite3 /home/anki/biangbiangmian3000/data/srs.db ".backup '
          '/home/anki/biangbiangmian3000/data/backups/srs_$(date +\\%Y-\\%m-\\%d_\\%H-\\%M-\\%S).db"')
OTHER = "*/2 * * * * /home/anki/biangbiangmian3000/deploy/deploy.sh >> /home/anki/deploy.log 2>&1"
UNRELATED = "* * * * * /home/anki/hoersaal/deploy/deploy.sh >> /home/anki/hoersaal-deploy.log 2>&1"


@pytest.fixture
def fake_cron(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "crontab").write_text(FAKE_CRONTAB)
    os.chmod(bindir / "crontab", 0o755)
    store = tmp_path / "crontab.txt"
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "FAKE_CRON_STORE": str(store)}

    def run(*args):
        return subprocess.run(["bash", os.path.join(ROOT, "deploy", "install_cron.sh"), *args],
                              env=env, capture_output=True, text=True)
    return store, run


def _writes(store):
    w = store.parent / (store.name + ".writes")
    return len(w.read_text().splitlines()) if w.exists() else 0


def test_install_cron_replaces_legacy_line_and_keeps_others(fake_cron):
    store, run = fake_cron
    store.write_text("\n".join([OTHER, LEGACY, UNRELATED]) + "\n")
    r = run()
    assert r.returncode == 0, r.stderr
    text = store.read_text()
    assert LEGACY not in text
    assert OTHER in text and UNRELATED in text
    assert f"/bin/bash {ROOT}/scripts/backup_db.sh" in text
    assert text.count("biangbiangmian3000 managed") == 2  # begin + end


def test_install_cron_is_idempotent(fake_cron):
    store, run = fake_cron
    store.write_text("\n".join([OTHER, LEGACY]) + "\n")
    run()
    first = store.read_text()
    assert _writes(store) == 1
    r = run()
    assert r.returncode == 0 and r.stdout == ""
    assert store.read_text() == first
    assert _writes(store) == 1  # 没有第二次写入


def test_install_cron_dry_run_writes_nothing(fake_cron):
    store, run = fake_cron
    store.write_text(LEGACY + "\n")
    r = run("--dry-run")
    assert r.returncode == 0
    assert "backup_db.sh" in r.stdout
    assert store.read_text() == LEGACY + "\n"
    assert _writes(store) == 0


def test_install_cron_with_no_crontab_yet(fake_cron):
    store, run = fake_cron
    r = run()
    assert r.returncode == 0, r.stderr
    assert "backup_db.sh" in store.read_text()
