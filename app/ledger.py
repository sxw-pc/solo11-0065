"""SQLite 台账：保存运行、决策、检查、finding、指标和分层结果。"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

RUN_ID_RE = re.compile(r"^run-[A-Za-z0-9T._-]{8,80}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    generated_at TEXT NOT NULL,
    experiment_id TEXT NOT NULL,
    experiment_name TEXT NOT NULL,
    decision TEXT NOT NULL,
    decision_title TEXT NOT NULL,
    matched_rule TEXT NOT NULL,
    decision_reason TEXT NOT NULL,
    headline TEXT NOT NULL,
    primary_metric_key TEXT NOT NULL,
    primary_effect REAL,
    primary_p_value REAL,
    finding_count INTEGER NOT NULL,
    input_digest TEXT NOT NULL,
    config_digest TEXT NOT NULL,
    artifact_dir TEXT NOT NULL,
    decision_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    code TEXT NOT NULL,
    status TEXT NOT NULL,
    label TEXT NOT NULL,
    summary TEXT NOT NULL,
    details_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    code TEXT NOT NULL,
    severity TEXT NOT NULL,
    category TEXT NOT NULL,
    title TEXT NOT NULL,
    detail TEXT NOT NULL,
    evidence_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    role TEXT NOT NULL,
    metric_key TEXT NOT NULL,
    label TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'mean',
    estimable INTEGER NOT NULL DEFAULT 1,
    control_n INTEGER NOT NULL,
    treatment_n INTEGER NOT NULL,
    control_value REAL,
    treatment_value REAL,
    absolute_effect REAL,
    p_value REAL,
    significant INTEGER NOT NULL,
    favorable INTEGER NOT NULL,
    guardrail_status TEXT NOT NULL,
    control_numerator_sum REAL NOT NULL DEFAULT 0,
    control_denominator_sum REAL NOT NULL DEFAULT 0,
    treatment_numerator_sum REAL NOT NULL DEFAULT 0,
    treatment_denominator_sum REAL NOT NULL DEFAULT 0,
    control_zero_denominator_units INTEGER NOT NULL DEFAULT 0,
    treatment_zero_denominator_units INTEGER NOT NULL DEFAULT 0,
    control_effective_units INTEGER NOT NULL DEFAULT 0,
    treatment_effective_units INTEGER NOT NULL DEFAULT 0,
    details_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS strata (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    field TEXT NOT NULL,
    value TEXT NOT NULL,
    control_n INTEGER NOT NULL,
    treatment_n INTEGER NOT NULL,
    absolute_effect REAL,
    p_value REAL,
    eligible INTEGER NOT NULL,
    reversal INTEGER NOT NULL,
    details_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_created ON runs(generated_at DESC);
CREATE INDEX IF NOT EXISTS idx_findings_run ON findings(run_id, severity);
CREATE INDEX IF NOT EXISTS idx_metrics_run ON metrics(run_id);
CREATE INDEX IF NOT EXISTS idx_strata_run ON strata(run_id);
"""

# 旧版 metrics 表没有 ratio/可估计性字段，且关键列为 NOT NULL；
# 检测到旧结构时整表重建（台账数据可由 out/ 产物重新写入）。
def _migrate_metrics_table(connection: sqlite3.Connection) -> None:
    columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(metrics)").fetchall()
    }
    if not columns or "estimable" in columns:
        return
    connection.executescript(
        """
        ALTER TABLE metrics RENAME TO metrics_legacy;
        """
        + SCHEMA.split("CREATE TABLE IF NOT EXISTS strata")[0]
        .replace("CREATE TABLE IF NOT EXISTS metrics", "CREATE TABLE metrics")
        + """
        INSERT INTO metrics (
            run_id, role, metric_key, label, kind, estimable, control_n,
            treatment_n, control_value, treatment_value, absolute_effect,
            p_value, significant, favorable, guardrail_status, details_json
        )
        SELECT run_id, role, metric_key, label, 'mean', 1, control_n,
               treatment_n, control_value, treatment_value, absolute_effect,
               p_value, significant, favorable, guardrail_status, details_json
        FROM metrics_legacy;
        DROP TABLE metrics_legacy;
        """
    )


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_db(db_path: Path) -> None:
    with closing(connect(db_path)) as connection:
        connection.executescript(SCHEMA)
        _migrate_metrics_table(connection)
        connection.commit()


