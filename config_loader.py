"""Repo config loader (config.json).

This project uses a single JSON config file as the source of truth for
runtime parameters. Entry scripts read from it rather than argparse flags.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional


def default_config_path() -> Path:
    # config_loader.py lives at repo root next to config.json
    return Path(__file__).resolve().parent / "config.json"


def load_config(path: Optional[str | Path] = None) -> Dict[str, Any]:
    cfg_path = Path(path) if path is not None else default_config_path()
    if not cfg_path.exists():
        raise FileNotFoundError(f"Config file not found: {cfg_path}")
    with cfg_path.open("r", encoding="utf-8") as f:
        cfg = json.load(f)
    if not isinstance(cfg, dict):
        raise ValueError("config.json must contain a JSON object at the top level")
    return cfg


def get_section(cfg: Dict[str, Any], section: str) -> Dict[str, Any]:
    val = cfg.get(section)
    if val is None:
        return {}
    if not isinstance(val, dict):
        raise ValueError(f'config section "{section}" must be an object')
    return val

