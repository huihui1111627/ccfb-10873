#!/usr/bin/env python3
"""Score the platform against the planted ground truth and print a report card.

    python3 evaluate.py                 # evaluate the latest persisted analysis
    python3 evaluate.py --rerun         # re-run analysis first, then evaluate
    python3 evaluate.py --json out.json # also dump the full JSON result

This is the script that produces the precision / recall numbers.  The ground
truth file is read *only* here (and in the tests); the detection pipeline never
sees it.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from claimnet import analysis, db  # noqa: E402
from claimnet.evaluate import evaluate_from_db, load_truth  # noqa: E402


def bar(value: float, width: int = 22) -> str:
    filled = round(value * width)
    return "█" * filled + "░" * (width - filled)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rerun", action="store_true", help="run analysis first")
    ap.add_argument("--analysis-id", type=int)
    ap.add_argument("--json", type=Path, help="write full result JSON here")
    args = ap.parse_args()

    if db.counts()["claims"] == 0:
        print("数据库为空。先运行:  python3 run.py --no-bootstrap  "
              "或 python3 -m claimnet.etl", file=sys.stderr)
        sys.exit(1)

    if args.rerun:
        print("运行分析…")
        analysis.run_analysis(persist=True)

    res = evaluate_from_db(args.analysis_id)
    truth = load_truth()
    s = res["gang_level"]["strict"]
    m = res["gang_level"]["majority"]
    p = res["person_level"]
    w = res["weak_pendants"]

    print()
    print("═" * 62)
    print(f"  ClaimNet 评估报告   ·   分析批次 #{res['analysis_id']}")
    print(f"  真实团伙 {s['truth_gangs']} 个 / 预测团伙 {s['predicted_gangs']} 个"
          f" / 涉案人员 {truth['totals']['persons']} 人")
    print("═" * 62)
    print("\n【团伙级 · 严格匹配】双向重合 ≥80%")
    print(f"  精确率  {s['precision']:.1%}  {bar(s['precision'])}")
    print(f"  召回率  {s['recall']:.1%}  {bar(s['recall'])}")
    print(f"  F1      {s['f1']:.1%}  {bar(s['f1'])}")
    print(f"  TP={s['tp']}  FP={s['fp']}  FN={s['fn']}")
    print("\n【团伙级 · 多数匹配】双向重合 ≥50%")
    print(f"  精确率  {m['precision']:.1%}   召回率 {m['recall']:.1%}   F1 {m['f1']:.1%}")
    print("\n【人员级】")
    print(f"  精确率  {p['precision']:.1%}  {bar(p['precision'])}")
    print(f"  召回率  {p['recall']:.1%}  {bar(p['recall'])}")
    print(f"  TP={p['tp']}  FP={p['fp']}  FN={p['fn']}")
    print("\n【弱关联挂接成员】数据中仅通过一跳弱关联挂上核心的人")
    print(f"  召回率  {w['recall']:.1%}  ({w['caught']}/{w['count']})  "
          f"其中正确归属 {w['attached_to_correct_gang']} 人")

    print("\n【排序质量】评分最高的前 K 个团伙覆盖了多少真实团伙")
    for k, v in res["rank_quality"]["top_k_truth_gangs_covered"].items():
        print(f"  Top {k:>2}: {v:>2} / {s['truth_gangs']}")

    if res["missed_gangs"]:
        print("\n【漏检团伙】")
        for g in res["missed_gangs"]:
            print(f"  {g['gang_id']}  {g['style']:<14} {g['size']} 人")
    if res["false_positive_gangs"]:
        print("\n【误报团伙】")
        for g in res["false_positive_gangs"]:
            print(f"  {g['gang_id']}  {g['score']} 分  {g['size']} 人  "
                  f"{(g['reasons'][0] if g['reasons'] else '')[:44]}")
    print("\n" + "═" * 62)
    print("  说明：严格匹配要求预测团伙与真实团伙双向重合 ≥80%，")
    print("        多带一两个弱挂接成员的可解释误差会体现为人员级 FP，")
    print("        而不是团伙级误报。")
    print("═" * 62)

    if args.json:
        args.json.write_text(json.dumps(res, ensure_ascii=False, indent=2),
                             encoding="utf-8")
        print(f"\n完整 JSON 已写入 {args.json}")


if __name__ == "__main__":
    main()
