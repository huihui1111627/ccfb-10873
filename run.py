#!/usr/bin/env python3
"""ClaimNet launcher: bootstrap data if needed, then serve the app.

    python3 run.py                  # generate data + analyse + serve on :8000
    python3 run.py --port 9000
    python3 run.py --no-bootstrap   # serve whatever is already in the DB
    python3 run.py --regen          # rebuild the synthetic book from scratch
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def bootstrap(force: bool = False) -> None:
    from claimnet import analysis, db, etl
    from claimnet.config import RAW_DIR

    counts = db.counts()
    if force or counts["claims"] == 0:
        if force or not (RAW_DIR / "claims.json").exists():
            print("→ 生成合成理赔数据（45 个植入团伙）…")
            from claimnet.synth import generate
            gen = generate()
            info = gen.export()
            print(f"   {info['claims']} 笔理赔 / {info['gangs']} 个团伙 / "
                  f"{info['persons'] if 'persons' in info else gen.persons.__len__()} 人")
        print("→ 导入数据库…")
        etl.load_raw(reset=True)
        counts = db.counts()

    if counts["analyses"] == 0 or force:
        print("→ 运行首次团伙分析…")
        result = analysis.run_analysis(persist=True)
        print(f"   识别 {result['stats']['gangs']} 个团伙"
              f"（{result['stats']['duration_ms']} ms）")

    # make sure every gang in the latest run has an investigation case, even
    # when the analysis pre-existed (e.g. bootstrap was interrupted)
    from claimnet import feedback
    latest = db.get_conn().execute(
        "SELECT id FROM analyses ORDER BY id DESC LIMIT 1").fetchone()
    if latest:
        rows = db.get_conn().execute(
            "SELECT gang_id FROM gangs WHERE analysis_id=?", (latest["id"],)).fetchall()
        for row in rows:
            feedback.ensure_case(row["gang_id"], latest["id"])
    print(f"   数据版本 {db.data_version()} · 理赔 {db.counts()['claims']} 笔")


def main() -> None:
    ap = argparse.ArgumentParser(description="ClaimNet 车险团伙欺诈挖掘平台")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--no-bootstrap", action="store_true")
    ap.add_argument("--regen", action="store_true", help="regenerate synthetic data")
    ap.add_argument("--reload", action="store_true")
    args = ap.parse_args()

    if not args.no_bootstrap:
        bootstrap(force=args.regen)

    import uvicorn
    print(f"\n  ClaimNet  →  http://{args.host}:{args.port}\n")
    uvicorn.run("claimnet.api:app", host=args.host, port=args.port,
                reload=args.reload, log_level="info")


if __name__ == "__main__":
    main()
