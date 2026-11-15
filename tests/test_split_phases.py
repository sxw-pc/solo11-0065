"""分阶段流量计划（rollout allocation phases）的契约与逐段核验。"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.audit import run_audit
from app.checks import assess_exposure, assess_split
from app.config import ConfigError, load_config
from app.data import load_assignments
from app.models import AllocationPhase, Assignment, Observation, SplitSpec
from app.reporting import render_html, render_markdown

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"
BASE_CONFIG = FIXTURES / "experiment.yaml"

WINDOW_START = datetime(2026, 3, 1, tzinfo=timezone.utc)
RAMP_END = datetime(2026, 3, 8, tzinfo=timezone.utc)
WINDOW_END = datetime(2026, 3, 15, tzinfo=timezone.utc)

BASE_SPLIT_BLOCK = """\
split:
  expected:
    control: 0.5
    treatment: 0.5
  tolerance: 0.03
  min_assignments: 1200
"""


def _config_text(phases: str) -> str:
    text = BASE_CONFIG.read_text(encoding="utf-8")
    assert BASE_SPLIT_BLOCK in text
    block = (
        "split:\n"
        "  expected:\n"
        "    control: 0.5\n"
        "    treatment: 0.5\n"
        "  tolerance: 0.03\n"
        "  min_assignments: 1200\n"
        "  phases:\n" + phases
    )
    return text.replace(BASE_SPLIT_BLOCK, block, 1)


def _phase_yaml(
    key: str,
    start: str,
    end: str,
    control: float,
    treatment: float,
    minimum: int = 100,
) -> str:
    return (
        f"    - key: {key}\n"
        f"      start: \"{start}\"\n"
        f"      end: \"{end}\"\n"
        f"      expected:\n"
        f"        control: {control}\n"
        f"        treatment: {treatment}\n"
        f"      min_assignments: {minimum}\n"
    )


def _phased_config(
    tmp_path: Path,
    phases: list[tuple[str, str, str, float, float, int]],
) -> Path:
    body = "".join(
        _phase_yaml(key, start, end, control, treatment, minimum)
        for key, start, end, control, treatment, minimum in phases
    )
    target = tmp_path / "experiment.yaml"
    target.write_text(_config_text(body), encoding="utf-8")
    return target


def _phase_spec() -> tuple[AllocationPhase, AllocationPhase]:
    ramp = AllocationPhase(
        key="ramp-90-10",
        start=WINDOW_START,
        end=RAMP_END,
        expected={"control": 0.9, "treatment": 0.1},
        min_assignments=100,
    )
    even = AllocationPhase(
        key="even-50-50",
        start=RAMP_END,
        end=WINDOW_END,
        expected={"control": 0.5, "treatment": 0.5},
        min_assignments=100,
    )
    return ramp, even


def _phased_config_obj(config, phases, *, expected=None, min_assignments=200):
    spec = SplitSpec(
        expected=expected or {"control": 0.5, "treatment": 0.5},
        tolerance=0.03,
        min_assignments=min_assignments,
        phases=tuple(phases),
    )
    return replace(config, split=spec)


def _assignment(unit: str, variant: str, moment: datetime) -> Assignment:
    return Assignment(
        unit_id=unit,
        experiment_id="exp-onboarding-card-2026",
        variant=variant,
        assigned_at=moment,
        strata={"platform": "desktop", "region": "north"},
    )


def _balanced_phase(
    prefix: str,
    count: int,
    control_share: float,
    start: datetime,
    end: datetime,
) -> list[Assignment]:
    control_count = round(count * control_share)
    span = end - start
    rows: list[Assignment] = []
    for index in range(count):
        variant = "control" if index < control_count else "treatment"
        rows.append(
            _assignment(
                f"{prefix}-{index:05d}",
                variant,
                start + span * (index / max(count, 1)),
            )
        )
    return rows


# ---------------------------------------------------------------------------
# 配置契约
# ---------------------------------------------------------------------------


def test_phased_config_parses_schedule(tmp_path):
    ramp, even = _phase_spec()
    path = _phased_config(tmp_path, [
        (ramp.key, ramp.start.isoformat(), ramp.end.isoformat(), 0.9, 0.1, 100),
        (even.key, even.start.isoformat(), even.end.isoformat(), 0.5, 0.5, 100),
    ])
    config = load_config(path)
    assert config.split.phased is True
    assert [phase.key for phase in config.split.phases] == ["ramp-90-10", "even-50-50"]
    assert config.split.phases[0].expected == {"control": 0.9, "treatment": 0.1}
    assert config.split.phases[0].start == WINDOW_START
    assert config.split.phases[-1].end == WINDOW_END
    assert config.split.min_assignments == 1200  # 显式配置优先
    # 未声明 split.expected 时以第一段作为兼容摘要。
    text = path.read_text(encoding="utf-8")
    text = text.replace(
        "  expected:\n    control: 0.5\n    treatment: 0.5\n  tolerance: 0.03\n",
        "  tolerance: 0.03\n",
        1,
    )
    path.write_text(text, encoding="utf-8")
    assert load_config(path).split.expected == {"control": 0.9, "treatment": 0.1}


def test_phased_config_rejects_gap(tmp_path):
    path = _phased_config(tmp_path, [
        ("ramp", "2026-03-01T00:00:00+00:00", "2026-03-07T00:00:00+00:00", 0.9, 0.1, 100),
        ("even", "2026-03-08T00:00:00+00:00", "2026-03-15T00:00:00+00:00", 0.5, 0.5, 100),
    ])
    with pytest.raises(ConfigError, match="空档"):
        load_config(path)


def test_phased_config_rejects_overlap(tmp_path):
    path = _phased_config(tmp_path, [
        ("ramp", "2026-03-01T00:00:00+00:00", "2026-03-09T00:00:00+00:00", 0.9, 0.1, 100),
        ("even", "2026-03-08T00:00:00+00:00", "2026-03-15T00:00:00+00:00", 0.5, 0.5, 100),
    ])
    with pytest.raises(ConfigError, match="重叠"):
        load_config(path)


@pytest.mark.parametrize(
    "phases, message",
    [
        (("ramp", "2026-02-28T00:00:00+00:00", "2026-03-15T00:00:00+00:00", 0.5, 0.5, 100),
         "experiment.start"),
        (("ramp", "2026-03-01T00:00:00+00:00", "2026-03-16T00:00:00+00:00", 0.5, 0.5, 100),
         "experiment.end"),
        (("ramp", "2026-03-01T00:00:00+00:00", "2026-03-15T00:00:00+00:00", 0.8, 0.3, 100),
         "之和"),
        (("ramp", "2026-03-01T00:00:00+00:00", "2026-03-15T00:00:00+00:00", 1.0, 0.0, 100),
         None),
    ],
)
def test_phased_config_boundary_and_proportion_contract(tmp_path, phases, message):
    path = _phased_config(tmp_path, [phases])
    if message is None:
        config = load_config(path)
        assert config.split.phases[0].expected == {"control": 1.0, "treatment": 0.0}
    else:
        with pytest.raises(ConfigError, match=message):
            load_config(path)


def test_phases_must_tile_the_experiment_window(tmp_path):
    """调度区间必须覆盖整个实验窗口；首尾缺口与相邻空档同样拒绝。"""
    leading_gap = _phased_config(tmp_path, [
        ("ramp", "2026-03-02T00:00:00+00:00", "2026-03-08T00:00:00+00:00", 0.9, 0.1, 100),
        ("even", "2026-03-08T00:00:00+00:00", "2026-03-14T00:00:00+00:00", 0.5, 0.5, 100),
    ])
    with pytest.raises(ConfigError, match="experiment.start"):
        load_config(leading_gap)
    trailing_gap = _phased_config(tmp_path, [
        ("ramp", "2026-03-01T00:00:00+00:00", "2026-03-08T00:00:00+00:00", 0.9, 0.1, 100),
        ("even", "2026-03-08T00:00:00+00:00", "2026-03-14T00:00:00+00:00", 0.5, 0.5, 100),
    ])
    with pytest.raises(ConfigError, match="experiment.end"):
        load_config(trailing_gap)


def test_phased_config_requires_full_variant_coverage(tmp_path):
    text = _config_text(
        "    - key: ramp\n"
        "      start: \"2026-03-01T00:00:00+00:00\"\n"
        "      end: \"2026-03-15T00:00:00+00:00\"\n"
        "      expected:\n"
        "        control: 1.0\n"
        "      min_assignments: 100\n"
    )
    path = tmp_path / "experiment.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match="覆盖全部变体"):
        load_config(path)


def test_phased_config_requires_minimum_assignments(tmp_path):
    text = _config_text(
        "    - key: ramp\n"
        "      start: \"2026-03-01T00:00:00+00:00\"\n"
        "      end: \"2026-03-15T00:00:00+00:00\"\n"
        "      expected:\n"
        "        control: 0.5\n"
        "        treatment: 0.5\n"
    )
    path = tmp_path / "experiment.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match="min_assignments"):
        load_config(path)


def test_phased_config_rejects_duplicate_phase_key(tmp_path):
    path = _phased_config(tmp_path, [
        ("ramp", "2026-03-01T00:00:00+00:00", "2026-03-08T00:00:00+00:00", 0.9, 0.1, 100),
        ("ramp", "2026-03-08T00:00:00+00:00", "2026-03-15T00:00:00+00:00", 0.5, 0.5, 100),
    ])
    with pytest.raises(ConfigError, match="重复"):
        load_config(path)


# ---------------------------------------------------------------------------
# 边界映射与数据层 phase key
# ---------------------------------------------------------------------------


def test_phase_key_boundaries_are_half_open_utc(config, tmp_path):
    ramp, even = _phase_spec()
    phased = _phased_config_obj(config, (ramp, even))
    rows = [
        _assignment("u-start", "control", WINDOW_START),
        _assignment("u-before-boundary", "control", RAMP_END - timedelta(microseconds=1)),
        _assignment("u-at-boundary", "control", RAMP_END),
        _assignment("u-at-end", "control", WINDOW_END),
        _assignment("u-outside", "control", WINDOW_END + timedelta(seconds=1)),
    ]
    assert phased.split.phase_key_at(rows[0].assigned_at) == "ramp-90-10"
    assert phased.split.phase_key_at(rows[1].assigned_at) == "ramp-90-10"
    assert phased.split.phase_key_at(rows[2].assigned_at) == "even-50-50"
    assert phased.split.phase_key_at(rows[3].assigned_at) == "even-50-50"
    assert phased.split.phase_key_at(rows[4].assigned_at) is None


def test_loader_assigns_stable_phase_key(config, tmp_path):
    ramp, even = _phase_spec()
    phased = _phased_config_obj(config, (ramp, even))
    csv_path = tmp_path / "assignments.csv"
    csv_path.write_text(
        "unit_id,experiment_id,variant,assigned_at,platform,region\n"
        "unit-0001,exp-onboarding-card-2026,control,2026-03-07T23:59:59+00:00,desktop,north\n"
        "unit-0002,exp-onboarding-card-2026,treatment,2026-03-08T00:00:00+00:00,mobile,south\n"
        "unit-0003,exp-onboarding-card-2026,control,2026-03-15T00:00:00+00:00,legacy,west\n"
        "unit-0004,exp-onboarding-card-2026,control,2026-03-15T00:00:01+00:00,desktop,north\n",
        encoding="utf-8",
    )
    loaded = load_assignments(csv_path, phased)
    assert [row.phase_key for row in loaded] == [
        "ramp-90-10", "even-50-50", "even-50-50", None,
    ]


def test_static_config_has_no_phase_key(config):
    assert config.split.phased is False
    assert config.split.phases == ()
    assert config.split.phase_key_at(WINDOW_START) is None


# ---------------------------------------------------------------------------
# 逐阶段核验
# ---------------------------------------------------------------------------


def test_phase_boundaries_assign_units_accurately(config):
    ramp, even = _phase_spec()
    phased = _phased_config_obj(config, (ramp, even))
    rows = _balanced_phase("r", 100, 0.9, WINDOW_START, RAMP_END - timedelta(seconds=1))
    rows += _balanced_phase("e", 100, 0.5, RAMP_END, WINDOW_END)
    check, details, findings = assess_split(phased, rows)
    assert check.status == "pass"
    first, second = details["phases"]
    assert first["counts"] == {"control": 90, "treatment": 10}
    assert second["counts"] == {"control": 50, "treatment": 50}
    assert first["total"] == 100 and second["total"] == 100
    assert findings == []


def test_opposite_phase_deviations_cannot_cancel(config):
    """汇总 50/50 通过，但两段各自违背当时声明计划，逐段必须 fail。"""
    ramp, even = _phase_spec()
    phased = _phased_config_obj(config, (ramp, even))
    # ramp 阶段 45/55（偏离 90/10 达 45pp）；even 阶段 55/45（偏离 50/50 达 5pp）。
    rows = _balanced_phase("r", 100, 0.45, WINDOW_START, RAMP_END - timedelta(seconds=1))
    rows += _balanced_phase("e", 100, 0.55, RAMP_END, WINDOW_END)
    check, details, findings = assess_split(phased, rows)
    first, second = details["phases"]
    assert first["counts"] == {"control": 45, "treatment": 55}
    assert second["counts"] == {"control": 55, "treatment": 45}
    # 全局合并正是 100/100，静态口径会通过；逐段均为 fail。
    merged = {
        key: first["counts"][key] + second["counts"][key]
        for key in ("control", "treatment")
    }
    assert merged == {"control": 100, "treatment": 100}
    assert first["status"] == "fail"
    assert second["status"] == "fail"
    assert check.status == "fail"
    assert {finding.code for finding in findings} == {"split.phase_imbalance"}
    assert sum(1 for finding in findings if finding.code == "split.phase_imbalance") == 2
    assert all(finding.severity == "high" for finding in findings)


def test_low_sample_phase_is_unverified_not_fail(config):
    ramp, even = _phase_spec()
    phased = _phased_config_obj(config, (ramp, even))
    rows = _balanced_phase("r", 120, 0.9, WINDOW_START, RAMP_END - timedelta(seconds=1))
    # 10 个 unit 远低于 even 阶段 100 的最低样本数；比例本身甚至完美 50/50。
    rows += _balanced_phase("e", 10, 0.5, RAMP_END, WINDOW_END)
    check, details, findings = assess_split(phased, rows)
    first, second = details["phases"]
    assert first["status"] == "pass"
    assert second["status"] == "unverified"
    assert second["low_sample"] is True
    assert second["tolerance_breach"] is False
    assert check.status == "warn"
    assert [finding.code for finding in findings] == ["split.phase_insufficient"]
    assert findings[0].severity == "medium"


def test_low_sample_phase_does_not_mask_another_phase_failure(config):
    ramp, even = _phase_spec()
    phased = _phased_config_obj(config, (ramp, even))
    rows = _balanced_phase("r", 120, 0.5, WINDOW_START, RAMP_END - timedelta(seconds=1))
    rows += _balanced_phase("e", 10, 0.5, RAMP_END, WINDOW_END)
    check, details, findings = assess_split(phased, rows)
    assert details["phases"][0]["status"] == "fail"
    assert details["phases"][1]["status"] == "unverified"
    assert check.status == "fail"
    codes = {finding.code for finding in findings}
    assert codes == {"split.phase_imbalance", "split.phase_insufficient"}
    assert details["worst_phase"] == "ramp-90-10"


def test_cross_phase_duplicate_unit_is_conflict_not_double_counted(config):
    ramp, even = _phase_spec()
    phased = _phased_config_obj(config, (ramp, even))
    rows = _balanced_phase("r", 120, 0.9, WINDOW_START, RAMP_END - timedelta(seconds=1))
    rows += _balanced_phase("e", 100, 0.5, RAMP_END, WINDOW_END)
    # 提取一个 ramp 阶段 unit，让它在 even 阶段再被分配一次。
    duplicate = rows[0]
    rows.append(
        replace(
            duplicate,
            assigned_at=RAMP_END + timedelta(hours=1),
            variant="treatment",
        )
    )
    check, details, findings = assess_split(phased, rows)
    assert check.status == "fail"
    assert details["cross_phase_duplicate_units"] == 1
    assert details["cross_phase_duplicate_samples"][0]["unit_id"] == duplicate.unit_id
    # 冲突 unit 从两段计数中剔除：ramp 少 1 个 unit，even 不计入该 unit。
    assert sum(details["phases"][0]["counts"].values()) == 119
    assert sum(details["phases"][1]["counts"].values()) == 100
    conflict = next(
        finding for finding in findings
        if finding.code == "split.phase_reassignment"
    )
    assert conflict.severity == "high"
    assert conflict.evidence["count"] == 1
    assert details["worst_phase"] == "cross-phase-duplicate"


def test_cross_phase_duplicate_prevents_false_pass(config):
    ramp, even = _phase_spec()
    phased = _phased_config_obj(config, (ramp, even))
    rows = _balanced_phase("r", 120, 0.9, WINDOW_START, RAMP_END - timedelta(seconds=1))
    rows += _balanced_phase("e", 100, 0.5, RAMP_END, WINDOW_END)
    duplicate = rows[0]
    rows.append(replace(duplicate, assigned_at=RAMP_END + timedelta(hours=1)))
    check, details, findings = assess_split(phased, rows)
    # 若重复 unit 被同时计入两段，比例仍看起来健康；实现必须显式阻止这种伪装。
    assert check.status == "fail"
    assert any(
        finding.code == "split.phase_reassignment" for finding in findings
    )
    result = _phased_run(phased, rows)
    assert result["decision"]["card"] == "review"
    assert result["flags"]["split_phase_reassigned"] is True
    assert any(
        finding["code"] == "split.phase_reassignment"
        for finding in result["findings"]
    )
    assert "split.phase_reassignment" in render_markdown(result)


def test_same_phase_duplicate_counts_once_and_exposure_reports(config):
    ramp, even = _phase_spec()
    phased = _phased_config_obj(config, (ramp, even))
    rows = _balanced_phase("r", 100, 0.9, WINDOW_START, RAMP_END - timedelta(seconds=1))
    rows += _balanced_phase("e", 100, 0.5, RAMP_END, WINDOW_END)
    duplicate = rows[0]
    rows.append(replace(duplicate, assigned_at=duplicate.assigned_at + timedelta(minutes=5)))
    check, details, _ = assess_split(phased, rows)
    assert details["phases"][0]["counts"] == {"control": 90, "treatment": 10}
    assert details["cross_phase_duplicate_units"] == 0
    assert check.status == "pass"
    exposure_check, exposure_details, exposure_findings = assess_exposure(
        phased, rows, []
    )
    assert exposure_check.status == "fail"
    assert exposure_details["duplicate_assignment_units"] == 1
    assert exposure_findings[0].code == "exposure.duplicate"


def test_row_order_does_not_change_phase_result(config):
    ramp, even = _phase_spec()
    phased = _phased_config_obj(config, (ramp, even))
    rows = _balanced_phase("r", 100, 0.45, WINDOW_START, RAMP_END - timedelta(seconds=1))
    rows += _balanced_phase("e", 100, 0.55, RAMP_END, WINDOW_END)
    duplicate = rows[3]
    rows.append(replace(duplicate, assigned_at=duplicate.assigned_at + timedelta(minutes=1)))
    first = assess_split(phased, rows)
    reversed_rows = list(reversed(rows))
    second = assess_split(phased, reversed_rows)
    assert first[0] == second[0]
    assert first[1] == second[1]
    assert [finding.code for finding in first[2]] == [
        finding.code for finding in second[2]
    ]


def test_assignments_outside_schedule_are_reported(config):
    ramp, even = _phase_spec()
    phased = _phased_config_obj(config, (ramp, even))
    rows = _balanced_phase("r", 100, 0.9, WINDOW_START, RAMP_END - timedelta(seconds=1))
    rows += _balanced_phase("e", 100, 0.5, RAMP_END, WINDOW_END)
    rows.append(_assignment("u-early", "control", WINDOW_START - timedelta(days=1)))
    check, details, _ = assess_split(phased, rows)
    assert details["outside_schedule_units"] == 1
    assert details["outside_schedule_samples"] == ["u-early"]
    assert check.status == "pass"


def test_empty_phase_is_unverified(config):
    ramp, even = _phase_spec()
    phased = _phased_config_obj(config, (ramp, even))
    rows = _balanced_phase("r", 120, 0.9, WINDOW_START, RAMP_END - timedelta(seconds=1))
    check, details, findings = assess_split(phased, rows)
    empty = details["phases"][1]
    assert empty["total"] == 0
    assert empty["counts"] == {"control": 0, "treatment": 0}
    assert empty["status"] == "unverified"
    assert empty["p_value"] == 1.0
    assert details["phases"][0]["status"] == "pass"
    assert check.status == "warn"
    assert [finding.code for finding in findings] == ["split.phase_insufficient"]


def test_phased_zero_share_variant_is_detected(config):
    phase = AllocationPhase(
        key="control-only",
        start=WINDOW_START,
        end=WINDOW_END,
        expected={"control": 1.0, "treatment": 0.0},
        min_assignments=100,
    )
    phased = _phased_config_obj(config, (phase,))
    rows = _balanced_phase("r", 120, 0.9, WINDOW_START, WINDOW_END)
    check, details, findings = assess_split(phased, rows)
    assert details["phases"][0]["status"] == "fail"
    assert details["phases"][0]["chi_square"] is None
    assert details["phases"][0]["zero_expected_violations"] == ["treatment"]
    assert details["phases"][0]["p_value"] == 0.0
    assert check.status == "fail"


# ---------------------------------------------------------------------------
# 端到端：flags、决策、报告
# ---------------------------------------------------------------------------


def _observations(rows: list[Assignment]) -> list[Observation]:
    return [
        Observation(
            unit_id=row.unit_id,
            experiment_id=row.experiment_id,
            variant=row.variant,
            observed_at=row.assigned_at + timedelta(hours=2),
            values={
                "activation_rate": 1.0 if row.variant == "treatment" else 0.0,
                "session_depth": 5.0,
                "crash_rate": 0.0,
            },
        )
        for row in rows
    ]


def _phased_run(config, rows):
    return run_audit(
        config,
        rows,
        _observations(rows),
        run_id="run-test-phases-01",
        generated_at="2026-03-16T00:00:00+00:00",
        input_digest="a" * 64,
        config_digest="b" * 64,
    )


def test_assignments_from_other_experiments_are_ignored(config):
    ramp, even = _phase_spec()
    phased = _phased_config_obj(config, (ramp, even))
    rows = _balanced_phase("r", 100, 0.9, WINDOW_START, RAMP_END - timedelta(seconds=1))
    rows += _balanced_phase("e", 100, 0.5, RAMP_END, WINDOW_END)
    other = replace(
        _assignment("unit-9999", "control", WINDOW_START + timedelta(days=1)),
        experiment_id="exp-checkout-copy-2026",
    )
    check, details, findings = assess_split(phased, [*rows, other])
    assert check.status == "pass"
    assert details["outside_schedule_units"] == 0
    assert findings == []


def test_phased_failure_drives_flags_decision_and_reports(config, tmp_path):
    ramp, even = _phase_spec()
    phased = _phased_config_obj(
        config, (ramp, even), expected={"control": 0.5, "treatment": 0.5},
        min_assignments=200,
    )
    rows = _balanced_phase("r", 100, 0.45, WINDOW_START, RAMP_END - timedelta(seconds=1))
    rows += _balanced_phase("e", 100, 0.55, RAMP_END, WINDOW_END)
    result = _phased_run(phased, rows)
    split_check = next(
        check for check in result["checks"] if check["code"] == "split.balance"
    )
    assert split_check["status"] == "fail"
    assert split_check["details"]["mode"] == "phased"
    assert [phase["status"] for phase in split_check["details"]["phases"]] == [
        "fail", "fail",
    ]
    assert result["flags"]["split_balanced"] is False
    assert result["flags"]["split_imbalanced"] is True
    assert result["decision"]["card"] == "review"
    assert result["decision"]["matched_rule"] == "review-data-integrity"
    assert result["counts"]["split_phases"] == 2
    assert result["counts"]["cross_phase_duplicate_units"] == 0
    assert result["counts"]["split_worst_phase"] == "ramp-90-10"
    assert result["flags"]["split_phase_failed"] is True
    assert result["flags"]["split_phase_unverified"] is False
    assert result["flags"]["split_phase_reassigned"] is False
    config_payload = result["experiment"]["split"]
    assert [phase["key"] for phase in config_payload["phases"]] == [
        "ramp-90-10", "even-50-50",
    ]

    markdown = render_markdown(result)
    assert "## 分阶段流量计划" in markdown
    assert "split.phase_imbalance" in markdown
    assert "ramp-90-10" in markdown
    html = render_html(result)
    assert "分阶段流量计划" in html
    assert "ramp-90-10" in html


def test_phased_unverified_phase_warns_decision(config):
    ramp, even = _phase_spec()
    phased = _phased_config_obj(
        config, (ramp, even), expected={"control": 0.5, "treatment": 0.5},
        min_assignments=200,
    )
    rows = _balanced_phase("r", 120, 0.9, WINDOW_START, RAMP_END - timedelta(seconds=1))
    rows += _balanced_phase("e", 10, 0.5, RAMP_END, WINDOW_END)
    result = _phased_run(phased, rows)
    split_check = next(
        check for check in result["checks"] if check["code"] == "split.balance"
    )
    assert split_check["status"] == "warn"
    assert result["flags"]["split_imbalanced"] is True
    assert result["decision"]["card"] == "review"
    finding_codes = {finding["code"] for finding in result["findings"]}
    assert "split.phase_insufficient" in finding_codes
    assert "split.phase_imbalance" not in finding_codes


def test_static_config_audit_output_is_unchanged(config, dataset, audit_result):
    split_check = next(
        check for check in audit_result["checks"] if check["code"] == "split.balance"
    )
    assert "mode" not in split_check["details"]
    assert "phases" not in split_check["details"]
    assert "phases" not in audit_result["experiment"]["split"]
    assert split_check["details"]["expected"] == {"control": 0.5, "treatment": 0.5}
    assert audit_result["flags"]["split_balanced"] is True
    assert len(audit_result["checks"]) == 8
    assert "分阶段流量计划" not in render_markdown(audit_result)
    assert "分阶段流量计划" not in render_html(audit_result)
    assert "split_phases" not in audit_result["counts"]
