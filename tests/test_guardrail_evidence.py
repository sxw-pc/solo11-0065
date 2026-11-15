"""护栏三态证据契约：Newcombe-Wilson 区间、方向翻转与审计链路一致性。"""

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from app.audit import run_audit
from app.evaluate import build_flags, guardrail_check
from app.metrics import (
    compute_metric,
    degradation_interval,
    guardrail_status,
)
from app.models import Check, MetricSpec, Observation
from app.reporting import render_html, render_markdown
from app.stats import newcombe_wilson_interval, welch_t_test, wilson_score_interval

STAMP = datetime(2026, 3, 2, tzinfo=timezone.utc)


def _proportion_observations(
    config, key, control_successes, control_n, treatment_successes, treatment_n
):
    rows = []
    for index in range(control_n):
        rows.append(
            Observation(
                f"unit-c{index:05d}", config.experiment_id, "control", STAMP,
                {key: 1.0 if index < control_successes else 0.0},
            )
        )
    for index in range(treatment_n):
        rows.append(
            Observation(
                f"unit-t{index:05d}", config.experiment_id, "treatment", STAMP,
                {key: 1.0 if index < treatment_successes else 0.0},
            )
        )
    return rows


def _guardrail_result(
    config, control_successes, control_n, treatment_successes, treatment_n,
    *, max_degradation=0.01, orientation="lower_is_better", key="crash_rate",
    alpha=0.05,
):
    spec = replace(
        config.guardrails[0], key=key, orientation=orientation,
        max_degradation=max_degradation, alpha=alpha,
    )
    observations = _proportion_observations(
        config, key, control_successes, control_n, treatment_successes, treatment_n
    )
    return compute_metric(config, observations, spec, "guardrail")


def test_wilson_score_interval_known_values():
    low, high = wilson_score_interval(8, 800, 0.05)
    assert low == pytest.approx(0.005075687871617386, abs=1e-12)
    assert high == pytest.approx(0.01960761080994513, abs=1e-12)


def test_wilson_score_interval_respects_boundaries():
    low, high = wilson_score_interval(0, 10, 0.05)
    assert low == 0.0
    assert 0.0 < high < 1.0
    low, high = wilson_score_interval(10, 10, 0.05)
    assert high == 1.0
    assert 0.0 < low < 1.0
    with pytest.raises(ValueError):
        wilson_score_interval(1, 0, 0.05)
    with pytest.raises(ValueError):
        wilson_score_interval(11, 10, 0.05)


def test_newcombe_wilson_interval_known_values():
    low, high = newcombe_wilson_interval(8, 800, 14, 800, 0.05)
    assert low == pytest.approx(-0.004415235138205093, abs=1e-12)
    assert high == pytest.approx(0.02015633479376252, abs=1e-12)
    low, high = newcombe_wilson_interval(200, 800, 240, 800, 0.05)
    assert low == pytest.approx(0.00623604813239103, abs=1e-12)
    assert high == pytest.approx(0.09351191262327609, abs=1e-12)


def test_newcombe_wilson_interval_stays_feasible_at_boundaries():
    cases = [
        (0, 10, 0, 10),
        (10, 10, 0, 10),
        (0, 10, 10, 10),
        (10, 10, 10, 10),
        (0, 1, 0, 1),
        (1, 1, 0, 1),
        (1, 3, 2, 7),
    ]
    for control_x, control_n, treatment_x, treatment_n in cases:
        low, high = newcombe_wilson_interval(
            control_x, control_n, treatment_x, treatment_n, 0.05
        )
        difference = treatment_x / treatment_n - control_x / control_n
        assert -1.0 <= low <= difference <= high <= 1.0


def test_newcombe_wilson_interval_alpha_edges():
    for alpha in (0.001, 0.5):
        low, high = newcombe_wilson_interval(8, 800, 14, 800, alpha)
        assert -1.0 <= low < high <= 1.0
    narrow = newcombe_wilson_interval(8, 800, 14, 800, 0.5)
    wide = newcombe_wilson_interval(8, 800, 14, 800, 0.001)
    assert narrow[0] > wide[0] and narrow[1] < wide[1]


