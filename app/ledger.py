"""SQLite 台账：保存运行、决策、检查、finding、指标、分层与跨运行生命周期。"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from .lifecycle import (
    STATUS_INCOMPARABLE,
    LifecycleEntry,
    PriorFinding,
    RunSnapshot,
    classify_findings,
)

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
    primary_effect REAL NOT NULL,
    primary_p_value REAL NOT NULL,
    finding_count INTEGER NOT NULL,
    input_digest TEXT NOT NULL,
    config_digest TEXT NOT NULL,
    artifact_dir TEXT NOT NULL,
    decision_json TEXT NOT NULL,
    baseline_run_id TEXT,
    comparison_status TEXT NOT NULL DEFAULT 'no_baseline',
    skipped_run_count INTEGER NOT NULL DEFAULT 0
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
    experiment_id TEXT NOT NULL DEFAULT '',
    finding_key TEXT NOT NULL,
    evidence_summary TEXT NOT NULL,
    code TEXT NOT NULL,
    severity TEXT NOT NULL,
    category TEXT NOT NULL,
    title TEXT NOT NULL,
    detail TEXT NOT NULL,
    evidence_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS finding_lifecycles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    experiment_id TEXT NOT NULL,
    finding_key TEXT NOT NULL,
    status TEXT NOT NULL,
    baseline_run_id TEXT,
    previous_run_id TEXT,
    first_seen_run_id TEXT NOT NULL,
    prior_evidence_summary TEXT,
    evidence_changed INTEGER NOT NULL,
    reappeared INTEGER NOT NULL,
    cross_config_gap INTEGER NOT NULL DEFAULT 0,
    code TEXT NOT NULL,
    severity TEXT NOT NULL,
    title TEXT NOT NULL,
    evidence_summary TEXT NOT NULL,
    prior_code TEXT,
    prior_severity TEXT,
    prior_title TEXT,
    UNIQUE(run_id, finding_key)
);
CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    role TEXT NOT NULL,
    metric_key TEXT NOT NULL,
    label TEXT NOT NULL,
    control_n INTEGER NOT NULL,
    treatment_n INTEGER NOT NULL,
    control_value REAL NOT NULL,
    treatment_value REAL NOT NULL,
    absolute_effect REAL NOT NULL,
    p_value REAL NOT NULL,
    significant INTEGER NOT NULL,
    favorable INTEGER NOT NULL,
    guardrail_status TEXT NOT NULL,
    details_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS strata (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    field TEXT NOT NULL,
    value TEXT NOT NULL,
    control_n INTEGER NOT NULL,
    treatment_n INTEGER NOT NULL,
    absolute_effect REAL NOT NULL,
    p_value REAL NOT NULL,
    eligible INTEGER NOT NULL,
    reversal INTEGER NOT NULL,
    details_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_created ON runs(generated_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_experiment ON runs(experiment_id, generated_at, run_id);
CREATE INDEX IF NOT EXISTS idx_findings_run ON findings(run_id, severity);
CREATE INDEX IF NOT EXISTS idx_lifecycles_run ON finding_lifecycles(run_id);
CREATE INDEX IF NOT EXISTS idx_lifecycles_identity
    ON finding_lifecycles(experiment_id, finding_key);
CREATE INDEX IF NOT EXISTS idx_metrics_run ON metrics(run_id);
CREATE INDEX IF NOT EXISTS idx_strata_run ON strata(run_id);
"""

# 依赖迁移后列的索引，单独执行，避免在旧库 ALTER TABLE 之前引用新列。
_POST_MIGRATION_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_findings_identity "
    "ON findings(experiment_id, finding_key)",
)

# 旧库（v1）增量迁移：findings 历史行没有稳定身份，保持可读并标记无比较基准。
_LEGACY_COLUMNS = {
    "runs": {
        "baseline_run_id": "TEXT",
        "comparison_status": "TEXT NOT NULL DEFAULT 'no_baseline'",
        "skipped_run_count": "INTEGER NOT NULL DEFAULT 0",
    },
    "findings": {
        "experiment_id": "TEXT NOT NULL DEFAULT ''",
        "finding_key": "TEXT NOT NULL DEFAULT ''",
        "evidence_summary": "TEXT NOT NULL DEFAULT ''",
    },
}

