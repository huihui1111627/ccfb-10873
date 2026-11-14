"""Ego-network extraction and evidence-chain report generation."""
from __future__ import annotations

import html
import json
from collections import deque
from datetime import date, datetime, timedelta

from . import config as C
from . import db
from .graph import ClaimBook, GraphBuilder


# ---------------------------------------------------------------- ego network
def load_builder(window_days: int = 0) -> tuple[ClaimBook, GraphBuilder]:
    book = ClaimBook(
        db.fetch_all("policies"), db.fetch_all("claims"),
        db.fetch_all("surveys"), db.fetch_all("repair_shops"),
    )
    # honour the calibrated affinity configuration
    raw = db.meta_get("model_config")
    model = json.loads(raw) if raw else {}
    builder = GraphBuilder(
        book, window_days=window_days,
        weights=model.get("affinity_weights"),
        windows=model.get("affinity_windows"),
    ).build()
    return book, builder


def ego_network(seed: str, depth: int = 1, node_types: list[str] | None = None,
                min_weight: float = 0.0, builder: GraphBuilder | None = None,
                max_nodes: int = C.EGO_MAX_NODES,
                window_days: int | None = None,
                fanout: int | None = None) -> dict:
    """BFS over the hetero graph from any node, capped for browser performance.

    Two safeguards keep the subgraph interpretable instead of a hairball:

    * **hub fan-out**: a body shop or a shared phone can touch hundreds of
      people.  Only the ``fanout`` most relevant neighbours of a hub are
      followed, ranked by edge weight then recency.
    * **time window**: time-stamped relations (shop visits) are filtered to the
      ``window_days`` leading up to the seed's latest activity, matching the
      same look-back used by the affinity model.

    ``depth`` is counted in entity hops; depth 1 on a person returns their
    identifiers / vehicles / shops, depth 2 also the people behind those.
    """
    depth = max(1, min(depth, C.EGO_MAX_DEPTH))
    fanout = fanout or C.EGO_HUB_FANOUT
    window = C.EGO_TIME_WINDOW_DAYS if window_days is None else window_days
    node_types = set(node_types or C.EGO_DEFAULT_TYPES)
    H = builder.hetero
    if seed not in H:
        for prefix in ("person", "vehicle", "phone", "bank_card", "address",
                       "repair_shop", "claim"):
            cand = f"{prefix}:{seed}"
            if cand in H:
                seed = cand
                break
        else:
            return {"error": f"node {seed} not found", "nodes": [], "edges": []}

    cutoff = _ego_cutoff(H, seed, window)
    keep_nodes: set[str] = {seed}
    keep_edges: list[tuple] = []
    frontier = deque([(seed, 0)])
    seen = {seed}
    truncated = False
    hubs_trimmed = 0
    while frontier:
        node, d = frontier.popleft()
        if d >= depth:
            continue
        neighbours = list(_neighbours(H, node))
        # rank hub neighbours: strong/weighted first, then most recent
        # weight desc, then recency desc (reverse-lexicographic date trick)
        neighbours.sort(key=lambda t: (
            -H.edges[t].get("weight", 1.0),
            _neg_date(_edge_recency(H, t)),
        ))
        followed = 0
        for u, v, k in neighbours:
            other = v if u == node else u
            kind = H.nodes[other].get("kind")
            if kind not in node_types:
                continue
            data = H.edges[u, v, k]
            weight = data.get("weight", 1.0)
            if weight < min_weight:
                continue
            if cutoff and not _edge_in_window(data, cutoff):
                continue
            if followed >= fanout:
                hubs_trimmed += 1
                break
            followed += 1
            keep_edges.append((u, v, k))
            if other not in seen:
                seen.add(other)
                keep_nodes.add(other)
                if len(keep_nodes) >= max_nodes:
                    truncated = True
                    frontier.clear()
                    break
                frontier.append((other, d + 1))
        if truncated:
            break

    payload = _subgraph_payload(H, keep_nodes, keep_edges, seed, truncated,
                                depth, node_types)
    payload["hubs_trimmed"] = hubs_trimmed
    payload["window_days"] = window
    return payload


def _edge_recency(H, edge_tuple) -> str:
    dates = H.edges[edge_tuple].get("dates") or []
    return max(dates) if dates else ""


def _neg_date(iso: str) -> int:
    """Sort key that puts newer dates first while staying an int."""
    if not iso:
        return 10 ** 9
    digits = int(iso[:10].replace("-", ""))
    return 99999999 - digits


def _ego_cutoff(H, seed: str, window_days: int) -> str | None:
    """Latest iso date around the seed minus the window; None means no filter."""
    if window_days <= 0:
        return None
    dates = []
    for t in _neighbours(H, seed):
        dates += H.edges[t].get("dates") or []
    node = H.nodes[seed]
    if node.get("last_claim"):
        dates.append(node["last_claim"])
    if not dates:
        return None
    latest = max(dates)
    y, m, dd = (int(x) for x in latest[:10].split("-"))
    return (date(y, m, dd) - timedelta(days=window_days)).isoformat()


