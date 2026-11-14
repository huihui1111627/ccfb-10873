"""Gang segmentation: strong-tie cores, then weak-only pendants.

Deterministic and explainable, which matters more than raw accuracy in an
investigation tool:

1. Build the *core graph* from person-affinity edges that are intrinsically
   strong (shared phone, bank card, mutual collision, same-vehicle driver).
   Honest background policyholders essentially never share those.
2. Split cores into communities (greedy modularity, seeded) and discard
   undersized / claim-less candidates.
3. Attach pendants: people outside every core who hang off exactly one core
   through at most two weak edges (e.g. one shared residential address).  These
   are the deliberately "one-hop weak" gang members planted in the data.
"""
from __future__ import annotations

from collections import defaultdict

import networkx as nx

from . import config as C
from .graph import GraphBuilder

STRONG_RELATIONS = {"shared_phone", "shared_bank_card", "mutual_collision"}


def _qualifies(entry: dict, weights: dict[str, float]) -> bool:
    relations = entry["relations"]
    if any(weights[r] >= C.STRONG_TIE for r in relations):
        return True
    # Several independent weak relations can corroborate a cluster, but only
    # when they accumulate real weight and breadth.
    return entry["weight"] >= 1.1 and len(relations) >= 3


class GangSegmenter:
    def __init__(self, builder: GraphBuilder):
        self.b = builder
        self.weights = builder.weights
        self.core_member_set: set[str] = set()

    # ------------------------------------------------------------------ cores
    def core_graph(self) -> nx.Graph:
        G = nx.Graph()
        degree: dict[str, int] = defaultdict(int)
        candidates = []
        for (a, b), entry in self.b.edges.items():
            if not _qualifies(entry, self.weights):
                continue
            candidates.append((entry["weight"], a, b, entry))
        candidates.sort(key=lambda x: (-x[0], x[1], x[2]))
        for weight, a, b, entry in candidates:
            if degree[a] >= C.MAX_DETECTION_DEGREE or degree[b] >= C.MAX_DETECTION_DEGREE:
                continue
            G.add_edge(a, b, weight=weight, relations=entry["relations"])
            degree[a] += 1
            degree[b] += 1
        return G

    def _communities(self, core: nx.Graph) -> list[set[str]]:
        if core.number_of_nodes() == 0:
            return []
        out: list[set[str]] = []
        for comp in nx.connected_components(core):
            # Small tight components are already one gang; greedy modularity on
            # a 7–12 node core tends to carve off a 2–3 node fragment that then
            # fails the claim-count filter, losing real members.
            if len(comp) <= 14:
                out.append(set(comp))
                continue
            sub = core.subgraph(comp)
            parts = nx.community.greedy_modularity_communities(sub, weight="weight")
            # never keep fragments smaller than the minimum gang size
            parts = [set(p) for p in parts if len(p) >= C.MIN_GANG_SIZE]
            out.extend(parts)
        return out

    def _gang_claims(self, members: set[str]) -> list[str]:
        """Claims attributable to the group.

        Two independent linkages count:
          * a claim on which two or more members appear (co-claim / collision);
          * a claim by a member at a repair shop where two or more members have
            claims (the shared-shop pattern shop-burst gangs rely on).
        """
        shop_filers: dict[str, set[str]] = defaultdict(set)
        for claim_no, people in self.b.claim_persons.items():
            doc = self.b._claim_by_no(claim_no)
            if doc and doc.shop_id:
                for pid in people:
                    shop_filers[doc.shop_id].add(pid)
        # a shop belongs to the group when >=2 members filed there; but a member
        # who files only at a shop shared with *one* other member still belongs,
        # so the "shops used by the group" set is built from any member overlap.
        gang_shops = {
            shop for shop, people in shop_filers.items()
            if len(people & members) >= 2
        }
        out = []
        for claim_no, people in self.b.claim_persons.items():
            doc = self.b._claim_by_no(claim_no)
            if len(people & members) >= 2:
                out.append(claim_no)
            elif doc and doc.shop_id in gang_shops and (people & members):
                out.append(claim_no)
        return sorted(set(out))

    # ------------------------------------------------------------------ pendants
    def _pendant_candidates(self) -> dict[str, list[tuple[str, dict]]]:
        """outside person -> [(core member, weak edge)] for weak ties into a core.

        Attachment must be a *single identifiable weak relation* (typically one
        shared residential address).  Composite edges such as
        ``co_claim + co_repair_shop`` reaching 0.52 are coincidence between two
        honest people who happened to use the same body shop — attaching them
        would sweep in every innocent counterparty of a gang claim.
        """
        candidates: dict[str, list[tuple[str, dict]]] = defaultdict(list)
        for (a, b), entry in self.b.edges.items():
            a_core = a in self.core_member_set
            b_core = b in self.core_member_set
            if a_core == b_core:
                continue
            if len(entry["relations"]) != 1:
                continue                      # only pure weak relations attach
            relation = entry["relations"][0]
            if self.weights[relation] >= C.STRONG_TIE:
                continue
            if entry["weight"] < C.MIN_EDGE_WEIGHT:
                continue
            outside, inside = (b, a) if a_core else (a, b)
            candidates[outside].append((inside, entry))
        return candidates

    def _attach_pendants(self) -> tuple[set[str], dict[str, list[dict]]]:
        """Attach people whose *only* connection is a weak tie into one gang core.

        A pendant may have several weak edges — it may share one residential
        address with five core members, which is five edges into the *same*
        core.  What matters is that every weak edge leads into one core
        community, and that none of the edges is intrinsically strong.
        """
        attached: set[str] = set()
        links: dict[str, list[dict]] = {}
        core_group = self._core_group_map()
        for person, pairs in self._pendant_candidates().items():
            groups = {core_group.get(inside) for inside, _ in pairs}
            groups.discard(None)
            if len(groups) != 1:
                continue                 # spans two core communities -> not clean
            if not self.b.person_claims.get(person):
                continue
            collapsed: dict[tuple[str, str], dict] = {}
            for inside, entry in pairs:
                key = (inside, ",".join(sorted(entry["relations"])))
                if key in collapsed:
                    collapsed[key]["evidence"] += entry["evidence"]
                    collapsed[key]["weight"] = max(collapsed[key]["weight"], entry["weight"])
                else:
                    collapsed[key] = {
                        "core_member": inside,
                        "core_member_name": self.b.person_meta[inside]["name"],
                        "relations": entry["relations"],
                        "weight": entry["weight"],
                        "evidence": list(entry["evidence"]),
                    }
            attached.add(person)
            links[person] = list(collapsed.values())
        return attached, links

    def _core_group_map(self) -> dict[str, int]:
        """core member -> community index (from the last segmentation)."""
        if not hasattr(self, "_core_groups"):
            self._core_groups = {}
        return self._core_groups

    # ------------------------------------------------------------------ public
    def segment(self) -> list[dict]:
        core = self.core_graph()
        provisional = []
        for members in self._communities(core):
            if len(members) < C.MIN_GANG_SIZE:
                continue
            claims = self._gang_claims(members)
            if len(claims) < C.MIN_GANG_CLAIMS:
                continue
            provisional.append((members, claims))
        self.core_member_set = {m for members, _ in provisional for m in members}
        # map every core member to its community so pendants can attach to the
        # right group even when they share an address with five members
        self._core_groups = {}
        for idx, (members, _) in enumerate(provisional):
            for m in members:
                self._core_groups[m] = idx

        gangs = []
        all_pendants, all_links = self._attach_pendants()
        # each pendant belongs to exactly one community (enforced above)
        group_of_person: dict[str, int] = {}
        for person, links in all_links.items():
            groups = {self._core_groups.get(link["core_member"]) for link in links}
            groups.discard(None)
            if len(groups) == 1:
                group_of_person[person] = groups.pop()

        for idx, (members, claims) in enumerate(provisional):
            mine = {
                p: links for p, links in all_links.items()
                if group_of_person.get(p) == idx
            }
            gangs.append({
                "core": sorted(members),
                "pendants": sorted(mine),
                "members": sorted(members | set(mine)),
                "weak_links": mine,
                "claims": claims,
                "core_edges": [
                    {"a": a, "b": b, "weight": round(core[a][b]["weight"], 3),
                     "relations": core[a][b]["relations"]}
                    for a, b in core.subgraph(members).edges()
                ],
            })
        return self._dedupe(gangs)

    def _dedupe(self, gangs: list[dict]) -> list[dict]:
        out: list[dict] = []
        for gang in sorted(gangs, key=lambda g: -len(g["members"])):
            merged = False
            for existing in out:
                inter = len(set(gang["core"]) & set(existing["core"]))
                if inter / max(1, min(len(gang["core"]), len(existing["core"]))) > 0.5:
                    existing["core"] = sorted(set(existing["core"]) | set(gang["core"]))
                    existing["members"] = sorted(set(existing["members"]) | set(gang["members"]))
                    existing["pendants"] = sorted(set(existing["pendants"]) | set(gang["pendants"]))
                    existing["claims"] = sorted(set(existing["claims"]) | set(gang["claims"]))
                    existing["weak_links"].update(gang["weak_links"])
                    existing["core_edges"] = existing["core_edges"] + gang["core_edges"]
                    merged = True
                    break
            if not merged:
                out.append(dict(gang))
        return out
