"""Heartbeat files are the healthcheck for daemons that serve no HTTP."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from aegis_observability.heartbeat import heartbeat_is_fresh, touch_heartbeat


def test_a_missing_file_is_not_fresh(tmp_path: Path) -> None:
    assert heartbeat_is_fresh(tmp_path / "absent", 60) is False


def test_touching_the_file_makes_it_fresh(tmp_path: Path) -> None:
    path = tmp_path / "runner.heartbeat"

    touch_heartbeat(path)

    assert path.exists()
    assert heartbeat_is_fresh(path, 60) is True


def test_an_old_file_goes_stale(tmp_path: Path) -> None:
    path = tmp_path / "runner.heartbeat"
    touch_heartbeat(path)
    old = time.time() - 600
    os.utime(path, (old, old))

    assert heartbeat_is_fresh(path, 120) is False


def test_a_non_positive_window_is_a_programming_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="positive"):
        heartbeat_is_fresh(tmp_path / "x", 0)
