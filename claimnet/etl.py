"""Load raw JSON documents into the SQLite claim book."""
from __future__ import annotations

import json
from pathlib import Path

from . import db
from .config import RAW_DIR


def _load(name: str, raw_dir: Path) -> list[dict]:
    p = raw_dir / f"{name}.json"
    if not p.exists():
        return []
    return json.loads(p.read_text(encoding="utf-8"))


def load_raw(raw_dir: Path = RAW_DIR, name: str = "baseline", reset: bool = False) -> dict:
    if reset:
        db.reset_all()
    result = db.import_batch(
        name=name,
        policies=_load("policies", raw_dir),
        claims=_load("claims", raw_dir),
        surveys=_load("surveys", raw_dir),
        shops=_load("shops", raw_dir),
        note=f"loaded from {raw_dir}",
    )
    return result


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--raw-dir", type=Path, default=RAW_DIR)
    ap.add_argument("--name", default="baseline")
    ap.add_argument("--reset", action="store_true")
    args = ap.parse_args()
    print(json.dumps(load_raw(args.raw_dir, args.name, args.reset),
                     ensure_ascii=False, indent=2))