def test_guardrail_status_three_state_contract():
    # 下界确认越界 -> breach
    assert guardrail_status(0.025, 0.055, 0.01) == "breach"
    # 上界确认安全 -> ok
    assert guardrail_status(0.0185, 0.0195, 0.02) == "ok"
    # 跨线（含小样本宽区间）-> watch
    assert guardrail_status(-0.01, 0.05, 0.02) == "watch"
    # 阈值相等时结果确定：触线不算证实，归为 watch
    assert guardrail_status(0.02, 0.03, 0.02) == "watch"
    assert guardrail_status(0.01, 0.02, 0.02) == "watch"
    assert guardrail_status(0.02, 0.02, 0.02) == "watch"


def test_degradation_interval_orientation_flip():
    assert degradation_interval("lower_is_better", -0.01, 0.05) == (-0.01, 0.05)
    assert degradation_interval("higher_is_better", -0.05, 0.01) == (-0.01, 0.05)


def test_guardrail_breach_needs_confirmed_interval(config):
    # 同一劣化点估计 0.04：小样本区间跨线只能 watch，大样本区间完全越界才是 breach
    small = _guardrail_result(config, 2, 50, 4, 50)
    assert small.degradation == pytest.approx(0.04)
    assert small.guardrail_status == "watch"
    assert small.ci_low < 0.01 < small.ci_high
    large = _guardrail_result(config, 80, 2000, 160, 2000)
    assert large.degradation == pytest.approx(0.04)
    assert large.guardrail_status == "breach"
    assert large.ci_low > 0.01


def test_guardrail_watch_for_uncertain_small_sample(config):
    # 任务反例：阈值 0.02，点劣化约 0.021，小样本宽区间 -> 不确定而非 fail
    result = _guardrail_result(config, 4, 200, 8, 195, max_degradation=0.02)
    assert result.degradation == pytest.approx(0.0210256, abs=1e-6)
    assert result.degradation > result.max_degradation
    assert result.guardrail_status == "watch"


def test_guardrail_ok_when_interval_safely_below_threshold(config):
    # 任务反例：点估计 0.019 接近阈值 0.03，但区间完全低于阈值 -> ok
    # （旧的“超过阈值一半”固定比例规则会把它错标为 watch）
    result = _guardrail_result(
        config, 400, 20000, 780, 20000, max_degradation=0.03
    )
    assert result.degradation == pytest.approx(0.019)
    assert result.degradation > 0.5 * result.max_degradation
    assert result.ci_high < result.max_degradation
    assert result.guardrail_status == "ok"


def test_guardrail_orientation_bounds_are_consistent(config):
    # 同一批“实验组更差”的数据，两种方向的劣化区间必须互为镜像且判级一致
    lib = _guardrail_result(config, 8, 800, 40, 800, orientation="lower_is_better")
    hib = _guardrail_result(
        config, 792, 800, 760, 800, orientation="higher_is_better",
        key="keep_rate",
    )
    assert lib.degradation == pytest.approx(hib.degradation)
    lib_low, lib_high = degradation_interval(
        lib.orientation, lib.ci_low, lib.ci_high
    )
    hib_low, hib_high = degradation_interval(
        hib.orientation, hib.ci_low, hib.ci_high
    )
    assert (lib_low, lib_high) == pytest.approx((hib_low, hib_high))
    assert (hib_low, hib_high) == pytest.approx((-hib.ci_high, -hib.ci_low))
    assert lib.guardrail_status == hib.guardrail_status == "breach"


def test_guardrail_extreme_proportions_are_deterministic(config):
    all_zero = _guardrail_result(config, 0, 50, 0, 50)
    assert all_zero.guardrail_status == "watch"
    all_crash_treatment = _guardrail_result(config, 0, 50, 50, 50)
    assert all_crash_treatment.guardrail_status == "breach"
    assert all_crash_treatment.ci_high <= 1.0
    no_crash_treatment = _guardrail_result(config, 50, 50, 0, 50)
    assert no_crash_treatment.guardrail_status == "ok"
    assert no_crash_treatment.ci_low >= -1.0
    single = _guardrail_result(config, 0, 1, 0, 1)
    assert single.guardrail_status == "watch"


