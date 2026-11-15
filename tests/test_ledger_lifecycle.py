"""台账事务写入与跨运行生命周期集成测试。"""

from __future__ import annotations

import copy
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

from app import ledger
from app.identity import build_finding_identity
from app.lifecycle import (
    STATUS_CONTINUING,
    STATUS_INCOMPARABLE,
    STATUS_NEW,
    STATUS_RESOLVED,
)


def _clone(result: dict, run_id: str, generated_at: str, *, config_digest=None):
    cloned = copy.deepcopy(result)
    cloned["run"]["run_id"] = run_id
    cloned["run"]["generated_at"] = generated_at
    if config_digest is not None:
        cloned["run"]["config_digest"] = config_digest
    return cloned


def _entry_by_code(detail: dict, code: str) -> dict:
    matches = [
        entry for entry in detail["lifecycle"]["entries"] if entry["code"] == code
    ]
    assert matches, f"lifecycle 中找不到 {code}"
    return matches[0]


def test_first_run_has_no_baseline_and_findings_are_new(audit_result, tmp_path):
    db = tmp_path / "ledger.sqlite3"
    ledger.save_run(db, audit_result, "out/run-a")
    detail = ledger.get_run(db, audit_result["run"]["run_id"])
    assert detail["lifecycle"]["comparison_status"] == "no_baseline"
    assert detail["lifecycle"]["baseline_run_id"] is None
    assert {entry["status"] for entry in detail["lifecycle"]["entries"]} == {STATUS_NEW}
    assert detail["next_run_id"] is None
    # finding 行携带稳定身份
    assert all(f["finding_key"] and f["evidence_summary"] for f in detail["findings"])


def test_second_same_config_run_marks_continuing_and_resolved(audit_result, tmp_path):
    db = tmp_path / "ledger.sqlite3"
    first = _clone(audit_result, "run-life-00000001", "2026-03-01T00:00:00+00:00")
    second = _clone(audit_result, "run-life-00000002", "2026-03-02T00:00:00+00:00")
    # 第二次 run：保留 reversal，移除 guardrail，证据数值变化
    second["findings"] = [
        f for f in second["findings"] if f["code"] != "guardrail.watch.crash_rate"
    ]
    reversal = next(
        f for f in second["findings"] if f["code"] == "stratification.reversal"
    )
    reversal["evidence"]["groups"][0]["effect"] = -0.999
    reversal["finding_key"], reversal["evidence_summary"] = build_finding_identity(
        reversal["code"], reversal["category"], reversal["evidence"]
    )

    ledger.save_run(db, first, "out/run-life-00000001")
    payload = ledger.save_run(db, second, "out/run-life-00000002")

    assert payload["comparison_status"] == "comparable"
    assert payload["baseline_run_id"] == "run-life-00000001"
    detail = ledger.get_run(db, "run-life-00000002")
    reversal_entry = _entry_by_code(detail, "stratification.reversal")
    assert reversal_entry["status"] == STATUS_CONTINUING
    assert reversal_entry["evidence_changed"] is True
    assert reversal_entry["previous_run_id"] == "run-life-00000001"
    assert reversal_entry["first_seen_run_id"] == "run-life-00000001"
    resolved = detail["lifecycle"]["resolved"]
    assert len(resolved) == 1
    assert resolved[0]["status"] == STATUS_RESOLVED
    assert resolved[0]["code"] == "guardrail.watch.crash_rate"
    assert detail["next_run_id"] is None
    first_detail = ledger.get_run(db, "run-life-00000001")
    assert first_detail["next_run_id"] == "run-life-00000002"


def test_reappearance_after_gap_tracks_previous_occurrence(audit_result, tmp_path):
    db = tmp_path / "ledger.sqlite3"
    r1 = _clone(audit_result, "run-gap-000000001", "2026-03-01T00:00:00+00:00")
    r2 = _clone(audit_result, "run-gap-000000002", "2026-03-02T00:00:00+00:00")
    r2["findings"] = [
        f for f in r2["findings"] if f["code"] != "metric.fragile"
    ]
    r3 = _clone(audit_result, "run-gap-000000003", "2026-03-03T00:00:00+00:00")

    ledger.save_run(db, r1, "out/1")
    ledger.save_run(db, r2, "out/2")
    ledger.save_run(db, r3, "out/3")

    detail3 = ledger.get_run(db, "run-gap-000000003")
    fragile = _entry_by_code(detail3, "metric.fragile")
    assert fragile["status"] == STATUS_CONTINUING
    assert fragile["reappeared"] is True
    # 回查指向真正上一次出现的 run-1，而不是缺失该 finding 的 run-2
    assert fragile["previous_run_id"] == "run-gap-000000001"
    assert fragile["baseline_run_id"] == "run-gap-000000002"

    history = ledger.get_finding_history(
        db, "run-gap-000000003", fragile["finding_key"]
    )
    assert [row["run_id"] for row in history] == [
        "run-gap-000000001", "run-gap-000000003",
    ]