def save_run(db_path: Path, result: dict[str, Any], artifact_dir: str) -> None:
    run = result["run"]
    run_id = run["run_id"]
    if not RUN_ID_RE.match(run_id):
        raise ValueError("run_id 不符合台账约束")
    experiment = result["experiment"]["experiment"]
    primary = result["metrics"]["primary"]
    decision = result["decision"]
    init_db(db_path)
    with closing(connect(db_path)) as connection, connection:
        for table in ("checks", "findings", "metrics", "strata", "runs"):
            connection.execute(f"DELETE FROM {table} WHERE run_id = ?", (run_id,))
        connection.execute(
            """INSERT INTO runs (
                run_id, generated_at, experiment_id, experiment_name, decision,
                decision_title, matched_rule, decision_reason, headline,
                primary_metric_key, primary_effect, primary_p_value, finding_count,
                input_digest, config_digest, artifact_dir, decision_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id, run["generated_at"], experiment["id"], experiment["name"],
                decision["card"], decision["title"], decision["matched_rule"],
                decision["reason"], result["headline"], primary["key"],
                primary["absolute_effect"], primary["p_value"],
                len(result["findings"]), run["input_digest"], run["config_digest"],
                artifact_dir, _json(decision),
            ),
        )
        connection.executemany(
            "INSERT INTO checks (run_id, code, status, label, summary, details_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(run_id, item["code"], item["status"], item["label"],
              item["summary"], _json(item["details"])) for item in result["checks"]],
        )
        connection.executemany(
            "INSERT INTO findings (run_id, code, severity, category, title, detail, "
            "evidence_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(run_id, item["code"], item["severity"], item["category"],
              item["title"], item["detail"], _json(item["evidence"]))
             for item in result["findings"]],
        )
        metric_rows = [
            (run_id, item["role"], item["key"], item["label"],
             item.get("kind", "mean"), int(item.get("estimable", True)),
             item["control_n"], item["treatment_n"], item["control_value"],
             item["treatment_value"], item["absolute_effect"], item["p_value"],
             int(item["significant"]), int(item["favorable"]),
             item["guardrail_status"],
             item.get("control_numerator_sum", 0.0),
             item.get("control_denominator_sum", 0.0),
             item.get("treatment_numerator_sum", 0.0),
             item.get("treatment_denominator_sum", 0.0),
             item.get("control_zero_denominator_units", 0),
             item.get("treatment_zero_denominator_units", 0),
             item.get("control_effective_units", 0),
             item.get("treatment_effective_units", 0),
             _json(item))
            for item in [primary, *result["metrics"]["secondary"],
                         *result["metrics"]["guardrails"]]
        ]
        connection.executemany(
            "INSERT INTO metrics (run_id, role, metric_key, label, kind, estimable, "
            "control_n, treatment_n, control_value, treatment_value, absolute_effect, "
            "p_value, significant, favorable, guardrail_status, "
            "control_numerator_sum, control_denominator_sum, treatment_numerator_sum, "
            "treatment_denominator_sum, control_zero_denominator_units, "
            "treatment_zero_denominator_units, control_effective_units, "
            "treatment_effective_units, details_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            metric_rows,
        )
        stratum_rows = []
        for field in result["stratification"]["fields"]:
            for group in field["groups"]:
                stratum_rows.append(
                    (run_id, group["field"], group["value"], group["control_n"],
                     group["treatment_n"], group["absolute_effect"],
                     group["p_value"], int(group["eligible"]), int(group["reversal"]),
                     _json(group))
                )
        connection.executemany(
            "INSERT INTO strata (run_id, field, value, control_n, treatment_n, "
            "absolute_effect, p_value, eligible, reversal, details_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            stratum_rows,
        )


def list_runs(db_path: Path, limit: int = 50, decision: str | None = None) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    init_db(db_path)
    query = "SELECT * FROM runs"
    params: list[Any] = []
    if decision:
        query += " WHERE decision = ?"
        params.append(decision)
    query += " ORDER BY generated_at DESC, run_id DESC LIMIT ?"
    params.append(max(1, min(limit, 200)))
    with closing(connect(db_path)) as connection:
        return [dict(row) for row in connection.execute(query, params).fetchall()]


def get_run(db_path: Path, run_id: str) -> dict[str, Any] | None:
    if not RUN_ID_RE.match(run_id) or not db_path.exists():
        return None
    init_db(db_path)
    with closing(connect(db_path)) as connection:
        row = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        payload = dict(row)
        payload["decision"] = json.loads(payload.pop("decision_json"))
        payload["findings"] = [
            dict(item) for item in connection.execute(
                "SELECT code, severity, category, title, detail FROM findings "
                "WHERE run_id = ? ORDER BY severity, code", (run_id,)
            ).fetchall()
        ]
        return payload


def count_runs(db_path: Path) -> int:
    if not db_path.exists():
        return 0
    init_db(db_path)
    with closing(connect(db_path)) as connection:
        row = connection.execute("SELECT COUNT(*) AS total FROM runs").fetchone()
        return int(row["total"])