def test_guardrail_proportion_ci_is_newcombe_not_wald(config):
    result = _guardrail_result(config, 8, 800, 14, 800)
    expected = newcombe_wilson_interval(8, 800, 14, 800, 0.05)
    assert (result.ci_low, result.ci_high) == pytest.approx(expected)
    # 区间天然落在可行范围内，不是 Wald 结果夹紧
    wald_low = 0.0075 - 1.959963986120195 * (
        (0.01 * 0.99 / 800 + 0.0175 * 0.9825 / 800) ** 0.5
    )
    assert result.ci_low != pytest.approx(wald_low)
    assert result.ci_low != max(-1.0, wald_low)


def test_primary_and_secondary_statistics_unchanged(config, dataset):
    primary = compute_metric(
        config, dataset.observations, config.primary, "primary"
    )
    assert primary.absolute_effect == pytest.approx(0.05)
    # 主指标区间保持原 Wald 结果（报告值按 10 位小数舍入）
    assert primary.ci_low == pytest.approx(round(0.006311044284254247, 10))
    assert primary.guardrail_status == "n/a"
    assert primary.degradation == 0.0


def test_mean_guardrail_uses_welch_interval(config):
    spec = MetricSpec(
        key="latency", column="latency", label="延迟", kind="mean",
        orientation="lower_is_better", alpha=0.05, max_degradation=0.5,
    )
    control = [1.0, 1.2, 0.8, 1.1, 0.9, 1.0, 1.3, 0.7]
    treatment = [1.4, 1.6, 1.2, 1.5, 1.3, 1.4, 1.7, 1.1]
    observations = [
        Observation(f"unit-c{i:04d}", config.experiment_id, "control", STAMP,
                    {"latency": value})
        for i, value in enumerate(control)
    ] + [
        Observation(f"unit-t{i:04d}", config.experiment_id, "treatment", STAMP,
                    {"latency": value})
        for i, value in enumerate(treatment)
    ]
    result = compute_metric(config, observations, spec, "guardrail")
    reference = welch_t_test(control, treatment, 0.05)
    assert (result.ci_low, result.ci_high) == pytest.approx(
        (reference["ci_low"], reference["ci_high"])
    )
    assert result.guardrail_status == "watch"


def test_mean_guardrail_zero_variance_at_threshold_is_deterministic(config):
    spec = MetricSpec(
        key="latency", column="latency", label="延迟", kind="mean",
        orientation="lower_is_better", alpha=0.05, max_degradation=0.5,
    )
    observations = [
        Observation(f"unit-c{i:04d}", config.experiment_id, "control", STAMP,
                    {"latency": 1.0})
        for i in range(5)
    ] + [
        Observation(f"unit-t{i:04d}", config.experiment_id, "treatment", STAMP,
                    {"latency": 1.5})
        for i in range(5)
    ]
    result = compute_metric(config, observations, spec, "guardrail")
    assert result.degradation == 0.5
    assert result.ci_low == result.ci_high == 0.5
    # 区间端点等于阈值：不证实越界也不证实安全，确定性地归为 watch
    assert result.guardrail_status == "watch"
    stricter = replace(spec, max_degradation=0.25)
    confirmed = compute_metric(config, observations, stricter, "guardrail")
    assert confirmed.guardrail_status == "breach"


def _stub_check(status):
    return Check("stub.check", "占位", status, "占位", {})


def _flags_for(guardrails, primary):
    quality_details = {"observation_coverage": 1.0}
    return build_flags(
        _stub_check("pass"), _stub_check("pass"), _stub_check("pass"),
        _stub_check("pass"), _stub_check("pass"), quality_details,
        primary, True, guardrails, False,
    )