_BUSY_RETRIES = 5
_BUSY_DELAY = 0.1


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _existing_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}


def init_db(db_path: Path) -> None:
    with closing(connect(db_path)) as connection:
        connection.executescript(SCHEMA)
        for table, columns in _LEGACY_COLUMNS.items():
            existing = _existing_columns(connection, table)
            for name, declaration in columns.items():
                if name not in existing:
                    connection.execute(
                        f"ALTER TABLE {table} ADD COLUMN {name} {declaration}"
                    )
        for statement in _POST_MIGRATION_INDEXES:
            connection.execute(statement)
        connection.commit()


def _open_immediate(connection: sqlite3.Connection) -> None:
    """获取写锁后再选基准，避免同 experiment 并发审计互相把对方当基准。"""
    connection.execute("BEGIN IMMEDIATE")


def _prior_runs(
    connection: sqlite3.Connection,
    experiment_id: str,
    generated_at: str,
    run_id: str,
) -> list[sqlite3.Row]:
    """同 experiment 严格早于当前 run 的成功 run，时间升序。

    相同 generated_at 用 run_id 做确定次序，避免同时间戳下基准选择不稳定。
    """
    return connection.execute(
        """SELECT run_id, generated_at, config_digest FROM runs
           WHERE experiment_id = ?
             AND (generated_at < ? OR (generated_at = ? AND run_id < ?))
           ORDER BY generated_at ASC, run_id ASC""",
        (experiment_id, generated_at, generated_at, run_id),
    ).fetchall()


def _findings_for_runs(
    connection: sqlite3.Connection, run_ids: list[str]
) -> dict[str, list[PriorFinding]]:
    if not run_ids:
        return {}
    placeholders = ",".join("?" for _ in run_ids)
    grouped: dict[str, list[PriorFinding]] = {run_id: [] for run_id in run_ids}
    rows = connection.execute(
        f"""SELECT run_id, finding_key, code, severity, title, evidence_summary
            FROM findings WHERE run_id IN ({placeholders})
            AND finding_key != ''""",
        run_ids,
    ).fetchall()
    for row in rows:
        grouped[row["run_id"]].append(
            PriorFinding(
                finding_key=row["finding_key"],
                code=row["code"],
                severity=row["severity"],
                title=row["title"],
                evidence_summary=row["evidence_summary"],
            )
        )
    return grouped


def _first_seen(
    connection: sqlite3.Connection,
    experiment_id: str,
    config_digest: str,
    current_run_id: str,
) -> dict[str, str]:
    """同 experiment、同 config_digest 历史中每个 finding_key 最早出现的 run。"""
    rows = connection.execute(
        """SELECT f.finding_key AS finding_key, f.run_id AS run_id
           FROM findings f JOIN runs r ON r.run_id = f.run_id
           WHERE r.experiment_id = ? AND r.config_digest = ?
             AND f.finding_key != '' AND r.run_id != ?
           ORDER BY r.generated_at ASC, r.run_id ASC""",
        (experiment_id, config_digest, current_run_id),
    ).fetchall()
    first_seen: dict[str, str] = {}
    for row in rows:
        first_seen.setdefault(row["finding_key"], row["run_id"])
    return first_seen


def _build_comparison(
    connection: sqlite3.Connection,
    run: dict[str, Any],
    current_findings: list[dict[str, Any]],
) -> dict[str, Any]:
    experiment_id = run["experiment_id"]
    prior_rows = _prior_runs(
        connection, experiment_id, run["generated_at"], run["run_id"]
    )
    immediate = prior_rows[-1] if prior_rows else None
    same_config = [
        RunSnapshot(row["run_id"], row["generated_at"], row["config_digest"])
        for row in prior_rows
        if row["config_digest"] == run["config_digest"]
    ]
    prior_by_run = _findings_for_runs(
        connection, [snapshot.run_id for snapshot in same_config]
    )
    first_seen = _first_seen(
        connection, experiment_id, run["config_digest"], run["run_id"]
    )
    cross_config_gap = bool(
        same_config
        and len(prior_rows) - len(same_config) > 0
        and prior_rows[-1]["run_id"] != same_config[-1].run_id
    )
    lifecycle = classify_findings(
        RunSnapshot(run["run_id"], run["generated_at"], run["config_digest"]),
        current_findings,
        same_config,
        prior_by_run,
        first_seen,
        cross_config_gap=cross_config_gap,
    )
    if same_config:
        baseline_run_id = same_config[-1].run_id
        comparison_status = "comparable"
    elif immediate is None:
        baseline_run_id = None
        comparison_status = "no_baseline"
    else:
        # 有历史 run 但配置 digest 全部不同：策略要求沿同配置找基准，找不到。
        baseline_run_id = None
        comparison_status = STATUS_INCOMPARABLE
    skipped = (
        len(prior_rows) - len(same_config)
        if comparison_status == "comparable"
        else len(prior_rows)
    )
    return {
        "lifecycle": lifecycle,
        "baseline_run_id": baseline_run_id,
        "comparison_status": comparison_status,
        "skipped_run_count": skipped,
        "immediate_run_id": immediate["run_id"] if immediate else None,
    }


