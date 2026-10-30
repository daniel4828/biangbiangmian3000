"""Tests for the /api/version deploy timestamp (issue #706).

The frontend version badge (#450) was removed in #1329; only the API contract
is tested now. `deployed_at` must carry a UTC offset: the production server
runs on Asia/Shanghai, and a client parsing an offset-less ISO date-time reads
it as its own local time, so no client-side formatting could recover the real
instant.
"""
from datetime import datetime

from fastapi.testclient import TestClient

import main


def test_deployed_at_carries_a_utc_offset():
    resp = TestClient(main.app).get("/api/version")
    assert resp.status_code == 200
    deployed_at = resp.json()["deployed_at"]
    # Parsing an offset-less string yields a naive datetime — exactly the bug.
    assert datetime.fromisoformat(deployed_at).tzinfo is not None, deployed_at
