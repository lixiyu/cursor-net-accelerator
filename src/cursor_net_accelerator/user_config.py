"""Load / save user config under Application Support."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .paths import config_path, state_dir

log = logging.getLogger(__name__)

DEFAULTS: dict[str, Any] = {
    "gate_test_enabled": False,
    "transport": "dns",  # dns | proxy
    "probe_busy_interval_seconds": 60.0,
    "probe_idle_interval_seconds": 600.0,
    "probe_idle_after_seconds": 300.0,
    "probe_min_gap_seconds": 15.0,
}


def ensure_defaults() -> Path:
    """Create config.toml with defaults if missing. Returns path."""
    path = config_path()
    if path.is_file():
        return path
    state_dir().mkdir(parents=True, exist_ok=True)
    save(DEFAULTS)
    log.info("wrote default config %s", path)
    return path


def load() -> dict[str, Any]:
    """Return merged defaults + file values (file wins for known keys)."""
    ensure_defaults()
    path = config_path()
    raw = _read_toml(path)
    out = dict(DEFAULTS)
    for key in DEFAULTS:
        if key in raw:
            out[key] = raw[key]
    # normalize
    transport = str(out.get("transport") or "dns").strip().lower()
    out["transport"] = transport if transport in ("dns", "proxy") else "dns"
    out["gate_test_enabled"] = bool(out.get("gate_test_enabled"))
    for k in (
        "probe_busy_interval_seconds",
        "probe_idle_interval_seconds",
        "probe_idle_after_seconds",
        "probe_min_gap_seconds",
    ):
        try:
            out[k] = float(out[k])
        except (TypeError, ValueError):
            out[k] = DEFAULTS[k]
    return out


def update(**kwargs: Any) -> dict[str, Any]:
    """Merge kwargs into config file and return full config."""
    cur = load()
    for k, v in kwargs.items():
        if k in DEFAULTS:
            cur[k] = v
    # re-normalize via load path
    save(cur)
    return load()


def save(data: dict[str, Any]) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    merged = dict(DEFAULTS)
    merged.update({k: data[k] for k in DEFAULTS if k in data})
    text = _to_toml(merged)
    path.write_text(text, encoding="utf-8")


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        import tomllib
    except ImportError:  # pragma: no cover
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        log.warning("config read failed %s: %s", path, exc)
        return {}


def _to_toml(data: dict[str, Any]) -> str:
    lines = [
        "# cursor-net-accelerator 用户配置",
        "# 路径: ~/Library/Application Support/cursor-net-accelerator/config.toml",
        "",
        f"gate_test_enabled = {_toml_bool(bool(data.get('gate_test_enabled')))}",
        f'transport = "{_esc(str(data.get("transport") or "dns"))}"',
        "",
        f"probe_busy_interval_seconds = {float(data['probe_busy_interval_seconds'])}",
        f"probe_idle_interval_seconds = {float(data['probe_idle_interval_seconds'])}",
        f"probe_idle_after_seconds = {float(data['probe_idle_after_seconds'])}",
        f"probe_min_gap_seconds = {float(data['probe_min_gap_seconds'])}",
        "",
    ]
    return "\n".join(lines)


def _toml_bool(v: bool) -> str:
    return "true" if v else "false"


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')
