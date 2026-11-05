"""app_tz sets the process time zone (#1456). Subprocesses: the zone is process-global."""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CODE = 'import app_tz, time; print(time.strftime("%Z"))'


def _run(code, env_changes, remove=()):
    env = os.environ.copy()
    for key in remove:
        env.pop(key, None)
    env.update(env_changes)
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, env=env,
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_default_is_german_time():
    out = _run(CODE, {"TZ": "Asia/Shanghai"}, remove=("APP_TIMEZONE",))
    assert out in ("CET", "CEST")


def test_app_timezone_overrides_default():
    assert _run(CODE, {"APP_TIMEZONE": "Asia/Shanghai"}) == "CST"


def test_empty_app_timezone_keeps_system_zone():
    assert _run(CODE, {"APP_TIMEZONE": "", "TZ": "Asia/Shanghai"}) == "CST"


def test_database_import_applies_it(tmp_path):
    code = 'import database.core, time; print(time.strftime("%Z"))'
    out = _run(
        code,
        {"TZ": "Asia/Shanghai", "DB_PATH": str(tmp_path / "tz.db")},
        remove=("APP_TIMEZONE",),
    )
    assert out in ("CET", "CEST")