def _entry_to_row(
    entry: LifecycleEntry,
    finding: dict[str, Any] | None,
    run_id: str,
    experiment_id: str,
) -> tuple[Any, ...]:
    if finding is None:
        # resolved：当前 run 不再产生该 finding，身份信息取自基准 run 的记录
        finding = {
            "finding_key": entry.finding_key,
            "code": entry.prior_finding.code if entry.prior_finding else "",
            "severity": entry.prior_finding.severity if entry.prior_finding else "",
            "title": entry.prior_finding.title if entry.prior_finding else "",
            "evidence_summary": entry.prior_evidence_summary or "",
        }
    prior = entry.prior_finding
    return (
        run_id, experiment_id, entry.finding_key, entry.status,
        entry.baseline_run_id, entry.previous_run_id, entry.first_seen_run_id,
        entry.prior_evidence_summary, int(entry.evidence_changed),
        int(entry.reappeared), int(entry.cross_config_gap), finding["code"],
        finding["severity"], finding["title"], finding["evidence_summary"],
        prior.code if prior else None,
        prior.severity if prior else None,
        prior.title if prior else None,
    )


def save_run(db_path: Path, result: dict[str, Any], artifact_dir: str) -> dict[str, Any]:
    """同一事务写入 run、findings 与生命周期关系，返回生命周期摘要。"""
    run = result["run"]
    run_id = run["run_id"]
    if not RUN_ID_RE.match(run_id):
        raise ValueError("run_id 不符合台账约束")
    experiment = result["experiment"]["experiment"]
    primary = result["metrics"]["primary"]
    decision = result["decision"]
    init_db(db_path)
    last_error: sqlite3.OperationalError | None = None
    for attempt in range(_BUSY_RETRIES):
        try:
            return _save_once(db_path, result, artifact_dir, run, run_id,
                              experiment, primary, decision)
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == _BUSY_RETRIES - 1:
                raise
            last_error = exc
            time.sleep(_BUSY_DELAY * (attempt + 1))
    raise last_error  # type: ignore[misc]


