"""Investigator feedback -> model calibration.

A confirmed gang raises the weight of the features that actually fired for it;
an excluded gang lowers them.  Every change is bounded (no feature can run away),
saved with an audit trail and reflected in a new model version.  Weights are
applied to the *next* analysis run, never retroactively.
"""
from __future__ import annotations

import copy
import json

from . import db
from .config import FEATURE_LABELS

LEARNING_RATE = 0.12          # per confirmation
MIN_WEIGHT = 0.01
MAX_WEIGHT = 0.30             # cap a single feature so feedback cannot collapse the model
# After this many pure confirmations without any exclusion, shrink each update so
# repeated "yes" on similar gangs does not ratchet every weight to the cap and
# turn the scorer into a yes-man.  Investigations that mix confirm+exclude (the
# normal case) keep the full learning rate.
DAMPING_AFTER = 3
DAMPING_FACTOR = 0.5
# And never drift further than this fraction away from the configured base.
MAX_DRIFT = 0.60


def current_model() -> dict:
    raw = db.meta_get("model_config")
    if raw:
        return json.loads(raw)
    from .config import BASE_FEATURE_WEIGHTS, AFFINITY_WEIGHTS, AFFINITY_WINDOWS
    return {
        "version": 1,
        "feature_weights": dict(BASE_FEATURE_WEIGHTS),
        "affinity_weights": dict(AFFINITY_WEIGHTS),
        "affinity_windows": dict(AFFINITY_WINDOWS),
        "threshold": 50.0,
        "history": [],
    }


def _normalise(weights: dict[str, float]) -> dict[str, float]:
    """Keep the vector summing to a constant so scores stay comparable."""
    target = 1.0
    total = sum(weights.values()) or 1.0
    scale = target / total
    return {k: round(max(MIN_WEIGHT, min(MAX_WEIGHT, v * scale)), 4)
            for k, v in weights.items()}


def apply_feedback(case_id: str, decision: str, note: str = "",
                   analyst: str = "investigator") -> dict:
    if decision not in ("confirm", "exclude"):
        raise ValueError("decision must be 'confirm' or 'exclude'")
    model = current_model()
    before = copy.deepcopy(model)
    case = db.get_conn().execute(
        "SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()
    if not case:
        raise KeyError(f"unknown case {case_id}")

    gang = db.get_conn().execute(
        "SELECT * FROM gangs WHERE case_id=? ORDER BY analysis_id DESC LIMIT 1",
        (case_id,)).fetchone()
    if not gang:
        raise KeyError(f"no gang linked to case {case_id}")

    reasons = json.loads(gang["reasons_json"])
    sign = 1.0 if decision == "confirm" else -1.0

    # damping: consecutive confirmations with no counter-evidence get weaker
    straight_confirms = 0
    for h in reversed(model["history"]):
        if h["decision"] == "confirm":
            straight_confirms += 1
        else:
            break
    rate = LEARNING_RATE
    if straight_confirms >= DAMPING_AFTER:
        rate *= DAMPING_FACTOR ** (straight_confirms - DAMPING_AFTER + 1)
        rate = max(rate, 0.01)

    from .config import BASE_FEATURE_WEIGHTS
    weights = dict(model["feature_weights"])
    changed = []
    for reason in reasons:
        feature = reason["feature"]
        if feature not in weights:
            continue
        # strong evidence for a confirmed gang grows; for an excluded gang shrinks
        delta = rate * sign * float(reason.get("value", 0.5))
        proposed = weights[feature] + delta
        # clamp against the configured base so feedback cannot drift indefinitely
        base = BASE_FEATURE_WEIGHTS.get(feature, weights[feature])
        lo = min(base * (1 - MAX_DRIFT), base)
        hi = max(base * (1 + MAX_DRIFT), base)
        new_value = max(MIN_WEIGHT, lo, min(MAX_WEIGHT, hi, proposed))
        if abs(new_value - weights[feature]) > 1e-9:
            changed.append({
                "feature": feature, "label": FEATURE_LABELS.get(feature, feature),
                "before": round(weights[feature], 4), "after": round(new_value, 4),
                "delta": round(new_value - weights[feature], 4),
            })
        weights[feature] = new_value

    model["feature_weights"] = _normalise(weights)
    model["version"] = before["version"] + 1
    model["history"].append({
        "case_id": case_id,
        "gang_id": gang["gang_id"],
        "decision": decision,
        "note": note,
        "analyst": analyst,
        "at": db.now_iso(),
        "from_version": before["version"],
        "to_version": model["version"],
        "changes": changed,
        "score_before": gang["score"],
    })
    db.meta_set("model_config", json.dumps(model, ensure_ascii=False))

    # mark the gang/case status
    new_status = "confirmed" if decision == "confirm" else "excluded"
    with db.tx() as c:
        c.execute("UPDATE gangs SET status=? WHERE analysis_id=? AND gang_id=?",
                  (new_status, gang["analysis_id"], gang["gang_id"]))
        c.execute("UPDATE cases SET status=?, updated_at=? WHERE case_id=?",
                  (new_status, db.now_iso(), case_id))
        c.execute(
            "INSERT INTO feedback(case_id,gang_id,analysis_id,decision,note,analyst,"
            "model_before,model_after,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                case_id, gang["gang_id"], gang["analysis_id"], decision, note, analyst,
                json.dumps(before, ensure_ascii=False),
                json.dumps(model, ensure_ascii=False), db.now_iso(),
            ),
        )
    return {
        "case_id": case_id, "decision": decision,
        "model_version": model["version"],
        "changes": changed,
        "weights": model["feature_weights"],
    }


