"""SQLite persistence for raw claim documents and analysis artefacts."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterable

from .config import DB_PATH

_SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS imports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    policy_count INTEGER NOT NULL DEFAULT 0,
    claim_count INTEGER NOT NULL DEFAULT 0,
    survey_count INTEGER NOT NULL DEFAULT 0,
    note TEXT
);

CREATE TABLE IF NOT EXISTS policies (
    policy_no TEXT PRIMARY KEY,
    insured_name TEXT,
    insured_id_no TEXT,
    phone TEXT,
    address TEXT,
    bank_card TEXT,
    plate TEXT,
    vin TEXT,
    vehicle_model TEXT,
    effective_date TEXT,
    expiry_date TEXT,
    premium REAL,
    import_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_policies_id ON policies(insured_id_no);

CREATE TABLE IF NOT EXISTS claims (
    claim_no TEXT PRIMARY KEY,
    policy_no TEXT,
    report_date TEXT,
    loss_date TEXT,
    accident_type TEXT,
    loss_amount REAL,
    payout REAL,
    status TEXT,
    driver_name TEXT,
    driver_id_no TEXT,
    third_party_plate TEXT,
    third_party_name TEXT,
    third_party_id_no TEXT,
    third_party_phone TEXT,
    third_party_vehicle_vin TEXT,
    repair_shop_id TEXT,
    survey_no TEXT,
    import_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_claims_policy ON claims(policy_no);
CREATE INDEX IF NOT EXISTS idx_claims_date ON claims(loss_date);
CREATE INDEX IF NOT EXISTS idx_claims_shop ON claims(repair_shop_id);

CREATE TABLE IF NOT EXISTS surveys (
    survey_no TEXT PRIMARY KEY,
    claim_no TEXT,
    survey_date TEXT,
    surveyor TEXT,
    scene_address TEXT,
    contact_phone TEXT,
    payout_card TEXT,
    repair_shop_id TEXT,
    statement TEXT,
    tow_company TEXT,
    import_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_surveys_claim ON surveys(claim_no);

CREATE TABLE IF NOT EXISTS repair_shops (
    shop_id TEXT PRIMARY KEY,
    name TEXT,
    address TEXT,
    risk_note TEXT,
    import_id INTEGER
);

CREATE TABLE IF NOT EXISTS analyses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    data_version TEXT NOT NULL,
    window_days INTEGER NOT NULL,
    model_version INTEGER NOT NULL,
    stats_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cases (
    case_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    note TEXT
);

CREATE TABLE IF NOT EXISTS gangs (
    analysis_id INTEGER NOT NULL,
    gang_id TEXT NOT NULL,
    case_id TEXT,
    score REAL NOT NULL,
    size INTEGER NOT NULL,
    features_json TEXT NOT NULL,
    reasons_json TEXT NOT NULL,
    members_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    patterns_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    PRIMARY KEY (analysis_id, gang_id)
);

CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id TEXT,
    gang_id TEXT,
    analysis_id INTEGER,
    decision TEXT NOT NULL,
    note TEXT,
    analyst TEXT,
    model_before TEXT,
    model_after TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    analysis_id INTEGER,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    severity TEXT NOT NULL,
    title TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    read INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

_local = threading.local()
_lock = threading.RLock()


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def get_conn() -> sqlite3.Connection:
    """One connection per thread.

    FastAPI runs sync endpoints in a worker pool, and a single sqlite3
    connection shared across threads raises ``InterfaceError: bad parameter or
    other API misuse`` under concurrent reads.  A thread-local connection (WAL
    mode) keeps concurrent reads safe without serialising the whole API.
    """
    conn = getattr(_local, "conn", None)
    if conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=15000")
        conn.executescript(_SCHEMA)
        conn.commit()
        _local.conn = conn
    return conn


@contextmanager
def tx():
    conn = get_conn()
    with _lock:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def reset_all() -> None:
    with _lock:
        conn = getattr(_local, "conn", None)
        if conn is not None:
            conn.close()
            _local.conn = None
        for suffix in ("", "-wal", "-shm"):
            p = DB_PATH.with_name(DB_PATH.name + suffix)
            if p.exists():
                p.unlink()
    get_conn()


def close_all() -> None:
    """Close this thread's connection (used by tests and shutdown hooks)."""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None


def meta_get(key: str, default: str | None = None) -> str | None:
    row = get_conn().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def meta_set(key: str, value: str) -> None:
    with tx() as c:
        c.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def bump_data_version() -> str:
    current = int(meta_get("data_version", "0") or 0) + 1
    meta_set("data_version", str(current))
    return str(current)


def data_version() -> str:
    return meta_get("data_version", "0") or "0"