def test_config_change_marks_incomparable_and_does_not_resolve_old_findings(
    audit_result, tmp_path
):
    db = tmp_path / "ledger.sqlite3"
    r1 = _clone(audit_result, "run-cfg-000000001", "2026-03-01T00:00:00+00:00")
    r2 = _clone(
        audit_result, "run-cfg-000000002", "2026-03-02T00:00:00+00:00",
        config_digest="different-config-digest",
    )
    ledger.save_run(db, r1, "out/1")
    payload = ledger.save_run(db, r2, "out/2")

    assert payload["comparison_status"] == STATUS_INCOMPARABLE
    assert payload["baseline_run_id"] is None
    assert payload["immediate_run_id"] == "run-cfg-000000001"
    assert payload["resolved"] == []
    assert {entry["status"] for entry in payload["entries"]} == {STATUS_NEW}
    detail = ledger.get_run(db, "run-cfg-000000002")
    assert detail["lifecycle"]["comparison_status"] == STATUS_INCOMPARABLE


def test_rollback_to_same_config_resumes_lineage_across_config_change(
    audit_result, tmp_path
):
    db = tmp_path / "ledger.sqlite3"
    digest_a = audit_result["run"]["config_digest"]
    r1 = _clone(audit_result, "run-rb-0000000001", "2026-03-01T00:00:00+00:00")
    r2 = _clone(
        audit_result, "run-rb-0000000002", "2026-03-02T00:00:00+00:00",
        config_digest="digest-b",
    )
    # r2 配置变化，reversal finding 暂时不再产生
    r2["findings"] = [
        f for f in r2["findings"] if f["code"] != "stratification.reversal"
    ]
    r3 = _clone(
        audit_result, "run-rb-0000000003", "2026-03-03T00:00:00+00:00",
        config_digest=digest_a,
    )
    ledger.save_run(db, r1, "out/1")
    ledger.save_run(db, r2, "out/2")
    payload = ledger.save_run(db, r3, "out/3")

    # 跨过配置不同的 run-2，沿同 config_digest 找回 run-1
    assert payload["comparison_status"] == "comparable"
    assert payload["baseline_run_id"] == "run-rb-0000000001"
    assert payload["skipped_run_count"] == 1
    reversal = next(
        entry for entry in payload["entries"]
        if entry["code"] == "stratification.reversal"
    )
    assert reversal["status"] == STATUS_CONTINUING
    assert reversal["cross_config_gap"] is True
    assert reversal["reappeared"] is False
    assert reversal["previous_run_id"] == "run-rb-0000000001"


def test_equal_timestamps_use_run_id_ordering(audit_result, tmp_path):
    db = tmp_path / "ledger.sqlite3"
    stamp = "2026-03-01T00:00:00+00:00"
    r1 = _clone(audit_result, "run-ts-000000000a", stamp)
    r2 = _clone(audit_result, "run-ts-000000000b", stamp)
    # 按 run_id 次序写入：相同 generated_at 下基准选择必须确定
    ledger.save_run(db, r1, "out/a")
    ledger.save_run(db, r2, "out/b")
    detail_a = ledger.get_run(db, "run-ts-000000000a")
    detail_b = ledger.get_run(db, "run-ts-000000000b")
    assert detail_a["next_run_id"] == "run-ts-000000000b"
    assert detail_b["lifecycle"]["comparison_status"] == "comparable"
    assert detail_b["lifecycle"]["baseline_run_id"] == "run-ts-000000000a"
    assert detail_a["lifecycle"]["comparison_status"] == "no_baseline"


