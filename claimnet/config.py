"""Central configuration: affinity weights, time windows, scoring weights."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
RUNTIME_DIR = ROOT / "runtime"
REPORT_DIR = ROOT / "reports"
FRONTEND_DIR = ROOT / "frontend"
GROUND_TRUTH_PATH = DATA_DIR / "ground_truth.json"

DB_PATH = Path(os.environ.get("CLAIMNET_DB", RUNTIME_DIR / "claimnet.db"))

# ---------------------------------------------------------------- affinity edges
# Base weight for one co-occurrence of a relation, and the look-back window (days)
# used to decide whether two events count as "the same relation".
AFFINITY_WEIGHTS = {
    "shared_phone": 1.00,
    "shared_bank_card": 0.95,
    "shared_address": 0.25,
    "co_claim": 0.40,
    "co_repair_shop": 0.12,
    "mutual_collision": 0.85,
    "shared_driver": 0.55,
}

AFFINITY_WINDOWS = {   # days; 0 = unlimited
    "shared_phone": 730,
    "shared_bank_card": 730,
    "shared_address": 365,
    "co_claim": 365,
    "co_repair_shop": 120,
    "mutual_collision": 365,
    "shared_driver": 730,
}

# Repeating the same relation increases confidence, capped.
REPEAT_BONUS = 0.15
MAX_REPEAT_MULTIPLIER = 2.0

# Weight at which an edge alone can seed a gang core ("strong tie").
STRONG_TIE = 0.50
# Edges below this weight are ignored for gang detection.
MIN_EDGE_WEIGHT = 0.15
# A pendant may be attached to a core only through weak edges.
MAX_PENDANT_WEAK_EDGES = 2
# Degree cap per node in the detection graph; strongest edges are kept.
MAX_DETECTION_DEGREE = 40
# Minimum community size to be considered a gang candidate.
MIN_GANG_SIZE = 3
# Minimum number of claims involving at least two members.
MIN_GANG_CLAIMS = 2
# Risk score at/above which a candidate is reported as a predicted gang.
GANG_REPORT_THRESHOLD = 50.0

# Feature weights for risk scoring; feedback calibration scales these.
BASE_FEATURE_WEIGHTS = {
    "identifier_sharing": 0.20,
    "claim_frequency": 0.15,
    "shop_concentration": 0.15,
    "collision_ring": 0.14,
    "time_burst": 0.12,
    "payout": 0.10,
    "weak_attachment": 0.08,
    "network_density": 0.03,
    "role_swap": 0.03,
}
FEATURE_LABELS = {
    "identifier_sharing": "强弱身份关联共用",
    "claim_frequency": "出险频次异常",
    "shop_concentration": "修理厂集中出险",
    "collision_ring": "三者车互碰闭环",
    "time_burst": "短期集中出险",
    "payout": "赔付金额异常",
    "weak_attachment": "弱关联挂接成员",
    "network_density": "团伙关联密度",
    "role_swap": "角色互换",
}

# Pattern detectors
SHOP_BURST_WINDOW_DAYS = 60
SHOP_BURST_MIN_CLAIMS = 4
SHOP_BURST_MIN_PERSONS = 4
SHOP_BURST_MIN_REPEAT_CUSTOMERS = 3
SHOP_BURST_MEMBER_CLAIMS = 3
COLLISION_WINDOW_DAYS = 365
COLLISION_MIN_REPEATS = 2

# Analysis default look-back; 0 means use whole book.
ANALYSIS_WINDOW_DAYS = 0

# Ego-network API safety caps
EGO_MAX_DEPTH = 3
EGO_MAX_NODES = 1800
EGO_MAX_EDGES = 6000
# Max neighbours followed when expanding a hub node (shops, phone banks).
EGO_HUB_FANOUT = 40
# Time window used to filter time-stamped relations during ego expansion.
EGO_TIME_WINDOW_DAYS = 180
EGO_DEFAULT_TYPES = [
    "person", "phone", "bank_card", "address", "vehicle", "repair_shop",
]

BASELINE_CLAIMS_PER_PERSON_YEAR = 0.85
