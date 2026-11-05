"""Run this process on the user's clock, whatever the server's zone is (#1456).

The server stays on Asia/Shanghai (other projects share it), so the zone is set
per process. Imported by database/core.py, which makes it apply to the app and
to every script that imports database. APP_TIMEZONE overrides the default;
an empty value keeps the system zone.
"""
import os
import time

DEFAULT_TIMEZONE = "Europe/Berlin"


def apply() -> None:
    zone = os.environ.get("APP_TIMEZONE", DEFAULT_TIMEZONE)
    if zone and hasattr(time, "tzset"):
        os.environ["TZ"] = zone
        time.tzset()


apply()