def test_guardrail_check_maps_states_to_findings_and_flags(config, dataset):
    primary = compute_metric(
        config, dataset.observations, config.primary, "primary"
    )
    breach = _guardrail_result(config, 80, 2000, 160, 2000)
    watch = _guardrail_result(config, 2, 50, 4, 50)
    ok = _guardrail_result(config, 400, 20000, 780, 20000, max_degradation=0.03)

    check, findings = guardrail_check([ok])
    assert check.status == "pass"
    assert findings == []
    flags = _flags_for([ok], primary)
    assert flags["guardrail_breach"] is False
    assert flags["guardrail_watch"] is False

    check, findings = guardrail_check([watch])
    assert check.status == "warn"
    assert [item.severity for item in findings] == ["medium"]
    assert findings[0].code == f"guardrail.watch.{watch.key}"
    flags = _flags_for([watch], primary)
    assert flags["guardrail_breach"] is False
    assert flags["guardrail_watch"] is True

    check, findings = guardrail_check([breach, watch, ok])
    assert check.status == "fail"
    severities = {item.code: item.severity for item in findings}
    assert severities[f"guardrail.breach.{breach.key}"] == "critical"
    assert severities[f"guardrail.watch.{watch.key}"] == "medium"
    flags = _flags_for([breach, watch, ok], primary)
    assert flags["guardrail_breach"] is True
    assert flags["guardrail_watch"] is True
    # 详情中的劣化区间必须与指标区间按方向翻转后一致
    detail = next(
        item for item in check.details["metrics"] if item["key"] == breach.key
    )
    assert detail["degradation_ci"] == pytest.approx(
        [breach.ci_low, breach.ci_high]
    )


def test_audit_breach_drives_stop_and_consistent_outputs(config, dataset):
    # 把处理组 26 条未崩溃观测改为崩溃：14/800 -> 40/800，劣化区间完全越界
    observations = []
    flipped = 0
    for row in dataset.observations:
        if (
            row.experiment_id == config.experiment_id
            and row.variant == "treatment"
            and row.values["crash_rate"] == 0.0
            and flipped < 26
        ):
            observations.append(
                replace(row, values={**row.values, "crash_rate": 1.0})
            )
            flipped += 1
        else:
            observations.append(row)
    assert flipped == 26
    result = run_audit(
        config, dataset.assignments, observations,
        run_id="run-test-guardrail-breach",
        generated_at="2026-03-16T00:00:00+00:00",
        input_digest="0" * 64, config_digest="0" * 64,
    )
    guardrail = result["metrics"]["guardrails"][0]
    assert guardrail["guardrail_status"] == "breach"
    assert guardrail["degradation"] == pytest.approx(0.04)
    check = next(
        item for item in result["checks"] if item["code"] == "guardrail.status"
    )
    assert check["status"] == "fail"
    detail = check["details"]["metrics"][0]
    assert detail["degradation_ci"] == pytest.approx(
        [guardrail["ci_low"], guardrail["ci_high"]]
    )
    breach_finding = next(
        item for item in result["findings"]
        if item["code"] == "guardrail.breach.crash_rate"
    )
    assert breach_finding["severity"] == "critical"
    assert result["flags"]["guardrail_breach"] is True
    assert result["decision"]["card"] == "stop"
    assert result["decision"]["matched_rule"] == "stop-negative-or-guardrail"
    markdown = render_markdown(result)
    assert "越界" in markdown
    html = render_html(result)
    assert "越界" in html and "停止" in html


def test_audit_ok_guardrail_clears_guardrail_findings(config, dataset):
    relaxed = replace(
        config,
        guardrails=(replace(config.guardrails[0], max_degradation=0.03),),
    )
    result = run_audit(
        relaxed, dataset.assignments, dataset.observations,
        run_id="run-test-guardrail-ok",
        generated_at="2026-03-16T00:00:00+00:00",
        input_digest="0" * 64, config_digest="0" * 64,
    )
    guardrail = result["metrics"]["guardrails"][0]
    assert guardrail["guardrail_status"] == "ok"
    check = next(
        item for item in result["checks"] if item["code"] == "guardrail.status"
    )
    assert check["status"] == "pass"
    assert not any(
        item["code"].startswith("guardrail.") for item in result["findings"]
    )
    assert result["flags"]["guardrail_breach"] is False
    assert result["flags"]["guardrail_watch"] is False
    markdown = render_markdown(result)
    assert "正常" in markdown
