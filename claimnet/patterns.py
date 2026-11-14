"""Pattern detectors and explainable gang risk scoring.

Every number the score uses is also returned as a human-readable reason, e.g.
"12 笔理赔集中在同一修理厂(占团伙 71%)，行业基线 18%".
"""
from __future__ import annotations

from collections import Counter, defaultdict
from statistics import mean

import networkx as nx

from . import config as C
from .graph import ClaimDoc, GraphBuilder, RELATION_LABELS


def _pct(x: float) -> str:
    return f"{x * 100:.0f}%"


class PatternDetector:
    def __init__(self, builder: GraphBuilder):
        self.b = builder
        self.claims = builder.claims
        self.claims_by_no = {c.claim_no: c for c in self.claims}

    # ------------------------------------------------------------ shop burst
    def shop_bursts(self) -> list[dict]:
        """A shop taking a cluster of claims from few people in a short window."""
        by_shop: dict[str, list[ClaimDoc]] = defaultdict(list)
        for c in self.claims:
            if c.shop_id:
                by_shop[c.shop_id].append(c)
        out = []
        for shop, docs in by_shop.items():
            docs.sort(key=lambda d: d.loss_date)
            # sliding 60-day windows
            for i, start in enumerate(docs):
                window = [d for d in docs[i:] if (d.loss_date - start.loss_date).days
                          <= C.SHOP_BURST_WINDOW_DAYS]
                if len(window) < C.SHOP_BURST_MIN_CLAIMS:
                    continue
                people = Counter()
                for d in window:
                    for pid in self.b.claim_persons.get(d.claim_no, ()):
                        people[pid] += 1
                gang_members = {p for p, n in people.items() if n >= C.SHOP_BURST_MEMBER_CLAIMS}
                if len(people) < C.SHOP_BURST_MIN_PERSONS:
                    continue
                if len(gang_members) < C.SHOP_BURST_MIN_REPEAT_CUSTOMERS:
                    continue
                out.append({
                    "shop_id": shop,
                    "shop_name": (self.b.book.shops.get(shop) or {}).get("name", shop),
                    "start": start.loss_date.isoformat(),
                    "end": max(d.loss_date for d in window).isoformat(),
                    "claim_count": len(window),
                    "claim_nos": [d.claim_no for d in window],
                    "people": len(people),
                    "repeat_customers": sorted(gang_members),
                    "payout": round(sum(d.payout for d in window), 2),
                })
                break  # one burst per shop is enough for the report
        out.sort(key=lambda x: (-x["claim_count"], x["shop_id"]))
        return out

    # ------------------------------------------------------------ collision ring
    def collision_rings(self) -> list[dict]:
        """Directed ring (or strong 2-cycle web) among people hitting each other."""
        directed = nx.DiGraph()
        pair_docs: dict[tuple[str, str], list[ClaimDoc]] = defaultdict(list)
        for c in self.claims:
            if c.driver_id and c.third_id:
                directed.add_edge(c.driver_id, c.third_id)
                pair_docs[(c.driver_id, c.third_id)].append(c)
        out = []
        cycles = list(nx.simple_cycles(directed))
        for cyc in cycles:
            if len(cyc) < 3:
                continue
            docs = []
            for a, b in zip(cyc, cyc[1:] + cyc[:1]):
                docs += pair_docs.get((a, b), [])
            if len(docs) < 3:
                continue
            out.append({
                "members": cyc,
                "claim_count": len(docs),
                "claim_nos": sorted(d.claim_no for d in docs),
                "payout": round(sum(d.payout for d in docs), 2),
                "start": min(d.loss_date for d in docs).isoformat(),
                "end": max(d.loss_date for d in docs).isoformat(),
            })
        # pure 2-cycles are mutual collisions; require at least 2 repeats
        for a, b in directed.edges():
            if a >= b or not directed.has_edge(b, a):
                continue
            docs = pair_docs.get((a, b), []) + pair_docs.get((b, a), [])
            if len(docs) < C.COLLISION_MIN_REPEATS:
                continue
            out.append({
                "members": [a, b],
                "claim_count": len(docs),
                "claim_nos": sorted(d.claim_no for d in docs),
                "payout": round(sum(d.payout for d in docs), 2),
                "start": min(d.loss_date for d in docs).isoformat(),
                "end": max(d.loss_date for d in docs).isoformat(),
            })
        out.sort(key=lambda x: (-x["claim_count"], -len(x["members"])))
        return out

    # ------------------------------------------------------------ identity sharing
    def identity_sharing(self, members: list[str]) -> dict:
        member_set = set(members)
        shared = {}
        for relation, attr in (("shared_phone", "phones"), ("shared_bank_card", "cards")):
            values: dict[str, set[str]] = defaultdict(set)
            for pid in member_set:
                for v in self.b.person_meta.get(pid, {}).get(attr, []):
                    values[v].add(pid)
            shared[relation] = {
                v: sorted(ids) for v, ids in values.items() if len(ids) > 1
            }
        return shared

    # ------------------------------------------------------------ per-gang features
    def gang_features(self, members: list[str], claims: list[str],
                      core: list[str], pendants: list[str],
                      weak_links: dict) -> tuple[dict, list[dict]]:
        member_set = set(members)
        docs = [self.claims_by_no[c] for c in claims if c in self.claims_by_no]
        reasons: list[dict] = []
        feats: dict[str, float] = {}

        # ---- identifier sharing
        ident = self.identity_sharing(members)
        people_with_shared = set()
        for rel, groups in ident.items():
            for ids in groups.values():
                people_with_shared |= set(ids)
        ident_ratio = len(people_with_shared) / max(1, len(members))
        feats["identifier_sharing"] = min(1.0, ident_ratio)
        if people_with_shared:
            detail = []
            for rel, groups in ident.items():
                if not groups:
                    continue
                biggest = max(groups.items(), key=lambda kv: len(kv[1]))
                detail.append(
                    f"{len(groups)} 个{RELATION_LABELS[rel]}被 {len(people_with_shared)} 人共用"
                    f"（最大一组 {len(biggest[1])} 人）"
                )
            reasons.append({
                "feature": "identifier_sharing", "value": feats["identifier_sharing"],
                "text": "；".join(detail) + f"，占团伙 {_pct(ident_ratio)}",
            })

        # ---- claim frequency
        per_person = Counter()
        for d in docs:
            for pid in self.b.claim_persons.get(d.claim_no, ()).intersection(member_set):
                per_person[pid] += 1
        avg_claims = mean(per_person.values()) if per_person else 0.0
        baseline = C.BASELINE_CLAIMS_PER_PERSON_YEAR * 2.8   # book spans ~2.8 years
        feats["claim_frequency"] = min(1.0, max(0.0, (avg_claims - baseline) / (6 * baseline)))
        if avg_claims > baseline * 1.25:
            reasons.append({
                "feature": "claim_frequency", "value": feats["claim_frequency"],
                "text": f"团伙人均出险 {avg_claims:.1f} 次，保单平均约 {baseline:.1f} 次/人·保单期",
            })

        # ---- shop concentration
        shop_counts = Counter(d.shop_id for d in docs if d.shop_id)
        if shop_counts:
            top_shop, top_n = shop_counts.most_common(1)[0]
            shop_ratio = top_n / max(1, sum(shop_counts.values()))
            # Herfindahl normalised
            hhi = sum((n / sum(shop_counts.values())) ** 2 for n in shop_counts.values())
            feats["shop_concentration"] = min(1.0, max(shop_ratio, hhi))
            if top_n >= 4 and shop_ratio >= 0.35:
                reasons.append({
                    "feature": "shop_concentration", "value": feats["shop_concentration"],
                    "text": f"{top_n} 笔理赔集中在「{self.b.book.shops.get(top_shop, {}).get('name', top_shop)}」"
                            f"（团伙理赔的 {_pct(shop_ratio)}）",
                })
        else:
            feats["shop_concentration"] = 0.0

        # ---- collision ring
        pairs = Counter()
        for d in docs:
            if d.driver_id and d.third_id and d.driver_id in member_set and d.third_id in member_set:
                pairs[(d.driver_id, d.third_id)] += 1
        mutual = 0
        mutual_docs = 0
        for (a, b), n in pairs.items():
            if (b, a) in pairs:
                mutual += 1
                mutual_docs += n + pairs[(b, a)]
        n = len(members)
        ring_ratio = (mutual / max(1, n * (n - 1))) if n > 1 else 0.0
        feats["collision_ring"] = min(1.0, ring_ratio * 3 + min(0.4, mutual_docs / 12))
        if mutual_docs:
            reasons.append({
                "feature": "collision_ring", "value": feats["collision_ring"],
                "text": f"{mutual} 组「A 撞 B 且 B 撞 A」的互碰关系，共 {mutual_docs} 笔理赔",
            })

        # ---- time burst
        feats["time_burst"] = self._burst_feature(docs)
        burst_count = self._best_window(docs)
        if burst_count >= 4:
            reasons.append({
                "feature": "time_burst", "value": feats["time_burst"],
                "text": f"{C.SHOP_BURST_WINDOW_DAYS} 天内集中出险 {burst_count} 笔",
            })

        # ---- payout
        book_mean = mean([c.payout for c in self.claims]) if self.claims else 0.0
        gang_mean = mean([d.payout for d in docs]) if docs else 0.0
        if book_mean > 0:
            feats["payout"] = min(1.0, max(0.0, (gang_mean / book_mean - 1) / 1.2))
            if gang_mean > book_mean * 1.15:
                reasons.append({
                    "feature": "payout", "value": feats["payout"],
                    "text": f"笔均赔付 {gang_mean:,.0f} 元，全量均值 {book_mean:,.0f} 元",
                })
        else:
            feats["payout"] = 0.0

        # ---- weak attachment
        feats["weak_attachment"] = min(1.0, len(pendants) / max(1, len(members)) * 2.5)
        if pendants:
            names = "、".join(
                self.b.person_meta[p]["name"] for p in pendants[:4]
            )
            reasons.append({
                "feature": "weak_attachment", "value": feats["weak_attachment"],
                "text": f"{len(pendants)} 名成员仅通过单条弱关联挂接核心（{names}）",
            })

        # ---- network density (core)
        if len(core) >= 2:
            core_set = set(core)
            edges = [
                (a, b) for (a, b) in self.b.edges
                if a in core_set and b in core_set
            ]
            density = len(edges) / (len(core) * (len(core) - 1) / 2)
            feats["network_density"] = min(1.0, density)
            if density > 0.5:
                reasons.append({
                    "feature": "network_density", "value": feats["network_density"],
                    "text": f"核心成员间关联密度 {_pct(density)}（{len(edges)} 条关系）",
                })
        else:
            feats["network_density"] = 0.0

        # ---- role swap
        roles = Counter()
        for d in docs:
            if d.driver_id in member_set:
                roles[(d.driver_id, "driver")] += 1
            if d.third_id in member_set:
                roles[(d.third_id, "third")] += 1
        both = {p for (p, r) in roles if (p, "driver") in roles and (p, "third") in roles}
        feats["role_swap"] = min(1.0, len(both) / max(1, len(members)))
        if both:
            reasons.append({
                "feature": "role_swap", "value": feats["role_swap"],
                "text": f"{len(both)} 名成员既作为被保险人又作为三者车出现，角色互换",
            })

        return feats, reasons

    def _best_window(self, docs: list[ClaimDoc]) -> int:
        if not docs:
            return 0
        docs = sorted(docs, key=lambda d: d.loss_date)
        best = 0
        j = 0
        for i in range(len(docs)):
            while j < len(docs) and (docs[j].loss_date - docs[i].loss_date).days <= C.SHOP_BURST_WINDOW_DAYS:
                j += 1
            best = max(best, j - i)
        return best

    def _burst_feature(self, docs: list[ClaimDoc]) -> float:
        best = self._best_window(docs)
        return min(1.0, max(0.0, (best - 3) / 8))


class RiskScorer:
    def __init__(self, weights: dict[str, float] | None = None):
        self.weights = dict(weights or C.BASE_FEATURE_WEIGHTS)

    def score(self, feats: dict[str, float], reasons: list[dict]) -> tuple[float, list[dict]]:
        total_w = sum(self.weights.values()) or 1.0
        score = sum(self.weights[k] * feats.get(k, 0.0) for k in self.weights) / total_w
        final = round(min(100.0, max(0.0, score * 100 * 1.15)), 1)
        # reason contribution in score points, sorted by impact
        enriched = []
        for r in reasons:
            w = self.weights.get(r["feature"], 0.0)
            points = round(w * r["value"] / total_w * 100 * 1.15, 1)
            enriched.append({
                **r,
                "label": C.FEATURE_LABELS.get(r["feature"], r["feature"]),
                "points": points,
            })
        enriched.sort(key=lambda r: -r["points"])
        return final, enriched