def _edge_in_window(data: dict, cutoff: str) -> bool:
    dates = data.get("dates")
    if not dates:
        return True              # undated edges are identity links, always kept
    return max(dates) >= cutoff


def _neighbours(H, node):
    for _, v, k in H.out_edges(node, keys=True):
        yield node, v, k
    for u, _, k in H.in_edges(node, keys=True):
        yield u, node, k


def _subgraph_payload(H, nodes, edges, seed, truncated, depth, node_types):
    node_list = []
    for nid in nodes:
        data = dict(H.nodes[nid])
        data["id"] = nid
        node_list.append(data)
    edge_list = []
    seen = set()
    for u, v, k in edges:
        eid = f"{u}|{k}|{v}"
        if eid in seen or u not in nodes or v not in nodes:
            continue
        seen.add(eid)
        edge_list.append({"id": eid, "source": u, "target": v,
                          **dict(H.edges[u, v, k])})
    return {
        "seed": seed, "depth": depth, "truncated": truncated,
        "node_count": len(node_list), "edge_count": len(edge_list),
        "nodes": node_list, "edges": edge_list,
    }


# ---------------------------------------------------------------- report
def _fmt_money(x: float) -> str:
    return f"{x:,.0f}"


def build_case_report(case_id: str) -> dict:
    """Assemble the evidence chain for a case (JSON-friendly)."""
    conn = db.get_conn()
    case = conn.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()
    if not case:
        raise KeyError(f"case {case_id} not found")
    gang = conn.execute(
        "SELECT * FROM gangs WHERE case_id=? ORDER BY analysis_id DESC LIMIT 1",
        (case_id,)).fetchone()
    if not gang:
        raise KeyError(f"case {case_id} has no gang snapshot")
    analysis = conn.execute(
        "SELECT * FROM analyses WHERE id=?", (gang["analysis_id"],)).fetchone()
    members = json.loads(gang["members_json"])
    evidence = json.loads(gang["evidence_json"])
    reasons = json.loads(gang["reasons_json"])
    patterns = json.loads(gang["patterns_json"])
    features = json.loads(gang["features_json"])
    feedback = conn.execute(
        "SELECT * FROM feedback WHERE case_id=? ORDER BY id", (case_id,)).fetchall()

    book, builder = load_builder(analysis["window_days"])
    claims_nos = {c.claim_no for c in book.claims
                  if any(m in (c.driver_id, c.third_id) for m in
                         {x["id_no"] for x in members})}
    claims = []
    for c in book.claims:
        if c.claim_no not in claims_nos:
            continue
        claims.append({
            "claim_no": c.claim_no, "loss_date": c.loss_date.isoformat(),
            "accident_type": c.accident_type, "payout": c.payout,
            "shop_id": c.shop_id,
            "shop_name": (book.shops.get(c.shop_id) or {}).get("name"),
            "driver_name": c.driver_name, "driver_id_no": c.driver_id,
            "third_name": c.third_name, "third_id_no": c.third_id,
            "status": c.status,
        })
    claims.sort(key=lambda c: c["loss_date"])
    # the exact claim list belonging to this gang snapshot
    gang_claims = [c for c in claims
                   if c["claim_no"] in set(_gang_claim_nos(gang, book))]
    return {
        "case_id": case_id,
        "case_status": case["status"],
        "created_at": case["created_at"],
        "analysis_id": gang["analysis_id"],
        "analysis_created_at": analysis["created_at"],
        "gang_id": gang["gang_id"],
        "score": gang["score"],
        "status": gang["status"],
        "members": members,
        "features": features,
        "reasons": reasons,
        "patterns": patterns,
        "evidence": evidence,
        "claims": gang_claims or claims,
        "feedback": [dict(f) for f in feedback],
        "totals": {
            "members": len(members),
            "core": sum(1 for m in members if m.get("role") == "core"),
            "pendants": sum(1 for m in members if m.get("role") == "pendant"),
            "claims": len(gang_claims or claims),
            "payout": round(sum(c["payout"] for c in (gang_claims or claims)), 2),
        },
    }


def _gang_claim_nos(gang_row, book: ClaimBook) -> list[str]:
    # Recover the member set and recompute the claim set consistently.
    members = {m["id_no"] for m in json.loads(gang_row["members_json"])}
    shops: dict[str, set[str]] = {}
    filers = {}
    for c in book.claims:
        people = {p for p in (c.driver_id, c.third_id) if p}
        filers[c.claim_no] = people
        if c.shop_id and (people & members):
            shops.setdefault(c.shop_id, set()).update(people & members)
    gang_shops = {s for s, ppl in shops.items() if len(ppl) >= 2}
    out = []
    for c in book.claims:
        people = filers.get(c.claim_no, set())
        if len(people & members) >= 2:
            out.append(c.claim_no)
        elif c.shop_id in gang_shops and (people & members):
            out.append(c.claim_no)
    return out


