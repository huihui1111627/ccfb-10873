"""Snapshot diffing: what changed between two analysis runs.

Gang membership churn is matched by *member overlap*, not gang_id, because ids
are re-assigned on every run.  A gang is considered the same organisation when
it shares >=50% of the smaller side's members.
"""
from __future__ import annotations

import json

from . import db


def _load(analysis_id: int) -> tuple[dict, list[dict]]:
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM analyses WHERE id=?", (analysis_id,)).fetchone()
    if not row:
        raise KeyError(f"analysis {analysis_id} not found")
    gangs = conn.execute(
        "SELECT * FROM gangs WHERE analysis_id=? ORDER BY score DESC", (analysis_id,)
    ).fetchall()
    out = []
    for g in gangs:
        out.append({
            "gang_id": g["gang_id"],
            "score": g["score"],
            "status": g["status"],
            "case_id": g["case_id"],
            "members": {
                m["id_no"]: m for m in json.loads(g["members_json"])
            },
            "reasons": json.loads(g["reasons_json"]),
        })
    return dict(row), out


def _match_gangs(old: list[dict], new: list[dict]) -> list[tuple[dict, dict, float]]:
    """Greedy best-overlap matching between two runs."""
    pairs = []
    candidates = []
    for i, og in enumerate(old):
        oset = set(og["members"])
        for j, ng in enumerate(new):
            nset = set(ng["members"])
            inter = len(oset & nset)
            if inter == 0:
                continue
            score = inter / max(1, min(len(oset), len(nset)))
            candidates.append((score, i, j, inter))
    candidates.sort(key=lambda x: (-x[0], -x[3]))
    matched_old, matched_new = {}, {}
    for score, i, j, inter in candidates:
        if score < 0.5:
            continue
        if i in matched_old or j in matched_new:
            continue
        matched_old[i] = j
        matched_new[j] = i
        pairs.append((old[i], new[j], score))
    return pairs, matched_old, matched_new


def compare(old_id: int, new_id: int) -> dict:
    old_row, old_gangs = _load(old_id)
    new_row, new_gangs = _load(new_id)
    pairs, matched_old, matched_new = _match_gangs(old_gangs, new_gangs)

    changes = []
    alerts = []
    for og, ng, similarity in pairs:
        old_members = set(og["members"])
        new_members = set(ng["members"])
        added = new_members - old_members
        removed = old_members - new_members
        score_delta = round(ng["score"] - og["score"], 1)
        changes.append({
            "type": "matched",
            "old_gang_id": og["gang_id"], "new_gang_id": ng["gang_id"],
            "similarity": round(similarity, 3),
            "old_score": og["score"], "new_score": ng["score"],
            "score_delta": score_delta,
            "added": [
                {"id_no": mid, **{k: v for k, v in ng["members"][mid].items()
                                  if k in ("name", "role", "claims", "payout")}}
                for mid in sorted(added)
            ],
            "removed": [
                {"id_no": mid, "name": og["members"][mid].get("name", mid)}
                for mid in sorted(removed)
            ],
            "old_size": len(old_members), "new_size": len(new_members),
        })
        if added:
            alerts.append({
                "kind": "gang_members_added",
                "severity": "high" if len(added) >= 2 or ng["score"] >= 70 else "medium",
                "title": f"团伙 {ng['gang_id']} 新增 {len(added)} 名成员",
                "detail": {
                    "gang_id": ng["gang_id"], "case_id": ng["case_id"],
                    "added": [{"id_no": m, "name": ng["members"][m]["name"]}
                              for m in sorted(added)],
                    "score": ng["score"], "score_delta": score_delta,
                },
            })
        if score_delta >= 10:
            alerts.append({
                "kind": "gang_score_up",
                "severity": "medium",
                "title": f"团伙 {ng['gang_id']} 风险分上升 {score_delta:+.1f}",
                "detail": {"old": og["score"], "new": ng["score"]},
            })

    for j, ng in enumerate(new_gangs):
        if j in matched_new:
            continue
        changes.append({
            "type": "new",
            "new_gang_id": ng["gang_id"], "new_score": ng["score"],
            "members": [{"id_no": mid, "name": m.get("name")}
                        for mid, m in ng["members"].items()],
            "new_size": len(ng["members"]),
        })
        alerts.append({
            "kind": "new_gang",
            "severity": "high" if ng["score"] >= 70 else "medium",
            "title": f"批次 #{new_id} 发现新团伙 {ng['gang_id']}（{len(ng['members'])} 人，{ng['score']} 分）",
            "detail": {"gang_id": ng["gang_id"], "case_id": ng["case_id"],
                       "members": [{"id_no": mid, "name": m.get("name")}
                                   for mid, m in ng["members"].items()]},
        })

    for i, og in enumerate(old_gangs):
        if i in matched_old:
            continue
        changes.append({
            "type": "dissolved",
            "old_gang_id": og["gang_id"], "old_score": og["score"],
            "old_size": len(og["members"]),
        })
        alerts.append({
            "kind": "gang_dissolved",
            "severity": "low",
            "title": f"批次 #{old_id} 的团伙 {og['gang_id']} 本次未复现",
            "detail": {"gang_id": og["gang_id"], "old_score": og["score"],
                       "old_size": len(og["members"])},
        })

    # person-level membership churn
    all_new = {mid for g in new_gangs for mid in g["members"]}
    all_old = {mid for g in old_gangs for mid in g["members"]}
    return {
        "old": {"analysis_id": old_id, "created_at": old_row["created_at"],
                "model_version": old_row["model_version"],
                "gangs": len(old_gangs), "members": len(all_old)},
        "new": {"analysis_id": new_id, "created_at": new_row["created_at"],
                "model_version": new_row["model_version"],
                "gangs": len(new_gangs), "members": len(all_new)},
        "summary": {
            "matched": len(pairs),
            "new_gangs": len(new_gangs) - len(matched_new),
            "dissolved_gangs": len(old_gangs) - len(matched_old),
            "members_added": len(all_new - all_old),
            "members_removed": len(all_old - all_new),
            "score_increases": sum(1 for c in changes
                                   if c["type"] == "matched" and c["score_delta"] > 0),
        },
        "changes": changes,
        "alerts": alerts,
    }


def list_analyses(limit: int = 30) -> list[dict]:
    rows = db.get_conn().execute(
        "SELECT id,created_at,data_version,window_days,model_version,stats_json "
        "FROM analyses ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        stats = json.loads(r["stats_json"])
        out.append({
            "analysis_id": r["id"], "created_at": r["created_at"],
            "data_version": r["data_version"], "window_days": r["window_days"],
            "model_version": r["model_version"], "gangs": stats.get("gangs"),
            "claims": stats.get("claims"), "persons": stats.get("persons"),
        })
    return out


def persist_alerts(comparison: dict, analysis_id: int) -> int:
    created = db.now_iso()
    import json as _json
    with db.tx() as c:
        for alert in comparison["alerts"]:
            c.execute(
                "INSERT INTO alerts(analysis_id,created_at,kind,severity,title,"
                "detail_json,read) VALUES(?,?,?,?,?,?,0)",
                (analysis_id, created, alert["kind"], alert["severity"],
                 alert["title"], _json.dumps(alert["detail"], ensure_ascii=False)),
            )
    return len(comparison["alerts"])