def test_late_committed_run_with_smaller_run_id_keeps_own_order(audit_result, tmp_path):
    # 相同时间戳、run_id 次序与提交次序不一致：
    # 后写入的 run-...a 在事务内重算先提交的 run-...b，最终关系与
    # (generated_at, run_id) 的确定次序保持一致，而不是停留在提交瞬间的状态。
    db = tmp_path / "ledger.sqlite3"
    stamp = "2026-03-01T00:00:00+00:00"
    r1 = _clone(audit_result, "run-ts-000000000a", stamp)
    r2 = _clone(audit_result, "run-ts-000000000b", stamp)
    ledger.save_run(db, r2, "out/b")
    ledger.save_run(db, r1, "out/a")
    detail_b = ledger.get_run(db, "run-ts-000000000b")
    detail_a = ledger.get_run(db, "run-ts-000000000a")
    # 重算后：a 是次序上的前驱（无基准），b 以 a 为基准
    assert detail_a["lifecycle"]["comparison_status"] == "no_baseline"
    assert detail_b["lifecycle"]["comparison_status"] == "comparable"
    assert detail_b["lifecycle"]["baseline_run_id"] == "run-ts-000000000a"
    assert detail_a["next_run_id"] == "run-ts-000000000b"
    # 展示次序仍由 (generated_at, run_id) 确定
    assert [r["run_id"] for r in ledger.list_runs(db)] == [
        "run-ts-000000000b", "run-ts-000000000a",
    ]


def test_concurrent_audits_each_choose_committed_baseline(audit_result, tmp_path):
    db = tmp_path / "ledger.sqlite3"
    base = datetime(2026, 3, 1, tzinfo=timezone.utc)
    errors: list[Exception] = []

    def worker(index: int) -> None:
        try:
            run_id = f"run-conc-000000{index:02d}"
            result = _clone(
                audit_result, run_id,
                (base + timedelta(seconds=index)).isoformat(),
            )
            ledger.save_run(db, result, f"out/{run_id}")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    runs = ledger.list_runs(db, limit=20)
    assert len(runs) == 6
    # 除最早 run 外，每个 run 都必须指向一个真实存在、且更早提交的基准
    run_ids = {row["run_id"] for row in runs}
    for row in runs:
        if row["baseline_run_id"] is not None:
            assert row["baseline_run_id"] in run_ids
    baselined = [row for row in runs if row["baseline_run_id"]]
    assert len(baselined) == 5


def test_save_run_is_idempotent_in_one_transaction(audit_result, tmp_path):
    db = tmp_path / "ledger.sqlite3"
    r1 = _clone(audit_result, "run-idem-0000001", "2026-03-01T00:00:00+00:00")
    r2 = _clone(audit_result, "run-idem-0000002", "2026-03-02T00:00:00+00:00")
    ledger.save_run(db, r1, "out/1")
    ledger.save_run(db, r2, "out/2")
    # 重放 r1：删除自身生命周期行后重建，r2 的数据不受影响
    ledger.save_run(db, r1, "out/1")
    assert ledger.count_runs(db) == 2
    detail2 = ledger.get_run(db, "run-idem-0000002")
    assert detail2["lifecycle"]["baseline_run_id"] == "run-idem-0000001"
    with sqlite3.connect(db) as con:
        count = con.execute(
            "SELECT COUNT(*) FROM finding_lifecycles WHERE run_id = ?",
            ("run-idem-0000001",),
        ).fetchone()[0]
    assert count == len(r1["findings"])