def render_report_markdown(report: dict) -> str:
    lines = []
    lines.append(f"# 团伙调查证据链报告 — {report['case_id']}")
    lines.append("")
    lines.append(f"- 关联团伙：**{report['gang_id']}**")
    lines.append(f"- 风险评分：**{report['score']} / 100**")
    lines.append(f"- 案件状态：{report['status']}")
    lines.append(f"- 分析批次：#{report['analysis_id']}（{report['analysis_created_at']}）")
    lines.append(f"- 成员：{report['totals']['members']} 人"
                 f"（核心 {report['totals']['core']} / 弱挂接 {report['totals']['pendants']}）")
    lines.append(f"- 关联理赔：{report['totals']['claims']} 笔，"
                 f"赔付合计 {_fmt_money(report['totals']['payout'])} 元")
    lines.append("")
    lines.append("## 一、风险评分依据")
    for r in report["reasons"]:
        lines.append(f"- **{r.get('label', r['feature'])}**（+{r.get('points', 0)} 分）：{r['text']}")
    lines.append("")
    lines.append("## 二、识别到的作案模式")
    for p in report["patterns"]:
        lines.append(f"- **{p['label']}**：{p['detail']}")
    lines.append("")
    lines.append("## 三、成员清单")
    lines.append("| 姓名 | 角色 | 出险次数 | 赔付合计 | 身份证号 |")
    lines.append("|---|---|---|---|---|")
    for m in report["members"]:
        role = "核心" if m.get("role") == "core" else "弱关联挂接"
        lines.append(f"| {m.get('name')} | {role} | {m.get('claims', 0)} | "
                     f"{_fmt_money(m.get('payout', 0))} | {m['id_no']} |")
    lines.append("")
    lines.append("## 四、关系证据链")
    for e in report["evidence"][:40]:
        rels = "、".join(r["label"] for r in e["relations"])
        lines.append(f"- {e['a_name']} ↔ {e['b_name']}：{rels}（权重 {e['weight']}）")
        for it in e["items"][:3]:
            values = it.get("shared_value")
            if len(str(values)) > 80:
                values = str(values)[:77] + "..."
            dates = "、".join(it.get("dates", [])[-3:])
            lines.append(f"  - {it['relation']}：`{values}`"
                         + (f"，最近 {dates}" if dates else ""))
    lines.append("")
    lines.append("## 五、关联理赔明细")
    lines.append("| 赔案号 | 出险日期 | 类型 | 赔付 | 修理厂 | 驾驶员 | 三者 |")
    lines.append("|---|---|---|---|---|---|---|")
    for c in report["claims"]:
        lines.append(f"| {c['claim_no']} | {c['loss_date']} | {c.get('accident_type') or ''} | "
                     f"{_fmt_money(c['payout'])} | {c.get('shop_name') or ''} | "
                     f"{c.get('driver_name') or ''} | {c.get('third_name') or ''} |")
    lines.append("")
    if report["feedback"]:
        lines.append("## 六、调查处理记录")
        for f in report["feedback"]:
            lines.append(f"- {f['created_at']} · {f['analyst']} · "
                         f"{'确认' if f['decision'] == 'confirm' else '排除'}"
                         + (f" · {f['note']}" if f.get("note") else ""))
        lines.append("")
    lines.append("---")
    lines.append(f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    return "\n".join(lines)


def render_report_html(report: dict) -> str:
    md = render_report_markdown(report)
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>证据链报告 {html.escape(report['case_id'])}</title>
<style>
body{{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
max-width:980px;margin:32px auto;padding:0 20px;color:#1f2937;line-height:1.65;font-size:14px}}
h1{{font-size:22px;border-bottom:3px solid #dc2626;padding-bottom:10px}}
h2{{font-size:16px;margin-top:28px;color:#b91c1c}}
table{{border-collapse:collapse;width:100%;margin:10px 0;font-size:12.5px}}
th,td{{border:1px solid #e5e7eb;padding:6px 9px;text-align:left}}
th{{background:#f9fafb;font-weight:600}}
code{{background:#f3f4f6;padding:1px 5px;border-radius:3px;font-size:12px}}
.meta{{background:#fef2f2;border-left:4px solid #dc2626;padding:12px 16px;margin:16px 0}}
pre{{white-space:pre-wrap;font-family:inherit}}
</style></head><body>
<pre>{html.escape(md)}</pre>
</body></html>"""


def save_report(case_id: str) -> dict:
    report = build_case_report(case_id)
    md = render_report_markdown(report)
    out_dir = C.REPORT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / f"{case_id}.md"
    html_path = out_dir / f"{case_id}.html"
    md_path.write_text(md, encoding="utf-8")
    html_path.write_text(render_report_html(report), encoding="utf-8")
    return {"case_id": case_id, "markdown": str(md_path), "html": str(html_path),
            "report": report}
