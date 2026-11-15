"""结果成熟期（outcome_horizon）行为测试。

覆盖验收场景：
1. 无 horizon 的旧配置完全兼容。
2. assigned_at + horizon 的成熟边界（含等号）。
3. pending 与 mature_but_missing 不会互换。
4. 不同 MetricSpec horizon 得到不同有效样本并影响 CI/decision。
5. 主指标 pending 时正向成熟子样本不产出 ship。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from app.audit import run_audit
from app.checks import assess_data_quality
from app.config import load_config
from app.metrics import (
    assess_measurement,
    collect_measurements,
    compute_metric,
    compute_metric_with_measurement,
)
from app.models import Assignment, Observation

T0 = datetime(2026, 3, 1, tzinfo=timezone.utc)
HORIZON = 7.0  # days


def _config(tmp_path, horizon: float | None = HORIZON, end_day: int = 31):
    text = (
        "schema_version: 1\n"
        "experiment:\n"
        "  id: exp-horizon-test\n"
        "  name: 成熟期测试实验\n"
        "  owner: qa\n"
        "  hypothesis: horizon 测试\n"
        "  start: '2026-03-01T00:00:00+00:00'\n"
        f"  end: '2026-03-{end_day:02d}T00:00:00+00:00'\n"
        "control: control\n"
        "variants:\n"
        "  control: {label: 对照组}\n"
        "  treatment: {label: 实验组}\n"
        "split:\n"
        "  expected: {control: 0.5, treatment: 0.5}\n"
        "  tolerance: 0.2\n"
        "  min_assignments: 4\n"
        "exposure: {max_per_unit: 1, tolerance: 0}\n"
        "metrics:\n"
        "  primary:\n"
        "    key: activation_rate\n"
        "    column: activated\n"
        "    label: 激活率\n"
        "    kind: proportion\n"
        "    orientation: higher_is_better\n"
        "    min_effect: 0.0\n"
    )
    if horizon is not None:
        text += f"    outcome_horizon: {horizon}\n"
    text += (
        "  secondary:\n"
        "    - key: session_depth\n"
        "      column: session_depth\n"
        "      label: 会话深度\n"
        "      kind: mean\n"
        "      orientation: higher_is_better\n"
        "  guardrails:\n"
        "    - key: crash_rate\n"
        "      column: crashed\n"
        "      label: 崩溃率\n"
        "      kind: proportion\n"
        "      orientation: lower_is_better\n"
        "      max_degradation: 0.5\n"
        "stratification:\n"
        "  fields: [platform]\n"
        "  min_group_size: 2\n"
        "  max_groups: 4\n"
        "  alpha: 0.05\n"
        "  min_favorable_share: 0.5\n"
        "decision:\n"
        "  fallback: review\n"
        "  cards:\n"
        "    ship: {title: 可上线, action: go}\n"
        "    iterate: {title: 继续迭代, action: wait}\n"
        "    stop: {title: 停止, action: halt}\n"
        "    review: {title: 人工复核, action: check}\n"
        "  rules:\n"
        "    - id: ship-positive\n"
        "      card: ship\n"
        "      priority: 10\n"
        "      reason: 正向显著且成熟完整\n"
        "      when:\n"
        "        all:\n"
        "          - flag: primary_positive_significant\n"
        "          - flag: measurement_complete\n"
    )
    path = tmp_path / "experiment.yaml"
    path.write_text(text, encoding="utf-8")
    return load_config(path)


def _assignment(unit: str, variant: str, assigned_at: datetime) -> Assignment:
    return Assignment(
        unit_id=unit,
        experiment_id="exp-horizon-test",
        variant=variant,
        assigned_at=assigned_at,
        strata={"platform": "mobile"},
    )


def _observation(unit: str, variant: str, observed_at: datetime,
                 activated: float, depth: float = 1.0,
                 crashed: float = 0.0) -> Observation:
    return Observation(
        unit_id=unit,
        experiment_id="exp-horizon-test",
        variant=variant,
        observed_at=observed_at,
        values={
            "activation_rate": activated,
            "session_depth": depth,
            "crash_rate": crashed,
        },
    )


def _assignment_map(assignments):
    return {row.unit_id: row for row in assignments}


def _run(config, assignments, observations, generated_at, run_id="run-horizon-test"):
    return run_audit(
        config,
        assignments,
        observations,
        run_id=run_id,
        generated_at=generated_at.isoformat(),
        input_digest="0" * 64,
        config_digest="1" * 64,
    )


def _symmetric_units(count: int = 4):
    """control/treatment 各半的唯一 unit 分配。"""
    return [
        _assignment(
            f"unit-{index:04d}",
            "control" if index <= count // 2 else "treatment",
            T0,
        )
        for index in range(1, count + 1)
    ]


# ---------------------------------------------------------------------------
# 场景 1：无 horizon 的旧配置完全兼容
# ---------------------------------------------------------------------------


def test_legacy_config_without_horizon_still_uses_all_observations(tmp_path):
    config = _config(tmp_path, horizon=None)
    assignments = _symmetric_units()
    observations = [
        _observation("unit-0001", "control", T0 + timedelta(hours=1), 0.0),
        _observation("unit-0002", "control", T0 + timedelta(days=2), 1.0),
        _observation("unit-0003", "treatment", T0 + timedelta(days=1), 1.0),
        _observation("unit-0004", "treatment", T0 + timedelta(days=9), 1.0),
    ]
    result = _run(config, assignments, observations, T0 + timedelta(days=10))
    primary = result["metrics"]["primary"]
    assert primary["measurement_status"] == "legacy"
    assert primary["outcome_horizon"] is None
    assert primary["control_n"] == 2 and primary["treatment_n"] == 2
    assert result["measurement"]["tracked"] is False
    assert result["run"]["provisional"] is False
    assert result["decision"]["provisional"] is False
    assert all(
        check["code"] != "measurement.maturity" for check in result["checks"]
    )
    codes = {finding["code"] for finding in result["findings"]}
    assert "measurement_pending" not in codes
    assert "measurement_incomplete" not in codes
    # 旧配置不新增结果成熟期相关 flag 的副作用：仍为 legacy 状态。
    assert result["flags"]["measurement_tracked"] is False


def test_legacy_compute_metric_ignores_assignment_age(tmp_path):
    config = _config(tmp_path, horizon=None)
    stamp = T0 + timedelta(days=1)
    observations = [
        Observation(f"unit-{i:04d}", config.experiment_id, "control", stamp,
                    {config.primary.key: 0.0})
        for i in range(1, 21)
    ] + [
        Observation(f"unit-{i:04d}", config.experiment_id, "treatment", stamp,
                    {config.primary.key: 1.0})
        for i in range(21, 41)
    ]
    result = compute_metric(config, observations, config.primary, "primary")
    assert result.computed is True
    assert result.control_n == 20 and result.treatment_n == 20


# ---------------------------------------------------------------------------
# 场景 2：assigned_at + horizon 边界
# ---------------------------------------------------------------------------


def test_maturity_boundary_is_inclusive_and_just_before_is_pending(tmp_path):
    config = _config(tmp_path)
    assignments = _symmetric_units()
    mature_at = T0 + timedelta(days=HORIZON)
    observations = [
        _observation(row.unit_id, row.variant, mature_at,
                     0.0 if row.variant == "control" else 1.0)
        for row in assignments
    ]
    on_boundary = _run(config, assignments, observations, mature_at,
                       run_id="run-boundary-on")
    assert on_boundary["metrics"]["primary"]["measurement_status"] == "complete"
    assert on_boundary["metrics"]["primary"]["control_n"] == 2
    assert on_boundary["counts"]["pending_units"] == 0

    before = _run(config, assignments, observations,
                  mature_at - timedelta(microseconds=1), run_id="run-boundary-before")
    primary = before["metrics"]["primary"]
    assert primary["measurement_status"] == "provisional"
    assert primary["measurement"]["pending_units"] == 4
    assert primary["measurement"]["eligible_units"] == 0
    assert primary["measurement"]["mature_but_missing_units"] == 0
    assert primary["control_n"] == 0 and primary["treatment_n"] == 0
    assert primary["computed"] is False


def test_observation_exactly_at_horizon_is_counted_as_mature(tmp_path):
    config = _config(tmp_path)
    assignments = _symmetric_units()
    mature_at = T0 + timedelta(days=HORIZON)
    observations = [
        _observation(row.unit_id, row.variant, mature_at,
                     0.0 if row.variant == "control" else 1.0)
        for row in assignments
    ]
    cohort = assess_measurement(
        config, assignments, observations, config.primary, "primary", mature_at,
    )
    assert cohort.status == "complete"
    assert cohort.counts["eligible_units"] == 4
    assert {unit.status for unit in cohort.units} == {"mature"}

    early = assess_measurement(
        config, assignments,
        [replace(row, observed_at=row.observed_at - timedelta(seconds=1))
         for row in observations],
        config.primary, "primary", mature_at,
    )
    assert early.status == "incomplete"
    assert early.counts["observed_before_horizon_units"] == 4
    assert early.counts["eligible_units"] == 0


# ---------------------------------------------------------------------------
# 场景 3：pending 与 mature_but_missing 不互换
# ---------------------------------------------------------------------------


def test_new_cohort_pending_and_old_cohort_mature_but_missing(tmp_path):
    config = _config(tmp_path)
    old = T0
    new = T0 + timedelta(days=20)
    assignments = [
        _assignment("unit-0001", "control", old),
        _assignment("unit-0002", "control", new),
        _assignment("unit-0003", "treatment", old),
        _assignment("unit-0004", "treatment", new),
    ]
    observations = [
        # 老 cohort 的 unit-0001 无观测 -> mature_but_missing
        # 新 cohort 的观测尚未成熟 -> pending（不进入效应集）
        _observation("unit-0002", "control", new + timedelta(days=1), 1.0),
        _observation("unit-0003", "treatment", old + timedelta(days=8), 1.0),
        _observation("unit-0004", "treatment", new + timedelta(days=1), 1.0),
    ]
    run_at = T0 + timedelta(days=21)
    cohort = assess_measurement(
        config, assignments, observations, config.primary, "primary", run_at
    )
    by_unit = {unit.unit_id: unit for unit in cohort.units}
    assert by_unit["unit-0001"].status == "mature_but_missing"
    assert by_unit["unit-0003"].status == "mature"
    assert by_unit["unit-0002"].status == "pending"
    assert by_unit["unit-0004"].status == "pending"
    assert cohort.counts["pending_units"] == 2
    assert cohort.counts["mature_but_missing_units"] == 1
    assert cohort.status == "provisional"
    assert {row.unit_id for row in cohort.eligible_observations} == {"unit-0003"}

    result = _run(config, assignments, observations, run_at, run_id="run-mixed")
    codes = {finding["code"] for finding in result["findings"]}
    assert "measurement_pending" in codes
    assert "measurement_incomplete" in codes
    assert result["counts"]["pending_units"] == 2
    assert result["counts"]["mature_but_missing_units"] == 1


def test_pending_is_not_zero_and_not_permanent_missing(tmp_path):
    config = _config(tmp_path)
    assignments = _symmetric_units()
    observations = [
        _observation(row.unit_id, row.variant, T0 + timedelta(days=1),
                     0.0 if row.variant == "control" else 1.0)
        for row in assignments
    ]
    early = assess_measurement(
        config, assignments, observations, config.primary, "primary",
        T0 + timedelta(days=3),
    )
    assert early.status == "provisional"
    assert early.counts["pending_units"] == 4
    assert early.counts["mature_but_missing_units"] == 0
    assert early.eligible_observations == ()
    # 成熟之后同一批 unit 变成 observed_before_horizon，仍不是 pending。
    later = assess_measurement(
        config, assignments, observations, config.primary, "primary",
        T0 + timedelta(days=8),
    )
    assert later.status == "incomplete"
    assert later.counts["pending_units"] == 0
    assert later.counts["observed_before_horizon_units"] == 4
    assert later.counts["mature_but_missing_units"] == 0


def test_run_between_cohorts_matches_expected_classification(tmp_path):
    config = _config(tmp_path)
    old = T0
    new = T0 + timedelta(days=6)
    assignments = [
        _assignment("unit-0001", "control", old),
        _assignment("unit-0002", "control", old),
        _assignment("unit-0003", "treatment", new),
        _assignment("unit-0004", "treatment", new),
    ]
    observations = [
        _observation("unit-0001", "control", old + timedelta(days=7), 1.0),
        _observation("unit-0002", "control", old + timedelta(days=8), 1.0),
        _observation("unit-0003", "treatment", new + timedelta(days=1), 0.0),
        _observation("unit-0004", "treatment", new + timedelta(days=1), 0.0),
    ]
    cohort = assess_measurement(
        config, assignments, observations, config.primary, "primary",
        T0 + timedelta(days=10),
    )
    assert cohort.counts["eligible_units_by_variant"] == {
        "control": 2, "treatment": 0,
    }
    assert cohort.counts["status_by_variant"]["control"]["mature"] == 2
    assert cohort.counts["status_by_variant"]["treatment"]["pending"] == 2
    assert cohort.counts["denominator_units_by_variant"] == {
        "control": 2, "treatment": 2,
    }
    assert cohort.counts["pending_cohorts"] == [
        {
            "assigned_date": (T0 + timedelta(days=6)).date().isoformat(),
            "mature_at": (new + timedelta(days=HORIZON)).isoformat(),
            "units": 2,
            "by_variant": {"treatment": 2},
        }
    ]


# ---------------------------------------------------------------------------
# 场景 4：不同 MetricSpec horizon 产生不同有效样本并影响 CI/decision
# ---------------------------------------------------------------------------


def test_different_horizons_yield_different_eligible_samples(tmp_path):
    config = _config(tmp_path)
    short_spec = replace(config.primary, outcome_horizon=2.0)
    long_spec = replace(config.primary, outcome_horizon=30.0)
    assignments = []
    observations = []
    for index in range(1, 21):
        variant = "control" if index <= 10 else "treatment"
        unit = f"unit-{index:04d}"
        assignments.append(_assignment(unit, variant, T0))
        observed = T0 + timedelta(days=3 if index <= 10 else 15)
        observations.append(_observation(unit, variant, observed, 1.0))
    run_at = T0 + timedelta(days=16)
    assignment_map = _assignment_map(assignments)
    short_result, short_cohort = compute_metric_with_measurement(
        config, observations, short_spec, "primary", assignment_map, run_at,
    )
    long_result, long_cohort = compute_metric_with_measurement(
        config, observations, long_spec, "primary", assignment_map, run_at,
    )
    assert short_cohort.counts["eligible_units"] == 20
    assert long_cohort.counts["eligible_units"] == 0
    assert short_result.computed is True and long_result.computed is False
    assert short_result.control_n == 10 and short_result.treatment_n == 10
    assert long_result.control_n == 0
    assert short_cohort.status == "complete"
    assert long_cohort.status == "provisional"


def test_secondary_metric_uses_its_own_horizon(tmp_path):
    config = _config(tmp_path)
    primary = replace(config.primary, outcome_horizon=2.0)
    secondary = replace(config.secondary[0], outcome_horizon=20.0)
    config = replace(config, primary=primary, secondary=(secondary,))
    assignments = []
    observations = []
    for index in range(1, 13):
        variant = "control" if index <= 6 else "treatment"
        unit = f"unit-{index:04d}"
        assignments.append(_assignment(unit, variant, T0))
        observations.append(
            _observation(unit, variant, T0 + timedelta(days=3), 1.0,
                         depth=float(index))
        )
    result = _run(config, assignments, observations,
                  T0 + timedelta(days=5), run_id="run-secondary")
    primary_metric = result["metrics"]["primary"]
    secondary_metric = result["metrics"]["secondary"][0]
    assert primary_metric["control_n"] == 6
    assert secondary_metric["control_n"] == 0
    assert secondary_metric["measurement_status"] == "provisional"
    assert result["measurement"]["metrics"][0]["outcome_horizon"] == 2.0
    assert result["measurement"]["metrics"][1]["outcome_horizon"] == 20.0
    assert result["measurement"]["metrics"][1]["eligible_units"] == 0


def test_horizon_changes_confidence_interval(tmp_path):
    config = _config(tmp_path)
    assignments = []
    observations = []
    for index in range(1, 61):
        variant = "control" if index <= 30 else "treatment"
        unit = f"unit-{index:04d}"
        assignments.append(_assignment(unit, variant, T0))
        # 前 20 个 unit 第 5 天报告（只满足短窗），其余第 12 天报告。
        observed = T0 + timedelta(days=5 if index <= 20 else 12)
        if index <= 20:
            value = float(index % 2)
        else:
            value = 1.0 if variant == "treatment" else 0.0
        observations.append(_observation(unit, variant, observed, value))
    short_spec = replace(config.primary, outcome_horizon=3.0)
    long_spec = replace(config.primary, outcome_horizon=10.0)
    assignment_map = _assignment_map(assignments)
    run_at = T0 + timedelta(days=15)
    short_result, short_cohort = compute_metric_with_measurement(
        config, observations, short_spec, "primary", assignment_map, run_at,
    )
    long_result, long_cohort = compute_metric_with_measurement(
        config, observations, long_spec, "primary", assignment_map, run_at,
    )
    assert short_cohort.counts["eligible_units"] == 60
    assert long_cohort.counts["eligible_units"] == 40
    # 短窗包含 30 个 control + 30 个 treatment；长窗只剩第 12 天报告的 10
    # 个 control + 30 个 treatment，有效 n 与 CI 都不同。
    assert (short_result.control_n, short_result.treatment_n) == (30, 30)
    assert (long_result.control_n, long_result.treatment_n) == (10, 30)
    assert (short_result.ci_low, short_result.ci_high) != (
        long_result.ci_low, long_result.ci_high
    )


# ---------------------------------------------------------------------------
# 场景 5：primary pending 时正向成熟子样本不产出 ship
# ---------------------------------------------------------------------------


def test_primary_pending_blocks_ship_even_with_positive_mature_subset(tmp_path):
    config = _config(tmp_path)
    old = T0
    new = T0 + timedelta(days=25)
    assignments = []
    observations = []
    for index in range(1, 61):
        variant = "control" if index <= 30 else "treatment"
        unit = f"unit-{index:04d}"
        assignments.append(_assignment(unit, variant, old))
        observations.append(
            _observation(unit, variant, old + timedelta(days=8),
                         0.0 if variant == "control" else 1.0)
        )
    for index in range(61, 101):
        variant = "control" if index <= 80 else "treatment"
        unit = f"unit-{index:04d}"
        assignments.append(_assignment(unit, variant, new))
        # 早期快照（run 时尚不可见，或可见但未成熟）。
        observations.append(
            _observation(unit, variant, new + timedelta(days=1), 0.0)
        )
        # 成熟后的报告：在 run day26 时属于未来记录，day40 时可采纳。
        observations.append(
            _observation(unit, variant, new + timedelta(days=8), 0.0)
        )
    result = _run(config, assignments, observations,
                  T0 + timedelta(days=26), run_id="run-primary-pending")
    primary = result["metrics"]["primary"]
    assert primary["significant"] is True
    assert primary["favorable"] is True
    assert primary["absolute_effect"] == 1.0
    assert primary["measurement_status"] == "provisional"
    assert result["decision"]["card"] != "ship"
    assert result["decision"]["provisional"] is True
    assert result["run"]["provisional"] is True
    assert result["flags"]["measurement_provisional"] is True
    assert result["flags"]["measurement_complete"] is False
    assert result["flags"]["primary_positive_significant"] is False
    codes = {finding["code"] for finding in result["findings"]}
    assert "measurement_pending" in codes
    assert "metric.provisional" in codes

    later = _run(config, assignments, observations,
                 T0 + timedelta(days=40), run_id="run-primary-mature")
    assert later["run"]["provisional"] is False
    assert later["decision"]["card"] == "ship"


# ---------------------------------------------------------------------------
# 边界与去重
# ---------------------------------------------------------------------------


def test_duplicate_assignments_and_observations_do_not_inflate_denominator(tmp_path):
    config = _config(tmp_path)
    assignments = [
        _assignment("unit-0001", "control", T0),
        _assignment("unit-0002", "control", T0),
        _assignment("unit-0003", "treatment", T0),
        _assignment("unit-0004", "treatment", T0),
        _assignment("unit-0001", "control", T0),
    ]
    observations = [
        _observation("unit-0001", "control", T0 + timedelta(days=8), 0.0),
        _observation("unit-0001", "control", T0 + timedelta(days=9), 1.0),
        _observation("unit-0002", "control", T0 + timedelta(days=8), 0.0),
        _observation("unit-0003", "treatment", T0 + timedelta(days=8), 1.0),
        _observation("unit-0004", "treatment", T0 + timedelta(days=8), 1.0),
    ]
    cohort = assess_measurement(
        config, assignments, observations, config.primary, "primary",
        T0 + timedelta(days=10),
    )
    assert cohort.counts["denominator_units"] == 4
    assert cohort.counts["eligible_units"] == 4
    assert cohort.counts["duplicate_assignment_rows"] == 1
    assert cohort.counts["duplicate_observation_units"] == 1
    assert len(cohort.eligible_observations) == 4


def test_late_report_after_experiment_end_is_usable_but_future_is_not(tmp_path):
    config = _config(tmp_path, end_day=5)
    assignments = _symmetric_units()
    run_at = T0 + timedelta(days=8)
    observations = [
        # 实验结束（day5）后到达、但已满足 horizon 的迟到报告：可用。
        _observation("unit-0001", "control", T0 + timedelta(days=7), 0.0),
        # 实验结束后到达但早于 horizon：不可当作成熟结果。
        _observation("unit-0002", "control", T0 + timedelta(days=6), 0.0),
        _observation("unit-0003", "treatment", T0 + timedelta(days=7), 1.0),
        # run 时刻尚不可见的未来记录：不参与分析。
        _observation("unit-0004", "treatment", T0 + timedelta(days=12), 1.0),
    ]
    cohort = assess_measurement(
        config, assignments, observations, config.primary, "primary", run_at
    )
    assert cohort.counts["late_report_rows"] == 3
    assert cohort.counts["later_than_run_rows"] == 1
    assert cohort.counts["eligible_units"] == 2
    assert cohort.counts["observed_before_horizon_units"] == 1
    assert cohort.counts["mature_but_missing_units"] == 1

    quality, details, _ = assess_data_quality(
        config, assignments, observations, generated_at=run_at,
        measurements=collect_measurements(config, observations, assignments, run_at),
    )
    assert details["late_report_units"] == 3
    # 只有运行时尚不可见的 unit-0004 是窗口异常；实验结束后到达的成熟报告可用。
    assert details["observation_outside_window"] == 1
    assert quality.details["measurement_tracked"] is True


# ---------------------------------------------------------------------------
# 补充边界：stop 不被 provisional 覆盖；全 pending 的 run 不崩溃
# ---------------------------------------------------------------------------


def test_pending_does_not_downgrade_stop_but_marks_provisional(tmp_path):
    config = _config(tmp_path)
    old = T0
    new = T0 + timedelta(days=25)
    assignments = []
    observations = []
    # 老 cohort：主指标显著负向（treatment 全部未转化）。
    for index in range(1, 61):
        variant = "control" if index <= 30 else "treatment"
        unit = f"unit-{index:04d}"
        assignments.append(_assignment(unit, variant, old))
        observations.append(
            _observation(unit, variant, old + timedelta(days=8),
                         1.0 if variant == "control" else 0.0)
        )
    # 新 cohort：仍在等待。
    for index in range(61, 81):
        variant = "control" if index <= 70 else "treatment"
        unit = f"unit-{index:04d}"
        assignments.append(_assignment(unit, variant, new))
        observations.append(
            _observation(unit, variant, new + timedelta(days=1), 0.0)
        )
    result = _run(config, assignments, observations,
                  T0 + timedelta(days=26), run_id="run-stop-pending")
    assert result["metrics"]["primary"]["significant"] is True
    assert result["metrics"]["primary"]["favorable"] is False
    assert result["decision"]["card"] != "ship"
    assert result["decision"]["provisional"] is True
    assert result["run"]["provisional"] is True


def test_tracked_config_with_no_observations_yet_is_pending_not_error(tmp_path):
    config = _config(tmp_path)
    assignments = _symmetric_units()
    result = _run(config, assignments, [], T0 + timedelta(days=1),
                  run_id="run-no-observations")
    primary = result["metrics"]["primary"]
    assert primary["measurement_status"] == "provisional"
    assert primary["measurement"]["pending_units"] == 4
    assert primary["computed"] is False
    assert result["decision"]["card"] == "review"
    assert result["decision"]["provisional"] is True
    # 尚未到应观察时间，数据一致性不应因为「没有观测」而失败。
    quality = next(
        check for check in result["checks"] if check["code"] == "data.quality"
    )
    assert quality["status"] == "pass"


def test_pending_guardrail_is_not_reported_as_ok(tmp_path):
    config = _config(tmp_path)
    tracked_guardrail = replace(config.guardrails[0], outcome_horizon=7.0)
    config = replace(config, guardrails=(tracked_guardrail,))
    assignments = _symmetric_units()
    observations = [
        _observation(row.unit_id, row.variant, T0 + timedelta(days=1),
                     0.0 if row.variant == "control" else 1.0)
        for row in assignments
    ]
    result = _run(config, assignments, observations,
                  T0 + timedelta(days=2), run_id="run-guardrail-pending")
    guardrail = result["metrics"]["guardrails"][0]
    assert guardrail["measurement_status"] == "provisional"
    assert guardrail["guardrail_status"] == "pending"
    check = next(
        item for item in result["checks"] if item["code"] == "guardrail.status"
    )
    assert check["status"] == "warn"
    assert result["flags"]["guardrail_measurement_pending"] is True
    assert result["flags"]["guardrail_watch"] is True
    codes = {finding["code"] for finding in result["findings"]}
    assert "guardrail.pending.crash_rate" in codes


def test_mixed_config_tracked_primary_and_legacy_guardrail(tmp_path):
    config = _config(tmp_path)
    legacy_guardrail = replace(config.guardrails[0], outcome_horizon=None)
    config = replace(config, guardrails=(legacy_guardrail,))
    assignments = _symmetric_units()
    mature_at = T0 + timedelta(days=HORIZON)
    observations = [
        _observation(row.unit_id, row.variant, mature_at,
                     0.0 if row.variant == "control" else 1.0)
        for row in assignments
    ]
    early = _run(config, assignments, observations,
                 T0 + timedelta(days=1), run_id="run-mixed-legacy")
    primary = early["metrics"]["primary"]
    guardrail = early["metrics"]["guardrails"][0]
    assert primary["measurement_status"] == "provisional"
    assert primary["control_n"] == 0
    # 旧护栏不参与成熟度，仍使用全部观测并保持旧状态语义。
    assert guardrail["measurement_status"] == "legacy"
    assert guardrail["guardrail_status"] in ("ok", "watch", "breach", "n/a")
    assert guardrail["control_n"] == 2
    # 只有主指标是 tracked，因此不会因为护栏的旧口径而产生 pending flag。
    assert early["flags"]["measurement_provisional"] is True
    guardrail_measurement = next(
        item for item in early["measurement"]["metrics"]
        if item["role"] == "guardrail"
    )
    assert guardrail_measurement["status"] == "legacy"
    assert guardrail_measurement["outcome_horizon"] is None
