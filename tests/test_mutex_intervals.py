"""互斥参与区间协议测试：半开活动区间、三态结论与旧格式兼容策略。"""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.audit import run_audit
from app.checks import _activity_segments, assess_mutex
from app.data import (
    DataValidationError,
    load_assignments,
    load_observations,
    sha256_bytes,
    sha256_files,
)
from app.models import Assignment
from app.reporting import render_html, render_markdown

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"
CONFIG_PATH = FIXTURES / "experiment.yaml"
ASSIGNMENTS_PATH = FIXTURES / "assignments.csv"
OBSERVATIONS_PATH = FIXTURES / "observations.csv"

WINDOW_START = datetime(2026, 3, 1, tzinfo=timezone.utc)
WINDOW_END = datetime(2026, 3, 15, tzinfo=timezone.utc)


def _at(day: int, hour: int = 0, minute: int = 0, month: int = 3) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=timezone.utc)


def _assignment(
    unit_id: str,
    experiment_id: str,
    assigned_at: datetime,
    ended_at: datetime | None = None,
    *,
    ended_at_known: bool = True,
    source_line: int | None = None,
) -> Assignment:
    return Assignment(
        unit_id=unit_id,
        experiment_id=experiment_id,
        variant="control",
        assigned_at=assigned_at,
        strata={"platform": "desktop", "region": "north"},
        ended_at=ended_at,
        ended_at_known=ended_at_known,
        source_line=source_line,
    )


def _mutex_id(config) -> str:
    return config.mutexes[0].experiment_id


def _strip_lines(payload):
    """行号是文件位置信息；比较行序无关性时先归一化。"""
    if isinstance(payload, dict):
        return {
            key: ([] if key == "source_lines" else _strip_lines(value))
            for key, value in payload.items()
        }
    if isinstance(payload, list):
        return [_strip_lines(item) for item in payload]
    return payload


def _finding_shape(findings):
    return [
        (
            finding.code,
            finding.severity,
            finding.category,
            finding.title,
            _strip_lines(finding.evidence),
        )
        for finding in findings
    ]


def test_segments_close_open_interval_on_reassignment(config):
    rows = [
        _assignment("unit-0001", "exp-x", _at(2), None, source_line=2),
        _assignment("unit-0001", "exp-x", _at(5), _at(8), source_line=3),
    ]
    segments = _activity_segments(rows)
    assert len(segments) == 1
    assert segments[0]["start"] == _at(2)
    assert segments[0]["end"] == _at(8)
    assert segments[0]["end_known"] is True
    assert segments[0]["lines"] == [2, 3]


def test_segments_same_timestamp_open_record_wins(config):
    rows = [
        _assignment("unit-0001", "exp-x", _at(2), _at(8), source_line=2),
        _assignment("unit-0001", "exp-x", _at(2), None, source_line=3),
    ]
    segments = _activity_segments(rows)
    assert len(segments) == 1
    assert segments[0]["end"] is None
    assert segments[0]["end_known"] is True


def test_segments_keep_disjoint_intervals_separate(config):
    rows = [
        _assignment("unit-0001", "exp-x", _at(2), _at(3), source_line=2),
        _assignment("unit-0001", "exp-x", _at(10), _at(11), source_line=3),
    ]
    segments = _activity_segments(rows)
    assert [(seg["start"], seg["end"]) for seg in segments] == [
        (_at(2), _at(3)),
        (_at(10), _at(11)),
    ]


def test_segments_zero_length_interval_is_inert(config):
    rows = [_assignment("unit-0001", "exp-x", _at(2), _at(2), source_line=2)]
    assert _activity_segments(rows) == []


def test_mutex_historical_participation_is_clear(config, dataset):
    """反例：互斥实验 09:00–09:30 的历史参与不得误判为冲突。"""
    historical = _assignment(
        "unit-0100", _mutex_id(config),
        datetime(2026, 2, 10, 9, 0, tzinfo=timezone.utc),
        datetime(2026, 2, 10, 9, 30, tzinfo=timezone.utc),
        source_line=2,
    )
    check, details, findings = assess_mutex(
        config, [*dataset.assignments, historical]
    )
    assert check.status == "pass"
    assert details["conflict_units"] == 0
    assert details["unknown_units"] == 0
    assert details["clear_units"] == 1 + 6  # 新增 1 条 + fixture 自带 6 条历史记录
    assert findings == []