def test_legacy_database_remains_readable_and_marked_without_baseline(
    audit_result, tmp_path
):
    # 手工构造 v1 旧库：runs/findings 缺少身份列与生命周期表
    db = tmp_path / "legacy.sqlite3"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE runs (
            run_id TEXT PRIMARY KEY, generated_at TEXT NOT NULL,
            experiment_id TEXT NOT NULL, experiment_name TEXT NOT NULL,
            decision TEXT NOT NULL, decision_title TEXT NOT NULL,
            matched_rule TEXT NOT NULL, decision_reason TEXT NOT NULL,
            headline TEXT NOT NULL, primary_metric_key TEXT NOT NULL,
            primary_effect REAL NOT NULL, primary_p_value REAL NOT NULL,
            finding_count INTEGER NOT NULL, input_digest TEXT NOT NULL,
            config_digest TEXT NOT NULL, artifact_dir TEXT NOT NULL,
            decision_json TEXT NOT NULL
        );
        CREATE TABLE findings (
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
            code TEXT NOT NULL, severity TEXT NOT NULL, category TEXT NOT NULL,
            title TEXT NOT NULL, detail TEXT NOT NULL, evidence_json TEXT NOT NULL
        );
        CREATE TABLE checks (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT,
            code TEXT, status TEXT, label TEXT, summary TEXT, details_json TEXT);
        CREATE TABLE metrics (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT,
            role TEXT, metric_key TEXT, label TEXT, control_n INTEGER,
            treatment_n INTEGER, control_value REAL, treatment_value REAL,
            absolute_effect REAL, p_value REAL, significant INTEGER,
            favorable INTEGER, guardrail_status TEXT, details_json TEXT);
        CREATE TABLE strata (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT,
            field TEXT, value TEXT, control_n INTEGER, treatment_n INTEGER,
            absolute_effect REAL, p_value REAL, eligible INTEGER,
            reversal INTEGER, details_json TEXT);
        """
    )
    con.execute(
        "INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("run-legacy-0001", "2026-02-01T00:00:00+00:00", "exp-1", "旧实验",
         "review", "人工复核", "fallback", "r", "h", "activation_rate", 0.0, 0.5,
         1, "in", "cfg-old", "out/x", "{}"),
    )
    con.execute(
        "INSERT INTO findings (run_id, code, severity, category, title, detail, "
        "evidence_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("run-legacy-0001", "data.low_coverage", "medium", "data", "覆盖率低",
         "d", "{}"),
    )
    con.commit()
    con.close()

    detail = ledger.get_run(db, "run-legacy-0001")
    assert detail is not None
    assert detail["lifecycle"]["comparison_status"] == "legacy_no_identity"
    assert detail["lifecycle"]["entries"] == []
    assert detail["findings"][0]["finding_key"] == ""

    # 新 run 写入后，旧 finding 不会被误判为 resolved
    new_run = _clone(
        audit_result, "run-after-legacy-1", "2026-03-01T00:00:00+00:00"
    )
    payload = ledger.save_run(db, new_run, "out/new")
    assert payload["resolved"] == []
    assert payload["comparison_status"] == "no_baseline"


def test_replay_old_run_recomputes_successor_relationships(audit_result, tmp_path):
    db = tmp_path / "ledger.sqlite3"
    r1 = _clone(audit_result, "run-rep-00000001", "2026-03-01T00:00:00+00:00")
    r2 = _clone(audit_result, "run-rep-00000002", "2026-03-02T00:00:00+00:00")
    r3 = _clone(audit_result, "run-rep-00000003", "2026-03-03T00:00:00+00:00")
    ledger.save_run(db, r1, "out/1")
    ledger.save_run(db, r2, "out/2")
    ledger.save_run(db, r3, "out/3")

    # 重放 run-2：其 finding 集合变化（guardrail 消退），run-3 的关系必须被重算
    r2_replay = _clone(audit_result, "run-rep-00000002", "2026-03-02T00:00:00+00:00")
    r2_replay["findings"] = [
        f for f in r2_replay["findings"]
        if f["code"] != "guardrail.watch.crash_rate"
    ]
    ledger.save_run(db, r2_replay, "out/2")

    detail3 = ledger.get_run(db, "run-rep-00000003")
    guard = _entry_by_code(detail3, "guardrail.watch.crash_rate")
    # run-3 的 guardrail 现在相对新基准 run-2 是重现，而不是普通持续
    assert guard["status"] == STATUS_CONTINUING
    assert guard["reappeared"] is True
    assert guard["previous_run_id"] == "run-rep-00000001"
    assert guard["baseline_run_id"] == "run-rep-00000002"
    # 消退关系记录在它消失的 run-2，run-3 仅记录重现
    detail2 = ledger.get_run(db, "run-rep-00000002")
    assert any(
        row["code"] == "guardrail.watch.crash_rate"
        and row["status"] == STATUS_RESOLVED
        for row in detail2["lifecycle"]["resolved"]
    )


def test_finding_history_and_evidence_summary_limit(audit_result, tmp_path):
    db = tmp_path / "ledger.sqlite3"
    r1 = _clone(audit_result, "run-hist-0000001", "2026-03-01T00:00:00+00:00")
    r2 = _clone(audit_result, "run-hist-0000002", "2026-03-02T00:00:00+00:00")
    ledger.save_run(db, r1, "out/1")
    ledger.save_run(db, r2, "out/2")
    guard = next(
        f for f in r1["findings"] if f["code"] == "guardrail.watch.crash_rate"
    )
    history = ledger.get_finding_history(db, "run-hist-0000002", guard["finding_key"])
    assert [row["run_id"] for row in history] == [
        "run-hist-0000001", "run-hist-0000002",
    ]
    assert all(row["evidence_summary"] for row in history)
    assert ledger.get_finding_history(db, "run-hist-0000002", "unknown-key") == []
    assert ledger.get_finding_history(db, "run-missing", guard["finding_key"]) == []
