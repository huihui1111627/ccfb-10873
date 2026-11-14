"""Entity / relation extraction from policy, claim and survey documents.

Two graphs are produced:

* ``hetero`` — the entity graph investigators browse (person, phone, bank card,
  address, vehicle, repair shop, claim, survey nodes and typed edges).
* ``person`` — a weighted person-affinity graph used for gang detection.  Every
  edge keeps the raw evidence (which phone/card/address/claim and when) so the
  detection layer and the UI can explain it.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import networkx as nx

from . import config as C


# --------------------------------------------------------------------- helpers
def norm(value: Any) -> str | None:
    if value is None:
        return None
    v = str(value).strip()
    return v or None


def parse_date(value: Any) -> date | None:
    if not value:
        return None
    text = str(value).strip()[:19]
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def key(kind: str, value: str) -> str:
    return f"{kind}:{value}"


def mask(value: str | None, keep: int = 4) -> str | None:
    if not value:
        return value
    if len(value) <= keep:
        return value
    return "*" * (len(value) - keep) + value[-keep:]


@dataclass
class PersonRecord:
    id_no: str
    names: list[str] = field(default_factory=list)
    phones: set[str] = field(default_factory=set)
    cards: set[str] = field(default_factory=set)
    addresses: set[str] = field(default_factory=set)
    policy_addresses: set[str] = field(default_factory=set)
    plates: set[str] = field(default_factory=set)
    claims: list[str] = field(default_factory=list)
    shops: set[str] = field(default_factory=set)
    first_claim: date | None = None
    last_claim: date | None = None
    payout_total: float = 0.0
    as_insured: int = 0
    as_driver: int = 0
    as_third: int = 0

    def display_name(self) -> str:
        return self.names[0] if self.names else self.id_no


@dataclass
class ClaimDoc:
    claim_no: str
    policy_no: str | None
    loss_date: date
    report_date: date | None
    accident_type: str | None
    loss_amount: float
    payout: float
    status: str | None
    shop_id: str | None
    survey_no: str | None
    owner_id: str | None
    driver_id: str | None
    third_id: str | None
    third_plate: str | None
    driver_plate: str | None
    driver_name: str | None
    third_name: str | None
    survey: dict | None = None


class ClaimBook:
    """Normalised in-memory view over the three raw document types."""

    def __init__(self, policies: list[dict], claims: list[dict], surveys: list[dict],
                 shops: list[dict]):
        self.policies = policies
        self.claims_raw = claims
        self.surveys_raw = surveys
        self.shops = {s["shop_id"]: s for s in shops}
        self.persons: dict[str, PersonRecord] = {}
        self.plates: dict[str, dict] = {}
        self.claims: list[ClaimDoc] = []
        self.dropped_claims: list[dict] = []
        self.policy_by_no: dict[str, dict] = {}
        self.survey_by_claim: dict[str, dict] = {}
        self._load()

    def _load(self):
        for p in self.policies:
            self.policy_by_no[norm(p.get("policy_no")) or ""] = p
        for s in self.surveys_raw:
            k = norm(s.get("claim_no"))
            if k:
                self.survey_by_claim[k] = s

        for p in self.policies:
            idno = norm(p.get("insured_id_no"))
            if not idno:
                continue
            rec = self._person(idno)
            self._add_name(rec, norm(p.get("insured_name")))
            if v := norm(p.get("phone")):
                rec.phones.add(v)
            if v := norm(p.get("bank_card")):
                rec.cards.add(v)
            if v := norm(p.get("address")):
                rec.addresses.add(v)
                rec.policy_addresses.add(v)
            if v := norm(p.get("plate")):
                rec.plates.add(v)
                pv = self.plates.setdefault(v, {
                    "plate": v, "vin": norm(p.get("vin")),
                    "model": norm(p.get("vehicle_model")),
                    "owners": set(), "policies": set(),
                })
                pv["owners"].add(idno)
                if pn := norm(p.get("policy_no")):
                    pv["policies"].add(pn)
                if not pv.get("vin") and norm(p.get("vin")):
                    pv["vin"] = norm(p.get("vin"))
                if not pv.get("model") and norm(p.get("vehicle_model")):
                    pv["model"] = norm(p.get("vehicle_model"))
            rec.as_insured += 1

        for raw in self.claims_raw:
            pol_no = norm(raw.get("policy_no"))
            policy = self.policy_by_no.get(pol_no or "")
            if not policy:
                self.dropped_claims.append(
                    {"claim_no": raw.get("claim_no"), "reason": "保单不存在"})
                continue
            loss = parse_date(raw.get("loss_date")) or parse_date(raw.get("report_date"))
            if loss is None:
                self.dropped_claims.append(
                    {"claim_no": raw.get("claim_no"), "reason": "出险日期无法解析"})
                continue
            owner_id = norm(policy.get("insured_id_no"))
            driver_id = norm(raw.get("driver_id_no")) or owner_id
            third_id = norm(raw.get("third_party_id_no"))
            if third_id == driver_id:
                third_id = None
            doc = ClaimDoc(
                claim_no=norm(raw.get("claim_no")) or "",
                policy_no=pol_no,
                loss_date=loss,
                report_date=parse_date(raw.get("report_date")),
                accident_type=norm(raw.get("accident_type")),
                loss_amount=float(raw.get("loss_amount") or 0.0),
                payout=float(raw.get("payout") or 0.0),
                status=norm(raw.get("status")),
                shop_id=norm(raw.get("repair_shop_id")),
                survey_no=norm(raw.get("survey_no")),
                owner_id=owner_id,
                driver_id=driver_id,
                third_id=third_id,
                third_plate=norm(raw.get("third_party_plate")),
                driver_plate=norm(policy.get("plate")),
                driver_name=norm(raw.get("driver_name")),
                third_name=norm(raw.get("third_party_name")),
                survey=self.survey_by_claim.get(doc_claim_no(raw)),
            )
            self.claims.append(doc)
            if driver_id:
                rec = self._person(driver_id)
                self._add_name(rec, doc.driver_name)
                rec.claims.append(doc.claim_no)
                rec.as_driver += 1
                if doc.shop_id:
                    rec.shops.add(doc.shop_id)
                if doc.driver_plate:
                    rec.plates.add(doc.driver_plate)
                self._claim_stats(rec, doc)
            if third_id:
                rec = self._person(third_id)
                self._add_name(rec, doc.third_name)
                rec.claims.append(doc.claim_no)
                rec.as_third += 1
                if doc.shop_id:
                    rec.shops.add(doc.shop_id)
                if doc.third_plate:
                    rec.plates.add(doc.third_plate)
                    pv = self.plates.setdefault(doc.third_plate, {
                        "plate": doc.third_plate,
                        "vin": norm(raw.get("third_party_vehicle_vin")),
                        "model": None, "owners": set(), "policies": set(),
                    })
                    pv["owners"].add(third_id)
                self._claim_stats(rec, doc)
            survey = doc.survey
            if survey:
                # The survey contact belongs to the claimant being surveyed (driver),
                # never to the third party: attributing it to both would smear one
                # gang phone across every innocent counterparty.
                if driver_id:
                    rec = self._person(driver_id)
                    if v := norm(survey.get("contact_phone")):
                        rec.phones.add(v)
                    if v := norm(survey.get("payout_card")):
                        rec.cards.add(v)
                    if v := norm(survey.get("scene_address")):
                        rec.addresses.add(v)   # kept, but not used as residential evidence
                    if v := norm(survey.get("repair_shop_id")):
                        rec.shops.add(v)

        for rec in self.persons.values():
            if not rec.names:
                rec.names.append(rec.id_no)

    def _person(self, idno: str) -> PersonRecord:
        rec = self.persons.get(idno)
        if rec is None:
            rec = PersonRecord(id_no=idno)
            self.persons[idno] = rec
        return rec

    @staticmethod
    def _add_name(rec: PersonRecord, name: str | None):
        if name and name not in rec.names:
            rec.names.append(name)

    @staticmethod
    def _claim_stats(rec: PersonRecord, doc: ClaimDoc):
        if rec.first_claim is None or doc.loss_date < rec.first_claim:
            rec.first_claim = doc.loss_date
        if rec.last_claim is None or doc.loss_date > rec.last_claim:
            rec.last_claim = doc.loss_date
        rec.payout_total += doc.payout

    def windowed_claims(self, window_days: int) -> list[ClaimDoc]:
        if window_days <= 0:
            return list(self.claims)
        latest = max((c.loss_date for c in self.claims), default=None)
        if latest is None:
            return []
        cutoff = latest - timedelta(days=window_days)
        return [c for c in self.claims if c.loss_date >= cutoff]


def doc_claim_no(raw: dict) -> str:
    return norm(raw.get("claim_no")) or ""


# --------------------------------------------------------------------- builder
RELATION_LABELS = {
    "shared_phone": "共用电话",
    "shared_bank_card": "共用银行卡",
    "shared_address": "同一住址",
    "co_claim": "同一事故",
    "co_repair_shop": "同一修理厂",
    "mutual_collision": "三者车互碰",
    "shared_driver": "同车驾驶",
}


class GraphBuilder:
    """Builds the hetero graph and the weighted person-affinity graph."""

    def __init__(self, book: ClaimBook, window_days: int = 0,
                 weights: dict[str, float] | None = None,
                 windows: dict[str, int] | None = None):
        self.book = book
        self.window_days = window_days
        self.claims = book.windowed_claims(window_days)
        self.weights = {**C.AFFINITY_WEIGHTS, **(weights or {})}
        self.windows = {**C.AFFINITY_WINDOWS, **(windows or {})}
        self.hetero = nx.MultiDiGraph()
        self.person_graph = nx.Graph()
        self.edges: dict[tuple[str, str], dict] = {}
        self.person_claims: dict[str, list[ClaimDoc]] = defaultdict(list)
        self.person_phones: dict[str, set[str]] = defaultdict(set)
        self.person_cards: dict[str, set[str]] = defaultdict(set)
        self.person_addrs: dict[str, set[str]] = defaultdict(set)
        self.person_shops: dict[str, set[str]] = defaultdict(set)
        self.claim_persons: dict[str, set[str]] = {}
        self.shop_people: dict[str, set[str]] = defaultdict(set)
        self.names: dict[str, str] = {}
        self.person_meta: dict[str, dict] = {}

    # ------------------------------------------------------------------ public
    def build(self) -> "GraphBuilder":
        self._index()
        self._build_affinity()
        self._build_hetero()
        return self

    # ------------------------------------------------------------------ indexing
    def _index(self):
        for c in self.claims:
            people = {p for p in (c.driver_id, c.third_id) if p}
            self.claim_persons[c.claim_no] = people
            if c.shop_id:
                self.shop_people[c.shop_id] |= people
            for pid in people:
                self.person_claims[pid].append(c)
                if c.shop_id:
                    self.person_shops[pid].add(c.shop_id)
        for pid, rec in self.book.persons.items():
            if pid not in self.person_claims:
                continue
            self.names[pid] = rec.display_name()
            self.person_phones[pid] = set(rec.phones)
            self.person_cards[pid] = set(rec.cards)
            self.person_addrs[pid] = set(rec.policy_addresses)
            self.person_meta[pid] = {
                "id_no": pid,
                "name": rec.display_name(),
                "claims": len(self.person_claims[pid]),
                "payout": round(sum(c.payout for c in self.person_claims[pid]), 2),
                "first_claim": min((c.loss_date for c in self.person_claims[pid]),
                                   default=None),
                "last_claim": max((c.loss_date for c in self.person_claims[pid]),
                                  default=None),
                "shops": sorted(self.person_shops[pid]),
                "plates": sorted(rec.plates),
                "as_insured": rec.as_insured,
                "as_driver": rec.as_driver,
                "as_third": rec.as_third,
                "addresses": sorted(self.person_addrs[pid]),
                "phones": sorted(self.person_phones[pid]),
                "cards": sorted(self.person_cards[pid]),
            }

    # ------------------------------------------------------------------ affinity
    def _activity(self, pid: str) -> tuple[date, date]:
        meta = self.person_meta[pid]
        return meta["first_claim"], meta["last_claim"]

    def _pair_in_window(self, a: str, b: str, window: int) -> tuple[bool, int]:
        """True when the two activity intervals overlap or are within *window* days."""
        a0, a1 = self._activity(a)
        b0, b1 = self._activity(b)
        gap_delta = max(a0, b0) - min(a1, b1)
        gap = max(0, gap_delta.days)
        return (window <= 0 or gap <= window), gap

    def _add_evidence(self, a: str, b: str, relation: str, value: str,
                      dates: list[date] | None = None, count: int | None = None,
                      meta: dict | None = None):
        pair = (a, b) if a <= b else (b, a)
        entry = {
            "relation": relation,
            "shared_value": value,
            "dates": sorted({d.isoformat() for d in (dates or []) if d})[-8:],
            "count": count if count is not None else len(dates or []),
        }
        if meta:
            entry.update(meta)
        self.edges.setdefault(pair, {"a": pair[0], "b": pair[1], "items": []})
        self.edges[pair]["items"].append(entry)

    def _build_affinity(self):
        people = sorted(self.person_meta)

        # ---- identifier groups (phone / card / address)
        for relation, attr in (("shared_phone", "phones"),
                               ("shared_bank_card", "cards"),
                               ("shared_address", "addresses")):
            groups: dict[str, list[str]] = defaultdict(list)
            for pid in people:
                for value in self.person_meta[pid][attr]:
                    groups[value].append(pid)
            window = self.windows[relation]
            for value, holders in groups.items():
                holders = sorted(set(holders))
                if len(holders) < 2:
                    continue
                for i in range(len(holders)):
                    for j in range(i + 1, len(holders)):
                        a, b = holders[i], holders[j]
                        ok, gap = self._pair_in_window(a, b, window)
                        if not ok:
                            continue
                        dates = [d for p in (a, b) for d in self._activity(p) if d]
                        self._add_evidence(a, b, relation, value, dates,
                                           meta={"gap_days": gap})

        # ---- co-claim
        for claim_no, people_on_claim in self.claim_persons.items():
            people_on_claim = sorted(people_on_claim)
            if len(people_on_claim) < 2:
                continue
            doc = self._claim_by_no(claim_no)
            for i in range(len(people_on_claim)):
                for j in range(i + 1, len(people_on_claim)):
                    self._add_evidence(people_on_claim[i], people_on_claim[j],
                                       "co_claim", claim_no,
                                       [doc.loss_date] if doc else [])

        # ---- mutual collision (A third-parties B's claim and vice versa)
        directed: dict[tuple[str, str], list[str]] = defaultdict(list)
        for c in self.claims:
            if c.driver_id and c.third_id:
                directed[(c.driver_id, c.third_id)].append(c.claim_no)
        for (a, b), claims_ab in directed.items():
            if a >= b:
                continue
            claims_ba = directed.get((b, a), [])
            if not claims_ba:
                continue
            docs = [c for c in self.claims if c.claim_no in set(claims_ab) | set(claims_ba)]
            dates = [d.loss_date for d in docs]
            self._add_evidence(a, b, "mutual_collision",
                               ",".join(sorted(set(claims_ab) | set(claims_ba))),
                               dates, count=len(docs))

        # ---- co repair shop within window
        shop_events: dict[tuple[str, str], list[date]] = defaultdict(list)
        for c in self.claims:
            if c.shop_id:
                for pid in self.claim_persons.get(c.claim_no, ()):
                    shop_events[(c.shop_id, pid)].append(c.loss_date)
        window = self.windows["co_repair_shop"]
        for shop, ppl in self.shop_people.items():
            ppl = sorted(ppl)
            if len(ppl) < 2:
                continue
            for i in range(len(ppl)):
                for j in range(i + 1, len(ppl)):
                    a, b = ppl[i], ppl[j]
                    da, db = shop_events[(shop, a)], shop_events[(shop, b)]
                    if window > 0 and min(abs((x - y).days) for x in da for y in db) > window:
                        continue
                    near = min(
                        ((x, y) for x in da for y in db), key=lambda xy: abs((xy[0] - xy[1]).days)
                    )
                    self._add_evidence(a, b, "co_repair_shop", shop,
                                       list(near), count=1)

        # ---- shared driver (same vehicle driven by different people)
        plate_drivers: dict[str, dict[str, list[date]]] = defaultdict(lambda: defaultdict(list))
        for c in self.claims:
            if c.driver_id and c.driver_plate:
                plate_drivers[c.driver_plate][c.driver_id].append(c.loss_date)
        for plate, drivers in plate_drivers.items():
            ids = sorted(drivers)
            if len(ids) < 2:
                continue
            for i in range(len(ids)):
                for j in range(i + 1, len(ids)):
                    a, b = ids[i], ids[j]
                    self._add_evidence(a, b, "shared_driver", plate,
                                       sorted(drivers[a] + drivers[b]))
        # owners of the same plate are also co-owners
        for plate, pv in self.book.plates.items():
            owners = sorted(o for o in pv.get("owners", ()) if o in self.person_meta)
            for i in range(len(owners)):
                for j in range(i + 1, len(owners)):
                    self._add_evidence(owners[i], owners[j], "shared_driver",
                                       f"{plate}(共同车主)", None, count=1,
                                       meta={"same_vehicle_owner": True})

        self._finalise_edges()

    def _claim_by_no(self, claim_no: str) -> ClaimDoc | None:
        if not hasattr(self, "_claim_index"):
            self._claim_index = {c.claim_no: c for c in self.claims}
        return self._claim_index.get(claim_no)

    def _finalise_edges(self):
        G = self.person_graph
        for pid in self.person_meta:
            meta = self.person_meta[pid]
            G.add_node(pid, name=meta["name"], claims=meta["claims"],
                       payout=meta["payout"],
                       first_claim=meta["first_claim"].isoformat(),
                       last_claim=meta["last_claim"].isoformat())

        for pair, entry in self.edges.items():
            items = entry["items"]
            by_relation: dict[str, list[dict]] = defaultdict(list)
            for it in items:
                by_relation[it["relation"]].append(it)
            total = 0.0
            relations = []
            for relation, rel_items in sorted(by_relation.items()):
                base = self.weights[relation]
                distinct = len({it["shared_value"] for it in rel_items})
                mult = min(C.MAX_REPEAT_MULTIPLIER,
                           1.0 + C.REPEAT_BONUS * max(0, distinct - 1))
                total += base * mult
                relations.append(relation)
            weight = round(min(total, 8.0), 3)
            # A "strong tie" must contain at least one intrinsically strong
            # relation. Summing several weak relations (e.g. co-claim +
            # co-repair-shop) must not promote two honest strangers into a gang core.
            strong = any(self.weights[r] >= C.STRONG_TIE for r in relations)
            dates = sorted({d for it in items for d in it["dates"]})
            G.add_edge(pair[0], pair[1], weight=weight, relations=relations,
                       strong=strong, evidence=items,
                       last_date=dates[-1] if dates else None,
                       last_ts=dates[-1] if dates else "")
            entry.update(weight=weight, relations=relations, strong=strong,
                         last_date=dates[-1] if dates else None,
                         evidence=items)

    # ------------------------------------------------------------------ hetero
    def _build_hetero(self):
        H = self.hetero
        active = sorted(self.person_meta)
        for pid in active:
            meta = self.person_meta[pid]
            H.add_node(key("person", pid), kind="person", label=meta["name"],
                       id_no=pid, claims=meta["claims"], payout=meta["payout"],
                       as_insured=meta["as_insured"], as_driver=meta["as_driver"],
                       as_third=meta["as_third"],
                       first_claim=meta["first_claim"].isoformat(),
                       last_claim=meta["last_claim"].isoformat(),
                       shops=meta["shops"], plates=meta["plates"])
            for phone in meta["phones"]:
                H.add_node(key("phone", phone), kind="phone", label=mask(phone, 4),
                           value=phone)
                H.add_edge(key("person", pid), key("phone", phone), key="has_phone",
                           relation="has_phone", weight=self.weights["shared_phone"])
            for card in meta["cards"]:
                H.add_node(key("bank_card", card), kind="bank_card",
                           label=mask(card, 4), value=card)
                H.add_edge(key("person", pid), key("bank_card", card), key="has_card",
                           relation="has_card", weight=self.weights["shared_bank_card"])
            for addr in meta["addresses"]:
                H.add_node(key("address", addr), kind="address", label=addr, value=addr)
                H.add_edge(key("person", pid), key("address", addr), key="has_address",
                           relation="has_address", weight=self.weights["shared_address"])
            for shop in meta["shops"]:
                s = self.book.shops.get(shop, {})
                H.add_node(key("repair_shop", shop), kind="repair_shop",
                           label=s.get("name") or shop, value=shop)
                # the dates matter: "used the same shop" is only a usable
                # relation inside a time window, so ego expansion filters on them
                shop_dates = sorted(
                    {c.loss_date.isoformat() for c in self.person_claims[pid]
                     if c.shop_id == shop}
                )
                H.add_edge(key("person", pid), key("repair_shop", shop),
                           key=f"used_shop:{shop}", relation="used_shop",
                           weight=self.weights["co_repair_shop"],
                           dates=shop_dates)

        for plate, pv in self.book.plates.items():
            if not any(c.driver_plate == plate or c.third_plate == plate
                       for c in self.claims):
                continue
            H.add_node(key("vehicle", plate), kind="vehicle", label=plate,
                       value=plate, vin=pv.get("vin"), model=pv.get("model"),
                       owners=sorted(pv.get("owners", ())))
            for owner in sorted(pv.get("owners", ())):
                if key("person", owner) in H:
                    H.add_edge(key("person", owner), key("vehicle", plate),
                               key=f"owns:{owner}", relation="owns", weight=0.9)

        for c in self.claims:
            cn = key("claim", c.claim_no)
            H.add_node(cn, kind="claim", label=c.claim_no, claim_no=c.claim_no,
                       loss_date=c.loss_date.isoformat(),
                       accident_type=c.accident_type, loss_amount=c.loss_amount,
                       payout=c.payout, status=c.status, shop_id=c.shop_id)
            if c.driver_id:
                H.add_edge(cn, key("person", c.driver_id), key="driver",
                           relation="claim_driver", weight=1.0)
            if c.driver_plate:
                H.add_edge(cn, key("vehicle", c.driver_plate),
                           key="subject_vehicle", relation="claim_subject_vehicle",
                           weight=1.0)
            if c.third_id:
                H.add_edge(cn, key("person", c.third_id), key="third_party",
                           relation="claim_third_party", weight=1.0)
            if c.third_plate:
                H.add_edge(cn, key("vehicle", c.third_plate),
                           key="third_vehicle", relation="claim_third_vehicle",
                           weight=1.0)
            if c.shop_id:
                s = self.book.shops.get(c.shop_id, {})
                H.add_node(key("repair_shop", c.shop_id), kind="repair_shop",
                           label=s.get("name") or c.shop_id, value=c.shop_id)
                H.add_edge(cn, key("repair_shop", c.shop_id), key="repaired_at",
                           relation="claim_shop", weight=0.2,
                           dates=[c.loss_date.isoformat()])
            if c.survey:
                sn = key("survey", c.survey_no or c.claim_no)
                H.add_node(sn, kind="survey", label=c.survey_no or c.claim_no,
                           surveyor=c.survey.get("surveyor"), claim_no=c.claim_no)
                H.add_edge(cn, sn, key="has_survey", relation="claim_has_survey",
                           weight=0.5)

        # prune dangling identifier nodes so the UI does not show noise
        for nid in list(H.nodes):
            kind_ = H.nodes[nid].get("kind")
            if kind_ in ("phone", "bank_card", "address") and H.degree(nid) < 2:
                H.remove_node(nid)

    # ------------------------------------------------------------------ export
    def hetero_payload(self, node_cap: int | None = None) -> dict:
        nodes = []
        for nid, data in self.hetero.nodes(data=True):
            node = {"id": nid, **data}
            if data.get("kind") == "person":
                node["label"] = data.get("label") or data.get("id_no")
            nodes.append(node)
            if node_cap and len(nodes) >= node_cap:
                break
        kept = {n["id"] for n in nodes}
        edges = []
        for u, v, k, data in self.hetero.edges(keys=True, data=True):
            if u in kept and v in kept:
                edges.append({"id": f"{u}|{k}|{v}", "source": u, "target": v, **data})
        return {"nodes": nodes, "edges": edges,
                "truncated": node_cap is not None and len(nodes) >= node_cap}

    def person_edge_payload(self) -> list[dict]:
        out = []
        for pair, entry in self.edges.items():
            out.append({
                "source": pair[0], "target": pair[1],
                "weight": entry["weight"], "relations": entry["relations"],
                "strong": entry["strong"], "last_date": entry["last_date"],
                "evidence": entry["evidence"],
            })
        return out
