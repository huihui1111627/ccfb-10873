"""Orchestrates one full analysis run and persists its artefacts."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from . import config as C
from . import db
from .detect import GangSegmenter
from .graph import ClaimBook, GraphBuilder, RELATION_LABELS
from .patterns import PatternDetector, RiskScorer


def _book() -> ClaimBook:
    return ClaimBook(
        db.fetch_all("policies"), db.fetch_all("claims"),
        db.fetch_all("surveys"), db.fetch_all("repair_shops"),
    )


def _model() -> dict:
    raw = db.meta_get("model_config")
    if raw:
        return json.loads(raw)
    return {
        "version": 1,
        "feature_weights": dict(C.BASE_FEATURE_WEIGHTS),
        "affinity_weights": dict(C.AFFINITY_WEIGHTS),
        "affinity_windows": dict(C.AFFINITY_WINDOWS),
        "threshold": C.GANG_REPORT_THRESHOLD,
        "history": [],
    }


def save_model(model: dict) -> None:
    db.meta_set("model_config", json.dumps(model, ensure_ascii=False))


def latest_analysis() -> dict | None:
    row = db.get_conn().execute(
        "SELECT * FROM analyses ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row:
        return None
    out = dict(row)
    out["stats"] = json.loads(out.pop("stats_json"))
    gangs = db.get_conn().execute(
        "SELECT * FROM gangs WHERE analysis_id=? ORDER BY score DESC", (out["id"],)
    ).fetchall()
    out["gangs"] = [_gang_row(g) for g in gangs]
    return out


def _gang_row(row) -> dict:
    g = dict(row)
    for key in ("features_json", "reasons_json", "members_json", "evidence_json", "patterns_json"):
        g[key.replace("_json", "")] = json.loads(g.pop(key))
    return g


def run_analysis(window_days: int | None = None, persist: bool = True) -> dict:
    window = C.ANALYSIS_WINDOW_DAYS if window_days is None else window_days
    model = _model()
    started = datetime.now(timezone.utc)
    book = _book()
    builder = GraphBuilder(
        book, window_days=window,
        weights=model["affinity_weights"],
        windows=model["affinity_windows"],
    ).build()
    detector = PatternDetector(builder)
    scorer = RiskScorer(model["feature_weights"])
    bursts = detector.shop_bursts()
    rings = detector.collision_rings()
    segments = GangSegmenter(builder).segment()

    gangs = []
    for idx, seg in enumerate(segments):
        members = seg["members"]
        feats, reasons = detector.gang_features(
            members, seg["claims"], seg["core"], seg["pendants"], seg["weak_links"]
        )
        score, enriched = scorer.score(feats, reasons)
        if score < model["threshold"]:
            continue
        patterns = _gang_patterns(seg, bursts, rings)
        gangs.append({
            "gang_id": f"G{idx + 1:03d}",
            "score": score,
            "size": len(members),
            "core": seg["core"],
            "pendants": seg["pendants"],
            "features": feats,
            "reasons": enriched,
            "members": [
                {
                    "id_no": pid,
                    "name": builder.person_meta[pid]["name"],
                    "role": "core" if pid in seg["core"] else "pendant",
                    "claims": builder.person_meta[pid]["claims"],
                    "payout": builder.person_meta[pid]["payout"],
                    "weak_link": seg["weak_links"].get(pid),
                }
                for pid in members
            ],
            "evidence": _gang_evidence(builder, seg),
            "patterns": patterns,
            "claim_nos": seg["claims"],
        })
    gangs.sort(key=lambda g: (-g["score"], -g["size"]))
    for i, g in enumerate(gangs):
        g["gang_id"] = f"G{i + 1:03d}"

    stats = {
        "claims": len(book.claims),
        "persons": len(builder.person_meta),
        "person_edges": len(builder.edges),
        "hetero_nodes": builder.hetero.number_of_nodes(),
        "hetero_edges": builder.hetero.number_of_edges(),
        "candidates": len(segments),
        "gangs": len(gangs),
        "shop_bursts": len(bursts),
        "collision_rings": len(rings),
        "dropped_claims": book.dropped_claims[:20],
        "window_days": window,
        "duration_ms": int((datetime.now(timezone.utc) - started).total_seconds() * 1000),
    }
    analysis_id = None
    if persist:
        analysis_id = db.save_analysis({
            "created_at": db.now_iso(),
            "data_version": db.data_version(),
            "window_days": window,
            "model_version": model["version"],
            "stats": stats,
        })
        for g in gangs:
            db.save_gang(analysis_id, g)
        db.meta_set("last_analysis_id", str(analysis_id))
    return {
        "analysis_id": analysis_id,
        "stats": stats,
        "gangs": gangs,
        "model_version": model["version"],
    }


def _gang_patterns(seg: dict, bursts: list[dict], rings: list[dict]) -> list[dict]:
    members = set(seg["members"])
    claims = set(seg["claims"])
    out = []
    for burst in bursts:
        overlap = len(claims & set(burst["claim_nos"]))
        if overlap >= 2:
            out.append({
                "type": "shop_burst",
                "label": "修理厂集中出险",
                "detail": f"{burst['shop_name']} 在 {burst['start']}~{burst['end']} 内 "
                          f"{burst['claim_count']} 笔理赔，团伙占 {overlap} 笔",
                "shop_id": burst["shop_id"], "claim_nos": sorted(claims & set(burst["claim_nos"])),
            })
    for ring in rings:
        overlap = len(members & set(ring["members"]))
        if overlap < 2:
            continue
        # simple_cycles returns every rotation/partial ring of the same cluster;
        # keep only the maximal ring per overlapping member set.
        if any(out_ring["type"] == "collision_ring"
               and set(out_ring["members"]) <= set(ring["members"])
               for out_ring in out):
            continue
        out.append({
            "type": "collision_ring",
            "label": "三者车互碰",
            "detail": f"{overlap} 名成员形成互碰关系，{ring['claim_count']} 笔理赔 "
                      f"（{ring['start']}~{ring['end']}）",
            "members": ring["members"],
            "claim_nos": sorted(set(ring["claim_nos"]) & claims) or ring["claim_nos"],
        })
    # one consolidated ring entry is enough unless members are disjoint
    merged: list[dict] = []
    for p in out:
        if p["type"] == "collision_ring":
            same = next((m for m in merged if m["type"] == "collision_ring"
                         and set(m["members"]) & set(p["members"])), None)
            if same:
                same["members"] = sorted(set(same["members"]) | set(p["members"]))
                same["claim_nos"] = sorted(set(same["claim_nos"]) | set(p["claim_nos"]))
                same["detail"] = (f"{len(same['members'])} 名成员形成互碰关系，"
                                  f"{len(same['claim_nos'])} 笔理赔")
                continue
        merged.append(p)
    out = [p for p in merged]
    if seg["pendants"]:
        out.append({
            "type": "weak_pendant",
            "label": "弱关联挂接",
            "detail": f"{len(seg['pendants'])} 名成员仅通过一跳弱关联与核心相连",
            "members": seg["pendants"],
        })
    return out


def _gang_evidence(builder: GraphBuilder, seg: dict) -> list[dict]:
    members = set(seg["members"])
    out = []
    for (a, b), entry in builder.edges.items():
        if a not in members or b not in members:
            continue
        out.append({
            "a": a, "b": b,
            "a_name": builder.person_meta[a]["name"],
            "b_name": builder.person_meta[b]["name"],
            "weight": entry["weight"],
            "relations": [
                {"type": r, "label": RELATION_LABELS.get(r, r),
                 "strong": builder.weights[r] >= C.STRONG_TIE}
                for r in entry["relations"]
            ],
            "items": [
                {"relation": it["relation"],
                 "shared_value": it["shared_value"],
                 "dates": it["dates"], "count": it["count"]}
                for it in entry["evidence"]
            ],
        })
    out.sort(key=lambda e: -e["weight"])
    return out