def _save_once(
    db_path: Path,
    result: dict[str, Any],
    artifact_dir: str,
    run: dict[str, Any],
    run_id: str,
    experiment: dict[str, Any],
    primary: dict[str, Any],
    decision: dict[str, Any],
) -> dict[str, Any]:
    current_findings = list(result["findings"])
    with closing(connect(db_path)) as connection:
        _open_immediate(connection)
        try:
            comparison = _build_comparison(
                connection,
                {
                    "run_id": run_id,
                    "generated_at": run["generated_at"],
                    "experiment_id": experiment["id"],
                    "config_digest": run["config_digest"],
                },
                current_findings,
            )
            for table in (
                "finding_lifecycles", "checks", "findings", "metrics",
                "strata", "runs",
            ):
                connection.execute(
                    f"DELETE FROM {table} WHERE run_id = ?", (run_id,)
                )
            connection.execute(
                """INSERT INTO runs (
                    run_id, generated_at, experiment_id, experiment_name, decision,
                    decision_title, matched_rule, decision_reason, headline,
                    primary_metric_key, primary_effect, primary_p_value, finding_count,
                    input_digest, config_digest, artifact_dir, decision_json,
                    baseline_run_id, comparison_status, skipped_run_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id, run["generated_at"], experiment["id"], experiment["name"],
                    decision["card"], decision["title"], decision["matched_rule"],
                    decision["reason"], result["headline"], primary["key"],
                    primary["absolute_effect"], primary["p_value"],
                    len(current_findings), run["input_digest"], run["config_digest"],
                    artifact_dir, _json(decision),
                    comparison["baseline_run_id"], comparison["comparison_status"],
                    comparison["skipped_run_count"],
                ),
            )
            connection.executemany(
                "INSERT INTO checks (run_id, code, status, label, summary, details_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [(run_id, item["code"], item["status"], item["label"],
                  item["summary"], _json(item["details"])) for item in result["checks"]],
            )
            connection.executemany(
                "INSERT INTO findings (run_id, experiment_id, finding_key, "
                "evidence_summary, code, severity, category, title, detail, evidence_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(run_id, experiment["id"], item.get("finding_key", ""),
                  item.get("evidence_summary", ""),
                  item["code"], item["severity"], item["category"],
                  item["title"], item["detail"], _json(item["evidence"]))
                 for item in current_findings],
            )
            lifecycle = comparison["lifecycle"]
            lifecycle_rows = [
                _entry_to_row(entry, finding, run_id, experiment["id"])
                for entry, finding in (
                    (lifecycle.entries[item["finding_key"]], item)
                    for item in current_findings
                )
            ]
            lifecycle_rows.extend(
                _entry_to_row(entry, None, run_id, experiment["id"])
                for entry in lifecycle.resolved
            )
            connection.executemany(
                """INSERT INTO finding_lifecycles (
                    run_id, experiment_id, finding_key, status, baseline_run_id,
                    previous_run_id, first_seen_run_id, prior_evidence_summary,
                    evidence_changed, reappeared, cross_config_gap, code, severity,
                    title, evidence_summary, prior_code, prior_severity, prior_title
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                lifecycle_rows,
            )
            metric_rows = [
                (run_id, item["role"], item["key"], item["label"], item["control_n"],
                 item["treatment_n"], item["control_value"], item["treatment_value"],
                 item["absolute_effect"], item["p_value"], int(item["significant"]),
                 int(item["favorable"]), item["guardrail_status"], _json(item))
                for item in [primary, *result["metrics"]["secondary"],
                             *result["metrics"]["guardrails"]]
            ]
            connection.executemany(
                "INSERT INTO metrics (run_id, role, metric_key, label, control_n, "
                "treatment_n, control_value, treatment_value, absolute_effect, p_value, "
                "significant, favorable, guardrail_status, details_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
            # 重放旧 run 时，后继 run 的基准/生命周期引用可能失效，在同一事务内
            # 按时间顺序重算同 experiment 的后继 run，保证关系始终可回查。
            _recompute_successors(connection, experiment["id"], run_id)
        except Exception:
            connection.rollback()
            raise
        else:
            connection.commit()
    return lifecycle_payload(
        db_path, run_id,
        baseline_run_id=comparison["baseline_run_id"],
        comparison_status=comparison["comparison_status"],
        skipped_run_count=comparison["skipped_run_count"],
        immediate_run_id=comparison["immediate_run_id"],
    )


def _recompute_successors(
    connection: sqlite3.Connection, experiment_id: str, changed_run_id: str
) -> None:
    """重放旧 run 后，重算同 experiment 后继 run 的基准与生命周期行。"""
    changed = connection.execute(
        "SELECT generated_at FROM runs WHERE run_id = ?", (changed_run_id,)
    ).fetchone()
    if changed is None:
        return
    successors = connection.execute(
        """SELECT run_id, generated_at, config_digest FROM runs
           WHERE experiment_id = ?
             AND (generated_at > ? OR (generated_at = ? AND run_id > ?))
           ORDER BY generated_at ASC, run_id ASC""",
        (experiment_id, changed["generated_at"], changed["generated_at"],
         changed_run_id),
    ).fetchall()
    for successor in successors:
        prior_rows = connection.execute(
            """SELECT run_id, generated_at, config_digest FROM runs
               WHERE experiment_id = ?
                 AND (generated_at < ? OR (generated_at = ? AND run_id < ?))
               ORDER BY generated_at ASC, run_id ASC""",
            (experiment_id, successor["generated_at"], successor["generated_at"],
             successor["run_id"]),
        ).fetchall()
        immediate = prior_rows[-1] if prior_rows else None
        same_config = [
            RunSnapshot(row["run_id"], row["generated_at"], row["config_digest"])
            for row in prior_rows
            if row["config_digest"] == successor["config_digest"]
        ]
        current = [
            {
                "finding_key": row["finding_key"],
                "evidence_summary": row["evidence_summary"],
                "code": row["code"],
                "severity": row["severity"],
                "title": row["title"],
            }
            for row in connection.execute(
                "SELECT finding_key, evidence_summary, code, severity, title "
                "FROM findings WHERE run_id = ? AND finding_key != ''",
                (successor["run_id"],),
            ).fetchall()
        ]
        prior_by_run = _findings_for_runs(
            connection, [snapshot.run_id for snapshot in same_config]
        )
        first_seen = _first_seen(
            connection, experiment_id, successor["config_digest"],
            successor["run_id"],
        )
        cross_config_gap = bool(
            same_config
            and len(prior_rows) - len(same_config) > 0
            and prior_rows[-1]["run_id"] != same_config[-1].run_id
        )
        lifecycle = classify_findings(
            RunSnapshot(
                successor["run_id"], successor["generated_at"],
                successor["config_digest"],
            ),
            current,
            same_config,
            prior_by_run,
            first_seen,
            cross_config_gap=cross_config_gap,
        )
        if same_config:
            baseline_run_id = same_config[-1].run_id
            comparison_status = "comparable"
        elif immediate is None:
            baseline_run_id, comparison_status = None, "no_baseline"
        else:
            baseline_run_id, comparison_status = None, STATUS_INCOMPARABLE
        skipped = (
            len(prior_rows) - len(same_config)
            if comparison_status == "comparable"
            else len(prior_rows)
        )
        connection.execute(
            """UPDATE runs SET baseline_run_id = ?, comparison_status = ?,
               skipped_run_count = ? WHERE run_id = ?""",
            (baseline_run_id, comparison_status, skipped, successor["run_id"]),
        )
        connection.execute(
            "DELETE FROM finding_lifecycles WHERE run_id = ?",
            (successor["run_id"],),
        )
        finding_by_key = {item["finding_key"]: item for item in current}
        rows = [
            _entry_to_row(entry, finding_by_key[entry.finding_key],
                          successor["run_id"], experiment_id)
            for entry in lifecycle.entries.values()
        ]
        rows.extend(
            _entry_to_row(entry, None, successor["run_id"], experiment_id)
            for entry in lifecycle.resolved
        )
        connection.executemany(
            """INSERT INTO finding_lifecycles (
                run_id, experiment_id, finding_key, status, baseline_run_id,
                previous_run_id, first_seen_run_id, prior_evidence_summary,
                evidence_changed, reappeared, cross_config_gap, code, severity,
                title, evidence_summary, prior_code, prior_severity, prior_title
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
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
                "SELECT code, severity, category, title, detail, finding_key, "
                "evidence_summary FROM findings WHERE run_id = ? "
                "ORDER BY severity, code", (run_id,)
            ).fetchall()
        ]
        payload["lifecycle"] = _lifecycle_for_run(connection, run_id)
        payload["next_run_id"] = _successor_run_id(connection, row)
        return payload


def _successor_run_id(connection: sqlite3.Connection, row: sqlite3.Row) -> str | None:
    """同 experiment 中紧随其后的成功 run（时间次序，含配置变化后的 run）。"""
    successor = connection.execute(
        """SELECT run_id FROM runs
           WHERE experiment_id = ?
             AND (generated_at > ? OR (generated_at = ? AND run_id > ?))
           ORDER BY generated_at ASC, run_id ASC LIMIT 1""",
        (row["experiment_id"], row["generated_at"], row["generated_at"], row["run_id"]),
    ).fetchone()
    return successor["run_id"] if successor else None


def _lifecycle_for_run(
    connection: sqlite3.Connection, run_id: str
) -> dict[str, Any]:
    run_row = connection.execute(
        "SELECT * FROM runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    if run_row is None:
        return {}
    rows = connection.execute(
        "SELECT * FROM finding_lifecycles WHERE run_id = ? ORDER BY id", (run_id,)
    ).fetchall()
    if not rows:
        # 旧库历史 run：finding 没有稳定身份，保持可读并明确无比较基准。
        return {
            "baseline_run_id": None,
            "comparison_status": "legacy_no_identity",
            "skipped_run_count": 0,
            "immediate_run_id": None,
            "entries": [],
            "resolved": [],
        }
    entries = [_entry_payload(row) for row in rows if row["status"] != "resolved"]
    resolved = [_entry_payload(row) for row in rows if row["status"] == "resolved"]
    return {
        "baseline_run_id": run_row["baseline_run_id"],
        "comparison_status": run_row["comparison_status"],
        "skipped_run_count": run_row["skipped_run_count"],
        "immediate_run_id": None,
        "entries": entries,
        "resolved": resolved,
    }


def _entry_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "finding_key": row["finding_key"],
        "status": row["status"],
        "baseline_run_id": row["baseline_run_id"],
        "previous_run_id": row["previous_run_id"],
        "first_seen_run_id": row["first_seen_run_id"],
        "evidence_changed": bool(row["evidence_changed"]),
        "reappeared": bool(row["reappeared"]),
        "cross_config_gap": bool(row["cross_config_gap"]),
        "code": row["code"],
        "severity": row["severity"],
        "title": row["title"],
        "evidence_summary": row["evidence_summary"],
        "prior_evidence_summary": row["prior_evidence_summary"],
        "prior": (
            None if row["prior_code"] is None else {
                "code": row["prior_code"],
                "severity": row["prior_severity"],
                "title": row["prior_title"],
            }
        ),
    }


def get_finding_history(
    db_path: Path, run_id: str, finding_key: str
) -> list[dict[str, Any]]:
    """同一 experiment 内该 finding_key 的完整出现轨迹（含当前 run）。

    只返回该 key 实际产生 finding 的 run；它消退的 run 不出现在轨迹中，
    消退关系由 lifecycle.resolved 表达。
    """
    if not RUN_ID_RE.match(run_id) or not db_path.exists():
        return []
    init_db(db_path)
    with closing(connect(db_path)) as connection:
        run_row = connection.execute(
            "SELECT experiment_id FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if run_row is None or not finding_key:
            return []
        rows = connection.execute(
            """SELECT r.run_id AS run_id, r.generated_at AS generated_at,
                      r.config_digest AS config_digest, f.code AS code,
                      f.severity AS severity, f.title AS title,
                      f.evidence_summary AS evidence_summary,
                      l.status AS status, l.reappeared AS reappeared,
                      l.evidence_changed AS evidence_changed,
                      l.first_seen_run_id AS first_seen_run_id
               FROM findings f
               JOIN runs r ON r.run_id = f.run_id
               LEFT JOIN finding_lifecycles l
                 ON l.run_id = f.run_id AND l.finding_key = f.finding_key
               WHERE r.experiment_id = ? AND f.finding_key = ?
               ORDER BY r.generated_at ASC, r.run_id ASC""",
            (run_row["experiment_id"], finding_key),
        ).fetchall()
        return [dict(row) for row in rows]


def lifecycle_payload(
    db_path: Path,
    run_id: str,
    *,
    baseline_run_id: str | None,
    comparison_status: str,
    skipped_run_count: int,
    immediate_run_id: str | None,
) -> dict[str, Any]:
    """供报告使用的生命周期 JSON 结构（含状态计数与可回查 run 引用）。"""
    if not db_path.exists():
        return {}
    with closing(connect(db_path)) as connection:
        rows = connection.execute(
            "SELECT * FROM finding_lifecycles WHERE run_id = ? ORDER BY id", (run_id,)
        ).fetchall()
    entries = [_entry_payload(row) for row in rows if row["status"] != "resolved"]
    resolved = [_entry_payload(row) for row in rows if row["status"] == "resolved"]
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return {
        "strategy": "same_config_digest_nearest",
        "baseline_run_id": baseline_run_id,
        "comparison_status": comparison_status,
        "skipped_run_count": skipped_run_count,
        "immediate_run_id": immediate_run_id,
        "counts": counts,
        "entries": entries,
        "resolved": resolved,
    }


def count_runs(db_path: Path) -> int:
    if not db_path.exists():
        return 0
    init_db(db_path)
    with closing(connect(db_path)) as connection:
        row = connection.execute("SELECT COUNT(*) AS total FROM runs").fetchone()
        return int(row["total"])
