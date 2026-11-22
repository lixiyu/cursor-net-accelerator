"""Local state paths under Application Support."""

from __future__ import annotations

import os
from pathlib import Path


def state_dir() -> Path:
    env = os.environ.get("CURSOR_NET_ACCEL_STATE_DIR")
    if env:
        return Path(env).expanduser()
    return Path.home() / "Library/Application Support/cursor-net-accelerator"


def data_dir() -> Path:
    """Runtime data (health history, caches). Keep small; prune by retention."""
    return state_dir() / "data"


def health_history_path() -> Path:
    return data_dir() / "health-history.json"


def happy_eyeballs_gate_path() -> Path:
    # 已有文件在 state 根目录；发布成 App 时可再挪到 data/
    return state_dir() / "happy-eyeballs-gate.json"


def config_path() -> Path:
    return state_dir() / "config.toml"