def ensure_case(gang_id: str, analysis_id: int) -> str:
    """Bind a predicted gang to an investigation case.

    Cases are keyed by **membership**, not by gang_id: gang ids are re-assigned
    on every run, so a re-run of the same organisation must land in the same
    case file rather than spawning a duplicate.  We reuse any non-dissolved
    case from a previous run whose member set overlaps ≥60%.
    """
    conn = db.get_conn()
    row = conn.execute(
        "SELECT case_id FROM gangs WHERE analysis_id=? AND gang_id=?",
        (analysis_id, gang_id)).fetchone()
    if row and row["case_id"]:
        return row["case_id"]

    gang = conn.execute(
        "SELECT members_json FROM gangs WHERE analysis_id=? AND gang_id=?",
        (analysis_id, gang_id)).fetchone()
    members = {m["id_no"] for m in json.loads(gang["members_json"])} if gang else set()
    existing = conn.execute(
        "SELECT case_id, gang_id, analysis_id, members_json FROM gangs "
        "WHERE analysis_id < ? AND case_id IS NOT NULL "
        "ORDER BY analysis_id DESC", (analysis_id,)).fetchall()
    reuse = None
    for prev in existing:
        prev_members = {m["id_no"] for m in json.loads(prev["members_json"])}
        if not prev_members or not members:
            continue
        overlap = len(members & prev_members) / max(1, min(len(members), len(prev_members)))
        if overlap >= 0.6:
            reuse = prev["case_id"]
            break

    case_id = reuse or f"CASE-{analysis_id:04d}-{gang_id}"
    with db.tx() as c:
        c.execute(
            "INSERT OR IGNORE INTO cases(case_id,created_at,updated_at,status) "
            "VALUES(?,?,?,'open')",
            (case_id, db.now_iso(), db.now_iso()),
        )
        c.execute("UPDATE gangs SET case_id=? WHERE analysis_id=? AND gang_id=?",
                  (case_id, analysis_id, gang_id))
    return case_id


def model_summary() -> dict:
    model = current_model()
    return {
        "version": model["version"],
        "threshold": model["threshold"],
        "feature_weights": model["feature_weights"],
        "affinity_weights": model["affinity_weights"],
        "history_count": len(model["history"]),
        "recent_history": model["history"][-5:],
    }