def test_mutex_active_overlap_is_conflict_with_evidence(config, dataset):
    """互斥实验中 10:30 仍活动的 unit 必须被识别为真正污染。"""
    active = _assignment(
        "unit-0100", _mutex_id(config), _at(10, 10, 30), None, source_line=2,
    )
    check, details, findings = assess_mutex(config, [*dataset.assignments, active])
    assert check.status == "fail"
    assert details["conflict_units"] == 1
    conflict = next(item for item in findings if item.code == "mutex.conflict")
    assert conflict.severity == "critical"
    evidence = conflict.evidence
    assert evidence["experiment_id"] == _mutex_id(config)
    unit = evidence["units"][0]
    assert unit["unit_id"] == "unit-0100"
    segment = next(seg for seg in unit["segments"] if seg["state"] == "conflict")
    assert segment["overlap_start"] == _at(10, 10, 30).isoformat()
    assert segment["overlap_end"] == WINDOW_END.isoformat()
    assert segment["source_lines"] == [2]
    assert evidence["window"] == {
        "start": WINDOW_START.isoformat(),
        "end": WINDOW_END.isoformat(),
    }


def test_mutex_ended_overlap_reports_exact_intersection(config, dataset):
    ended = _assignment("unit-0100", _mutex_id(config), _at(10), _at(12), source_line=7)
    check, details, findings = assess_mutex(config, [*dataset.assignments, ended])
    assert check.status == "fail"
    unit = next(entry for entry in details["units"] if entry["unit_id"] == "unit-0100")
    segment = next(seg for seg in unit["segments"] if seg["state"] == "conflict")
    assert segment["overlap_start"] == _at(10).isoformat()
    assert segment["overlap_end"] == _at(12).isoformat()
    assert segment["source_lines"] == [7]


def test_mutex_result_is_row_order_independent(config, dataset, tmp_path):
    """同一活动区间、不同 CSV 行序，审计结论必须相同（行号除外）。"""
    mutex_id = _mutex_id(config)
    extra = [
        # 同一 unit 的两段分配：02-10 09:00–09:30 已结束，03-10 起仍活动
        ["unit-0007", mutex_id, "control", "2026-03-10T10:30:00+00:00", "", "desktop", "north"],
        ["unit-0007", mutex_id, "control", "2026-02-10T09:00:00+00:00", "2026-02-10T09:30:00+00:00", "desktop", "north"],
        # 纯历史参与：clear
        ["unit-0008", mutex_id, "control", "2026-02-11T09:00:00+00:00", "2026-02-11T09:30:00+00:00", "mobile", "south"],
    ]
    with ASSIGNMENTS_PATH.open("r", encoding="utf-8", newline="") as handle:
        base = list(csv.reader(handle))
    header, rows = base[0], base[1:]
    forward = tmp_path / "forward.csv"
    backward = tmp_path / "backward.csv"
    for path, body in ((forward, rows + extra), (backward, list(reversed(rows + extra)))):
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerows([header, *body])
    first = assess_mutex(config, load_assignments(forward, config))
    second = assess_mutex(config, load_assignments(backward, config))
    assert first[0].status == second[0].status == "fail"
    assert first[0].summary == second[0].summary
    assert _strip_lines(first[1]) == _strip_lines(second[1])
    assert _finding_shape(first[2]) == _finding_shape(second[2])
    # 行号本身仍指向各自文件中的真实位置
    unit7 = next(
        entry for entry in first[1]["units"] if entry["unit_id"] == "unit-0007"
    )
    assert unit7["state"] == "conflict"
    assert all(segment["source_lines"] for segment in unit7["segments"])