_POLICY_COLS = [
    "policy_no", "insured_name", "insured_id_no", "phone", "address", "bank_card",
    "plate", "vin", "vehicle_model", "effective_date", "expiry_date", "premium",
]
_CLAIM_COLS = [
    "claim_no", "policy_no", "report_date", "loss_date", "accident_type",
    "loss_amount", "payout", "status", "driver_name", "driver_id_no",
    "third_party_plate", "third_party_name", "third_party_id_no",
    "third_party_phone", "third_party_vehicle_vin", "repair_shop_id", "survey_no",
]
_SURVEY_COLS = [
    "survey_no", "claim_no", "survey_date", "surveyor", "scene_address",
    "contact_phone", "payout_card", "repair_shop_id", "statement", "tow_company",
]
_SHOP_COLS = ["shop_id", "name", "address", "risk_note"]


def _clean(record: dict[str, Any], cols: list[str]) -> dict[str, Any]:
    out = {}
    for c in cols:
        v = record.get(c)
        if isinstance(v, str):
            v = v.strip()
        out[c] = v if v != "" else None
    return out


def _insert_many(table: str, cols: list[str], rows: Iterable[dict[str, Any]], import_id: int) -> int:
    rows = list(rows)
    if not rows:
        return 0
    placeholders = ",".join("?" * (len(cols) + 1))
    sql = f"INSERT OR REPLACE INTO {table}({','.join(cols)},import_id) VALUES({placeholders})"
    with tx() as c:
        c.executemany(sql, [tuple(r.get(col) for col in cols) + (import_id,) for r in rows])
    return len(rows)


def import_batch(
    name: str,
    policies: list[dict] | None = None,
    claims: list[dict] | None = None,
    surveys: list[dict] | None = None,
    shops: list[dict] | None = None,
    note: str | None = None,
) -> dict:
    policies = [_clean(r, _POLICY_COLS) for r in (policies or [])]
    claims = [_clean(r, _CLAIM_COLS) for r in (claims or [])]
    surveys = [_clean(r, _SURVEY_COLS) for r in (surveys or [])]
    shops = [_clean(r, _SHOP_COLS) for r in (shops or [])]
    created = now_iso()
    with tx() as c:
        cur = c.execute(
            "INSERT INTO imports(name,created_at,policy_count,claim_count,survey_count,note) "
            "VALUES(?,?,?,?,?,?)",
            (name, created, len(policies), len(claims), len(surveys), note),
        )
        import_id = int(cur.lastrowid)
    _insert_many("policies", _POLICY_COLS, policies, import_id)
    _insert_many("claims", _CLAIM_COLS, claims, import_id)
    _insert_many("surveys", _SURVEY_COLS, surveys, import_id)
    _insert_many("repair_shops", _SHOP_COLS, shops, import_id)
    version = bump_data_version()
    return {
        "import_id": import_id,
        "data_version": version,
        "policies": len(policies),
        "claims": len(claims),
        "surveys": len(surveys),
        "shops": len(shops),
    }


def fetch_all(table: str) -> list[dict]:
    assert table in {"policies", "claims", "surveys", "repair_shops", "imports"}
    rows = get_conn().execute(f"SELECT * FROM {table}").fetchall()
    return [dict(r) for r in rows]


def counts() -> dict:
    c = get_conn()
    out = {}
    for t in ("policies", "claims", "surveys", "repair_shops", "imports", "analyses"):
        out[t] = c.execute(f"SELECT COUNT(*) n FROM {t}").fetchone()["n"]
    out["data_version"] = data_version()
    out["persons_with_claims"] = c.execute(
        "SELECT COUNT(DISTINCT v) n FROM ("
        " SELECT insured_id_no v FROM policies WHERE insured_id_no IS NOT NULL"
        " UNION SELECT driver_id_no FROM claims WHERE driver_id_no IS NOT NULL"
        " UNION SELECT third_party_id_no FROM claims WHERE third_party_id_no IS NOT NULL)"
    ).fetchone()["n"]
    return out


def save_analysis(row: dict) -> int:
    with tx() as c:
        cur = c.execute(
            "INSERT INTO analyses(created_at,data_version,window_days,model_version,stats_json) "
            "VALUES(?,?,?,?,?)",
            (
                row["created_at"], row["data_version"], row["window_days"],
                row["model_version"], json.dumps(row["stats"], ensure_ascii=False),
            ),
        )
        return int(cur.lastrowid)


def save_gang(analysis_id: int, gang: dict) -> None:
    with tx() as c:
        c.execute(
            "INSERT OR REPLACE INTO gangs(analysis_id,gang_id,case_id,score,size,"
            "features_json,reasons_json,members_json,evidence_json,patterns_json,status) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                analysis_id, gang["gang_id"], gang.get("case_id"), gang["score"], gang["size"],
                json.dumps(gang["features"], ensure_ascii=False),
                json.dumps(gang["reasons"], ensure_ascii=False),
                json.dumps(gang["members"], ensure_ascii=False),
                json.dumps(gang["evidence"], ensure_ascii=False),
                json.dumps(gang.get("patterns", []), ensure_ascii=False),
                gang.get("status", "open"),
            ),
        )
