"""Evaluation against the planted ground truth.

Metrics are computed at *gang level* (the unit the user asked for) and at
member level, with several matching rules so the numbers cannot be gamed:

* strict   : >=80% of a truth gang's members are in one predicted gang, and
             >=80% of that predicted gang are members of the truth gang.
* majority : >=50% overlap each way.
* person   : precision/recall over individual identity numbers.
Plus "score rank" quality: how many true gangs are in the top-k ranked gangs.
"""
from __future__ import annotations

import json

from .config import GROUND_TRUTH_PATH


def load_truth(path=GROUND_TRUTH_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _truth_map(truth: dict) -> dict[str, str]:
    return {m: g["gang_id"] for g in truth["gangs"] for m in g["members"]}


def evaluate(predicted: list[dict], truth: dict | None = None,
             threshold_ratio: float = 0.8) -> dict:
    truth = truth or load_truth()
    truth_map = _truth_map(truth)
    truth_sets = {g["gang_id"]: set(g["members"]) for g in truth["gangs"]}
    pred_sets = {g["gang_id"]: {m["id_no"] for m in g["members"]} for g in predicted}
    pred_members = set().union(*pred_sets.values()) if pred_sets else set()

    # ---------------------------------------------------------------- person level
    tp = sum(1 for pid in pred_members if pid in truth_map)
    fp = sum(1 for pid in pred_members if pid not in truth_map)
    fn = sum(1 for pid, gid in truth_map.items() if pid not in pred_members)
    person = _prf(tp, fp, fn)

    # ---------------------------------------------------------------- gang matching
    def match(ratio: float):
        matches = {}       # truth gid -> predicted gid
        used_pred = set()
        for gid, members in truth_sets.items():
            best, best_ratio = None, 0.0
            for pid, pmembers in pred_sets.items():
                if pid in used_pred:
                    continue
                overlap = len(members & pmembers)
                r_truth = overlap / max(1, len(members))
                r_pred = overlap / max(1, len(pmembers))
                if r_truth >= ratio and r_pred >= ratio and overlap > best_ratio:
                    best, best_ratio = pid, overlap
            if best:
                matches[gid] = best
                used_pred.add(best)
        return matches

    strict = match(threshold_ratio)
    majority = match(0.5)
    strict_tp = len(strict)
    majority_tp = len(majority)
    fp_gangs = len(pred_sets) - len(strict)
    fn_gangs = len(truth_sets) - strict_tp

    # ---------------------------------------------------------------- weak-only recall
    weak_members = {m for g in truth["gangs"] for m in g["weak_members"]}
    weak_caught = [m for m in weak_members if m in pred_members]
    weak_exact = [
        m for m in weak_members
        if m in pred_members and any(
            m in pred_sets[pred] for t_truth, pred in strict.items()
            if truth_map.get(m) == t_truth
        )
    ]

    # ---------------------------------------------------------------- rank quality
    topk = {}
    for k in (10, 20, 30, 45):
        top = predicted[:k]
        covered = set()
        for g in top:
            members = {m["id_no"] for m in g["members"]}
            best = max(truth_sets.items(), key=lambda kv: len(kv[1] & members))
            if len(best[1] & members) / max(1, len(best[1])) >= threshold_ratio:
                covered.add(best[0])
        topk[k] = len(covered)

    return {
        "gang_level": {
            "strict": {
                "precision": round(strict_tp / max(1, len(pred_sets)), 4),
                "recall": round(strict_tp / max(1, len(truth_sets)), 4),
                "f1": round(_f1(strict_tp / max(1, len(pred_sets)),
                                strict_tp / max(1, len(truth_sets))), 4),
                "tp": strict_tp, "fp": fp_gangs, "fn": fn_gangs,
                "predicted_gangs": len(pred_sets), "truth_gangs": len(truth_sets),
            },
            "majority": {
                "precision": round(majority_tp / max(1, len(pred_sets)), 4),
                "recall": round(majority_tp / max(1, len(truth_sets)), 4),
                "f1": round(_f1(majority_tp / max(1, len(pred_sets)),
                                majority_tp / max(1, len(truth_sets))), 4),
                "tp": majority_tp,
            },
        },
        "person_level": person,
        "weak_pendants": {
            "count": len(weak_members),
            "caught": len(weak_caught),
            "recall": round(len(weak_caught) / max(1, len(weak_members)), 4),
            "attached_to_correct_gang": len(weak_exact),
        },
        "rank_quality": {
            "top_k_truth_gangs_covered": topk,
            "mean_true_score": round(
                sum(g["score"] for g in predicted
                    if any(len(g["members"]) and
                           len(truth_sets[t] & {m["id_no"] for m in g["members"]})
                           / max(1, len(truth_sets[t])) >= threshold_ratio
                           for t in truth_sets)) / max(1, strict_tp), 2),
        },
        "false_positive_gangs": [
            {
                "gang_id": g["gang_id"], "score": g["score"],
                "size": len(g["members"]),
                "members": [m["name"] for m in g["members"]],
                "reasons": [r["text"] for r in g["reasons"]][:4],
            }
            for g in predicted
            if not any(g["gang_id"] == pid for pid in strict.values())
        ],
        "missed_gangs": [
            {
                "gang_id": gid, "style": next(
                    g["style"] for g in truth["gangs"] if g["gang_id"] == gid),
                "size": len(members),
                "members": [truth["persons_by_id"][m]["name"]
                            if "persons_by_id" in truth else m
                            for m in members][:8],
            }
            for gid, members in truth_sets.items() if gid not in strict
        ],
    }


def _prf(tp: int, fp: int, fn: int) -> dict:
    p = tp / max(1, tp + fp)
    r = tp / max(1, tp + fn)
    return {
        "precision": round(p, 4), "recall": round(r, 4), "f1": round(_f1(p, r), 4),
        "tp": tp, "fp": fp, "fn": fn,
    }


def _f1(p: float, r: float) -> float:
    return 2 * p * r / (p + r) if (p + r) else 0.0


def evaluate_from_db(analysis_id: int | None = None, truth_path=GROUND_TRUTH_PATH) -> dict:
    from . import db
    row = None
    if analysis_id:
        row = db.get_conn().execute(
            "SELECT * FROM analyses WHERE id=?", (analysis_id,)).fetchone()
    else:
        row = db.get_conn().execute(
            "SELECT * FROM analyses ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        raise RuntimeError("no analysis found — run an analysis first")
    analysis_id = row["id"]
    gangs = db.get_conn().execute(
        "SELECT * FROM gangs WHERE analysis_id=? ORDER BY score DESC",
        (analysis_id,)).fetchall()
    predicted = []
    for g in gangs:
        predicted.append({
            "gang_id": g["gang_id"], "score": g["score"],
            "members": json.loads(g["members_json"]),
            "reasons": json.loads(g["reasons_json"]),
        })
    truth = load_truth(truth_path)
    truth["persons_by_id"] = {
        p["id_no"]: p for p in truth.get("persons", [])
    }
    result = evaluate(predicted, truth)
    result["analysis_id"] = analysis_id
    return result