def test_legacy_csv_marks_membership_unknown_and_blocks_ship(
    config, dataset, tmp_path
):
    """无 ended_at 列的旧输入：标记 unknown、阻断 ship，但不伪称实际重叠。"""
    mutex_id = _mutex_id(config)
    with ASSIGNMENTS_PATH.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle))
    ended_index = rows[0].index("ended_at")
    header = [name for name in rows[0] if name != "ended_at"]
    body = [
        [value for index, value in enumerate(row) if index != ended_index]
        for row in rows[1:]
        if row[1] == config.experiment_id  # 只保留本实验行，剔除 fixture 历史记录
    ]
    # 旧格式行：unit-0001 在互斥实验中 03-10 被分配，结束时刻未知
    body.append(
        ["unit-0001", mutex_id, "control", "2026-03-10T10:30:00+00:00", "desktop", "north"]
    )
    legacy_path = tmp_path / "legacy.csv"
    with legacy_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerows([header, *body])
    assignments = load_assignments(legacy_path, config)
    assert all(not row.ended_at_known for row in assignments)

    check, details, findings = assess_mutex(config, assignments)
    assert check.status == "warn"
    assert details["conflict_units"] == 0
    assert details["unknown_units"] == 1
    unknown = next(item for item in findings if item.code == "mutex.membership_unknown")
    assert unknown.severity == "high"
    assert not any(item.code == "mutex.conflict" for item in findings)

    observations = load_observations(OBSERVATIONS_PATH, config)
    result = run_audit(
        config,
        assignments,
        observations,
        run_id="run-test-legacy-mutex",
        generated_at="2026-03-16T00:00:00+00:00",
        input_digest=sha256_files([legacy_path, OBSERVATIONS_PATH]),
        config_digest=sha256_bytes(CONFIG_PATH.read_bytes()),
    )
    # unknown 影响决策：可上线结论被阻断，转为人工复核
    assert result["flags"]["mutex_membership_unknown"] is True
    assert result["flags"]["mutex_conflict"] is False
    assert result["flags"]["no_mutex_conflict"] is False
    assert result["decision"]["card"] == "review"
    assert result["decision"]["matched_rule"] == "review-data-integrity"
    assert result["counts"]["mutex_unknown_units"] == 1
    # 报告不得把未知写成 clear：三态在 JSON / Markdown / HTML 中可区分
    mutex_check = next(
        item for item in result["checks"] if item["code"] == "mutex.conflict"
    )
    assert mutex_check["status"] == "warn"
    assert "mutex.membership_unknown" in render_markdown(result)
    assert "mutex.membership_unknown" in render_html(result)


def test_legacy_csv_provably_clear_when_assigned_after_window(config, dataset):
    """旧格式但 assigned_at 已晚于实验窗口：仅凭开始时刻即可证明不相交。"""
    future = _assignment(
        "unit-0100", _mutex_id(config), _at(20), None,
        ended_at_known=False, source_line=2,
    )
    check, details, findings = assess_mutex(config, [*dataset.assignments, future])
    assert check.status == "pass"
    assert details["unknown_units"] == 0
    assert findings == []


def test_invalid_interval_is_rejected_with_file_and_line(config, tmp_path):
    path = tmp_path / "assignments.csv"
    path.write_text(
        "unit_id,experiment_id,variant,assigned_at,ended_at,platform,region\n"
        "unit-0001,exp-onboarding-card-2026,control,"
        "2026-03-05T10:00:00+00:00,2026-03-05T09:00:00+00:00,desktop,north\n",
        encoding="utf-8",
    )
    with pytest.raises(DataValidationError) as excinfo:
        load_assignments(path, config)
    message = str(excinfo.value)
    assert "assignments.csv:2" in message
    assert "ended_at" in message


def test_empty_ended_at_means_still_active(config, tmp_path):
    path = tmp_path / "assignments.csv"
    path.write_text(
        "unit_id,experiment_id,variant,assigned_at,ended_at,platform,region\n"
        "unit-0001,exp-checkout-copy-2026,control,"
        "2026-03-10T10:30:00+00:00,,desktop,north\n"
        "unit-0001,exp-onboarding-card-2026,control,"
        "2026-03-05T08:00:00+00:00,,desktop,north\n",
        encoding="utf-8",
    )
    assignments = load_assignments(path, config)
    assert all(row.ended_at_known for row in assignments)
    check, details, findings = assess_mutex(config, assignments)
    assert check.status == "fail"
    assert details["conflict_units"] == 1
    assert findings[0].code == "mutex.conflict"


def test_fixture_mutex_history_is_clear_not_conflict(config, dataset):
    """fixture 自带的 6 条互斥实验历史记录判定为 clear，不产生 finding。"""
    check, details, findings = assess_mutex(config, dataset.assignments)
    assert check.status == "pass"
    assert details["conflict_units"] == 0
    assert details["unknown_units"] == 0
    assert details["clear_units"] == 6
    assert findings == []
    clear_entry = details["units"][0]
    assert clear_entry["state"] == "clear"
    assert clear_entry["segments"][0]["ended_at"] is not None
