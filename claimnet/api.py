"""FastAPI application: REST endpoints + static frontend."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import analysis, config as C, db, etl, feedback, report, snapshot
from .evaluate import evaluate_from_db
from .graph import ClaimBook, GraphBuilder, mask
from .patterns import PatternDetector

app = FastAPI(title="车险团伙欺诈挖掘平台 ClaimNet", version="1.0.0")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

# ---------------------------------------------------------------- in-process cache
_cache: dict[str, Any] = {"key": None, "book": None, "builder": None}


def _cache_key() -> str:
    model = db.meta_get("model_config") or ""
    return f"{db.data_version()}|{model}"


def _builder() -> GraphBuilder:
    key = _cache_key()
    if _cache["key"] != key:
        book, builder = report.load_builder()
        _cache.update(key=key, book=book, builder=builder)
    return _cache["builder"]


def _book() -> ClaimBook:
    _builder()
    return _cache["book"]


# ---------------------------------------------------------------- models
class ImportPayload(BaseModel):
    name: str = "batch"
    note: str | None = None
    policies: list[dict] = Field(default_factory=list)
    claims: list[dict] = Field(default_factory=list)
    surveys: list[dict] = Field(default_factory=list)
    shops: list[dict] = Field(default_factory=list)


class AnalysePayload(BaseModel):
    window_days: int | None = None
    persist: bool = True


class FeedbackPayload(BaseModel):
    case_id: str
    decision: str
    note: str = ""
    analyst: str = "investigator"


class WeightsPayload(BaseModel):
    affinity_weights: dict[str, float] | None = None
    feature_weights: dict[str, float] | None = None
    affinity_windows: dict[str, int] | None = None
    threshold: float | None = None


# ---------------------------------------------------------------- system
@app.get("/api/health")
def health():
    return {"ok": True, "db": str(C.DB_PATH), "counts": db.counts()}


@app.get("/api/overview")
def overview():
    counts = db.counts()
    latest = analysis.latest_analysis()
    model = feedback.current_model()
    alerts = db.get_conn().execute(
        "SELECT id,created_at,kind,severity,title,detail_json,read FROM alerts "
        "ORDER BY id DESC LIMIT 20").fetchall()
    return {
        "counts": counts,
        "model_version": model["version"],
        "threshold": model["threshold"],
        "feature_weights": model["feature_weights"],
        "latest_analysis": {
            "id": latest["id"], "created_at": latest["created_at"],
            "stats": latest["stats"],
            "gangs": [
                {"gang_id": g["gang_id"], "score": g["score"], "size": g["size"],
                 "case_id": g["case_id"], "status": g["status"],
                 "top_reasons": [r["text"] for r in g["reasons"][:2]]}
                for g in latest["gangs"]
            ],
        } if latest else None,
        "alerts": [
            {**dict(a), "detail": json.loads(a["detail_json"]),
             "detail_json": None}
            for a in alerts
        ],
        "analyses": snapshot.list_analyses(10),
    }


# ---------------------------------------------------------------- import
@app.get("/api/imports")
def list_imports():
    return db.fetch_all("imports")


@app.post("/api/import")
def import_documents(payload: ImportPayload):
    result = db.import_batch(
        payload.name, payload.policies, payload.claims, payload.surveys,
        payload.shops, payload.note,
    )
    _cache["key"] = None
    return result


@app.post("/api/import/raw")
def import_raw(reset: bool = False):
    result = etl.load_raw(reset=reset)
    _cache["key"] = None
    return result


@app.post("/api/reset")
def reset():
    db.reset_all()
    _cache["key"] = None
    return {"ok": True}


# ---------------------------------------------------------------- analysis
@app.post("/api/analyse")
def run_analysis(payload: AnalysePayload):
    result = analysis.run_analysis(payload.window_days, persist=payload.persist)
    if payload.persist:
        analysis_id = result["analysis_id"]
        for g in result["gangs"]:
            feedback.ensure_case(g["gang_id"], analysis_id)
        previous = db.get_conn().execute(
            "SELECT id FROM analyses WHERE id<? ORDER BY id DESC LIMIT 1",
            (analysis_id,)).fetchone()
        if previous:
            comp = snapshot.compare(previous["id"], analysis_id)
            snapshot.persist_alerts(comp, analysis_id)
            result["changes"] = comp
    _cache["key"] = None
    return result


@app.get("/api/analyses")
def analyses(limit: int = 30):
    return snapshot.list_analyses(limit)


@app.get("/api/analyses/{analysis_id}/gangs")
def analysis_gangs(analysis_id: int):
    row = db.get_conn().execute(
        "SELECT * FROM analyses WHERE id=?", (analysis_id,)).fetchone()
    if not row:
        raise HTTPException(404, "analysis not found")
    gangs = db.get_conn().execute(
        "SELECT * FROM gangs WHERE analysis_id=? ORDER BY score DESC",
        (analysis_id,)).fetchall()
    out = []
    for g in gangs:
        item = dict(g)
        for key in ("features_json", "reasons_json", "members_json",
                    "evidence_json", "patterns_json"):
            item[key.replace("_json", "")] = json.loads(item.pop(key))
        out.append(item)
    return {"analysis": {**dict(row), "stats": json.loads(row["stats_json"])},
            "gangs": out}


@app.get("/api/gangs")
def gangs(min_score: float = 0, status: str | None = None,
          analysis_id: int | None = None):
    if analysis_id is None:
        latest = db.get_conn().execute(
            "SELECT id FROM analyses ORDER BY id DESC LIMIT 1").fetchone()
        if not latest:
            return []
        analysis_id = latest["id"]
    rows = db.get_conn().execute(
        "SELECT * FROM gangs WHERE analysis_id=? AND score>=? ORDER BY score DESC",
        (analysis_id, min_score)).fetchall()
    out = []
    for g in rows:
        if status and g["status"] != status:
            continue
        item = dict(g)
        for key in ("features_json", "reasons_json", "members_json",
                    "evidence_json", "patterns_json"):
            item[key.replace("_json", "")] = json.loads(item.pop(key))
        out.append(item)
    return out


@app.get("/api/gangs/{gang_id}")
def gang_detail(gang_id: str, analysis_id: int | None = None):
    if analysis_id is None:
        latest = db.get_conn().execute(
            "SELECT id FROM analyses ORDER BY id DESC LIMIT 1").fetchone()
        analysis_id = latest["id"]
    row = db.get_conn().execute(
        "SELECT * FROM gangs WHERE analysis_id=? AND gang_id=?",
        (analysis_id, gang_id)).fetchone()
    if not row:
        raise HTTPException(404, "gang not found")
    item = dict(row)
    for key in ("features_json", "reasons_json", "members_json",
                "evidence_json", "patterns_json"):
        item[key.replace("_json", "")] = json.loads(item.pop(key))
    return item


# ---------------------------------------------------------------- graph / ego
@app.get("/api/graph")
def graph(limit: int = Query(1500, le=6000), types: str | None = None,
          focus: str = "risk"):
    """Whole-book graph.

    ``focus=risk`` (default) keeps only entities connected to a predicted gang
    or to a strong relation — the overview should answer "where are the rings",
    not plot every innocent policyholder.  ``focus=all`` returns everything.
    """
    b = _builder()
    wanted = set(types.split(",")) if types else None
    keep_ids: set[str] | None = None
    if focus != "all":
        keep_ids = set()
        gangs = db.get_conn().execute(
            "SELECT members_json FROM gangs WHERE analysis_id=("
            " SELECT MAX(id) FROM analyses)").fetchall()
        for row in gangs:
            for m in json.loads(row["members_json"]):
                keep_ids.add(f"person:{m['id_no']}")
        # focus=risk shows predicted-gang members only.  Honest people who
        # happen to share a phone (family landline) are reachable via ego
        # expansion and via focus=all; they must not pad the overview.
        # include the identifiers / vehicles / shops of the kept people
        expand = set(keep_ids)
        for nid in list(keep_ids):
            if nid not in b.hetero:
                continue
            for u, v in b.hetero.out_edges(nid):
                if b.hetero.nodes[v].get("kind") in ("claim", "survey"):
                    continue          # claim nodes would balloon the view
                expand.add(v)
            for u, v in b.hetero.in_edges(nid):
                expand.add(u)
        keep_ids = {n for n in expand if b.hetero.nodes[n].get("kind")
                    not in ("claim", "survey")}

    nodes = []
    for nid, data in b.hetero.nodes(data=True):
        if wanted and data.get("kind") not in wanted:
            continue
        if keep_ids is not None and nid not in keep_ids:
            continue
        nodes.append({"id": nid, **data})
        if len(nodes) >= limit:
            break
    kept = {n["id"] for n in nodes}
    edges = []
    for u, v, k, data in b.hetero.edges(keys=True, data=True):
        if u in kept and v in kept:
            edges.append({"id": f"{u}|{k}|{v}", "source": u, "target": v,
                          **{kk: vv for kk, vv in data.items()}})
    return {"nodes": nodes, "edges": edges, "truncated": len(nodes) >= limit,
            "focus": focus}


@app.get("/api/ego")
def ego(node: str, depth: int = 2, types: str | None = None,
        min_weight: float = 0.0, limit: int = C.EGO_MAX_NODES):
    types_list = types.split(",") if types else None
    return report.ego_network(node, depth=depth, node_types=types_list,
                              min_weight=min_weight, builder=_builder(),
                              max_nodes=limit)


@app.get("/api/person/{id_no}")
def person(id_no: str):
    b = _builder()
    meta = b.person_meta.get(id_no)
    if not meta:
        raise HTTPException(404, "person not found")
    claims = [
        {"claim_no": c.claim_no, "loss_date": c.loss_date.isoformat(),
         "accident_type": c.accident_type, "payout": c.payout,
         "role": "driver" if c.driver_id == id_no else "third_party",
         "shop_id": c.shop_id,
         "shop_name": (b.book.shops.get(c.shop_id) or {}).get("name")}
        for c in b.person_claims.get(id_no, [])
    ]
    edges = []
    for (a, bb), entry in b.edges.items():
        if id_no in (a, bb):
            other = bb if a == id_no else a
            edges.append({
                "other": other,
                "other_name": b.person_meta[other]["name"],
                "weight": entry["weight"], "relations": entry["relations"],
                "strong": entry["strong"],
                "evidence": entry["evidence"],
            })
    edges.sort(key=lambda e: -e["weight"])
    return {
        "person": {**meta, "phones": [mask(p, 4) for p in meta["phones"]],
                   "cards": [mask(c, 4) for c in meta["cards"]]},
        "claims": claims, "edges": edges[:80],
    }


@app.get("/api/shop/{shop_id}")
def shop(shop_id: str, window_days: int = 120):
    book = _book()
    info = book.shops.get(shop_id)
    if not info:
        raise HTTPException(404, "shop not found")
    docs = [c for c in book.claims if c.shop_id == shop_id]
    people: dict[str, int] = {}
    for c in docs:
        for p in (c.driver_id, c.third_id):
            if p:
                people[p] = people.get(p, 0) + 1
    det = PatternDetector(_builder())
    bursts = [b for b in det.shop_bursts() if b["shop_id"] == shop_id]
    return {
        "shop": info,
        "claim_count": len(docs),
        "payout": round(sum(c.payout for c in docs), 2),
        "people": len(people),
        "repeat_customers": sorted(
            ({"id_no": p, "name": _builder().person_meta.get(p, {}).get("name", p),
              "claims": n} for p, n in people.items() if n >= 2),
            key=lambda x: -x["claims"]),
        "bursts": bursts,
        "timeline": [{"claim_no": c.claim_no, "loss_date": c.loss_date.isoformat(),
                      "payout": c.payout} for c in
                     sorted(docs, key=lambda c: c.loss_date)],
    }


@app.get("/api/patterns/shops")
def shop_bursts():
    return PatternDetector(_builder()).shop_bursts()


@app.get("/api/patterns/collisions")
def collision_rings():
    return PatternDetector(_builder()).collision_rings()[:100]


# ---------------------------------------------------------------- cases
@app.get("/api/cases")
def cases():
    rows = db.get_conn().execute(
        "SELECT * FROM cases ORDER BY updated_at DESC").fetchall()
    out = []
    for r in rows:
        g = db.get_conn().execute(
            "SELECT gang_id,score,size,status,analysis_id FROM gangs "
            "WHERE case_id=? ORDER BY analysis_id DESC LIMIT 1",
            (r["case_id"],)).fetchone()
        out.append({**dict(r), "gang": dict(g) if g else None})
    return out


@app.post("/api/cases/{case_id}/feedback")
def case_feedback(case_id: str, payload: FeedbackPayload):
    try:
        result = feedback.apply_feedback(
            case_id, payload.decision, payload.note, payload.analyst)
    except KeyError as exc:
        raise HTTPException(404, str(exc))
    _cache["key"] = None
    return result


@app.get("/api/cases/{case_id}/report")
def case_report(case_id: str, download: bool = False, fmt: str = "json"):
    try:
        if download or fmt in ("md", "html"):
            saved = report.save_report(case_id)
            path = saved["html" if fmt == "html" else "markdown"]
            media = "text/html" if fmt == "html" else "text/markdown"
            return FileResponse(path, media_type=media,
                                filename=Path(path).name)
        return report.build_case_report(case_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc))


# ---------------------------------------------------------------- model / evaluation
@app.get("/api/model")
def model():
    return feedback.model_summary()


@app.post("/api/model/weights")
def update_weights(payload: WeightsPayload):
    model = feedback.current_model()
    if payload.affinity_weights:
        unknown = set(payload.affinity_weights) - set(C.AFFINITY_WEIGHTS)
        if unknown:
            raise HTTPException(400, f"unknown relations: {sorted(unknown)}")
        model["affinity_weights"].update(payload.affinity_weights)
    if payload.affinity_windows:
        model["affinity_windows"].update(payload.affinity_windows)
    if payload.feature_weights:
        unknown = set(payload.feature_weights) - set(C.BASE_FEATURE_WEIGHTS)
        if unknown:
            raise HTTPException(400, f"unknown features: {sorted(unknown)}")
        model["feature_weights"].update(payload.feature_weights)
    if payload.threshold is not None:
        model["threshold"] = payload.threshold
    model["version"] += 1
    feedback.save_model(model)
    _cache["key"] = None
    return feedback.model_summary()


@app.get("/api/evaluation")
def evaluation(analysis_id: int | None = None):
    try:
        return evaluate_from_db(analysis_id)
    except (RuntimeError, FileNotFoundError) as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/evaluation/truth")
def truth_summary():
    from .evaluate import load_truth
    try:
        truth = load_truth()
    except FileNotFoundError:
        raise HTTPException(404, "ground truth not generated")
    return {
        "gang_count": truth["gang_count"],
        "totals": truth["totals"],
        "styles": {},
        "g": [{"gang_id": g["gang_id"], "style": g["style"], "size": g["size"],
               "weak": len(g["weak_members"])} for g in truth["gangs"]],
        "decoys": [{"kind": d["kind"], "size": len(d["members"])}
                   for d in truth["decoys"]],
    }


@app.get("/api/snapshots/compare")
def compare(old: int, new: int):
    try:
        return snapshot.compare(old, new)
    except KeyError as exc:
        raise HTTPException(404, str(exc))


@app.get("/api/alerts")
def alerts(unread_only: bool = False):
    sql = "SELECT * FROM alerts ORDER BY id DESC LIMIT 100"
    rows = db.get_conn().execute(sql).fetchall()
    out = []
    for a in rows:
        if unread_only and a["read"]:
            continue
        item = dict(a)
        item["detail"] = json.loads(item.pop("detail_json"))
        out.append(item)
    return out


@app.post("/api/alerts/{alert_id}/read")
def read_alert(alert_id: int):
    with db.tx() as c:
        c.execute("UPDATE alerts SET read=1 WHERE id=?", (alert_id,))
    return {"ok": True}


# ---------------------------------------------------------------- pages
@app.get("/", response_class=HTMLResponse)
def index():
    return FileResponse(C.FRONTEND_DIR / "index.html")


@app.get("/report/{case_id}", response_class=HTMLResponse)
def report_page(case_id: str):
    saved = report.save_report(case_id)
    return FileResponse(saved["html"])


if C.FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=C.FRONTEND_DIR), name="static")
