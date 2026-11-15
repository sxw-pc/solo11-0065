"""指标统计：把观测转换成可复核的效应、区间与护栏状态。

主指标若声明了实验前协变量（``MetricSpec.baseline``），则在配对样本上做
CUPED 调整。身份权威始终是分配记录 ``(experiment_id, unit_id)``；基线文件只
按 ``(experiment_id, unit_id, metric_key)`` 关联，不使用治疗标签。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from typing import Iterable

from .models import (Assignment, BaselineObservation, CUPEDAdjustment, ExperimentConfig,
                     MetricResult, MetricSpec, Observation)
from .stats import cuped_estimate, two_proportion_test, welch_t_test

GUARDRAIL_WATCH_RATIO = 0.5
THETA_SOURCE = "control_only"


def _round(value: float, digits: int = 10) -> float:
    rounded = round(value, digits)
    return 0.0 if abs(rounded) < 1e-12 else rounded


def _round_p(value: float) -> float:
    """p 值沿用旧的 10 位舍入，但不会把严格为正的极小 p 变成精确 0。"""
    value = max(0.0, min(1.0, float(value)))
    rounded = round(value, 10)
    if rounded == 0.0 and value > 0.0:
        return value
    return rounded


def comparison(
    control_values: list[float],
    treatment_values: list[float],
    kind: str,
    alpha: float,
) -> dict[str, float | bool]:
    if kind == "proportion":
        return two_proportion_test(
            int(round(sum(control_values))),
            len(control_values),
            int(round(sum(treatment_values))),
            len(treatment_values),
            alpha,
        )
    return welch_t_test(control_values, treatment_values, alpha)


def _group_values(
    observations: Iterable[Observation], spec: MetricSpec
) -> dict[str, list[float]]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for observation in observations:
        grouped[observation.variant].append(observation.values[spec.key])
    return grouped


def _sample_variance(values: list[float], mean: float) -> float:
    if len(values) < 2:
        return 0.0
    return sum((value - mean) ** 2 for value in values) / (len(values) - 1)


def _estimate_payload(result: dict[str, float | bool]) -> dict[str, object]:
    return {
        "control_value": _round(float(result["control_value"])),
        "treatment_value": _round(float(result["treatment_value"])),
        "absolute_effect": _round(float(result["absolute_effect"])),
        "ci_low": _round(float(result["ci_low"])),
        "ci_high": _round(float(result["ci_high"])),
        "p_value": _round_p(float(result["p_value"])),
        "significant": bool(result["significant"]),
    }


def _paired_samples(
    config: ExperimentConfig,
    observations: list[Observation],
    assignments: list[Assignment],
    baseline: list[BaselineObservation],
) -> tuple[dict[str, list[tuple[str, float, float]]], dict[str, object]]:
    """按分配身份构造 ``variant -> [(unit_id, X, Y)]`` 的配对样本。

    只会纳入同一 ``experiment_id`` 的分配、观测与基线记录；观测变体与
    分配不一致的记录不进入配对，交给数据一致性检查单独报告。
    """
    experiment_id = config.experiment_id
    assigned: dict[str, str] = {}
    for row in assignments:
        if row.experiment_id == experiment_id:
            assigned.setdefault(row.unit_id, row.variant)
    observed: dict[str, Observation] = {}
    for row in observations:
        if row.experiment_id != experiment_id:
            continue
        expected = assigned.get(row.unit_id)
        if expected is None or expected != row.variant:
            continue
        observed.setdefault(row.unit_id, row)
    baseline_by_unit: dict[str, float] = {}
    for row in baseline:
        if (
            row.experiment_id == experiment_id
            and row.metric_key == config.primary.key
        ):
            baseline_by_unit.setdefault(row.unit_id, row.value)

    paired_units = sorted(set(observed) & set(baseline_by_unit) & set(assigned))
    pairs: dict[str, list[tuple[str, float, float]]] = {
        variant.key: [] for variant in config.variants
    }
    for unit_id in paired_units:
        observation = observed[unit_id]
        pairs.setdefault(assigned[unit_id], []).append(
            (unit_id, baseline_by_unit[unit_id],
             observation.values[config.primary.key])
        )
    assigned_counts = {variant.key: 0 for variant in config.variants}
    for variant in assigned.values():
        assigned_counts[variant] = assigned_counts.get(variant, 0) + 1
    observed_counts = {variant.key: 0 for variant in config.variants}
    for unit_id in observed:
        variant = assigned[unit_id]
        observed_counts[variant] = observed_counts.get(variant, 0) + 1
    stats = {
        "assigned_units": len(assigned),
        "observed_units": len(observed),
        "paired_units": len(paired_units),
        "assigned_by_variant": assigned_counts,
        "observed_by_variant": observed_counts,
        "paired_by_variant": {key: len(value) for key, value in pairs.items()},
    }
    return pairs, stats


def _build_adjustment(
    config: ExperimentConfig,
    observations: list[Observation],
    assignments: list[Assignment] | None,
    baseline: list[BaselineObservation] | None,
    spec: MetricSpec,
    alpha: float,
) -> tuple[MetricResult | None, list[Observation] | None]:
    """构建 CUPED 调整结果。

    返回 ``(primary_result, analysis_observations)``；没有 baseline 时返回
    ``(None, None)``，由调用方走原有 Welch 路径。
    """
    spec_baseline = spec.baseline
    if spec_baseline is None:
        return None, None
    if assignments is None or baseline is None:
        raise ValueError(f"指标 {spec.key} 声明了 baseline，但缺少分配或基线数据")
    pairs, sample = _paired_samples(config, observations, assignments, baseline)
    treatment_key = next(
        variant.key for variant in config.variants if variant.key != config.control
    )
    control_pairs = pairs.get(config.control, [])
    treatment_pairs = pairs.get(treatment_key, [])
    # 覆盖门槛以“有结果、且分配有效的分析样本”为分母：这门控的是
    # 因缺 baseline 而无法配对的比例，而不是实验期观测覆盖本身。
    observed_units = int(sample["observed_units"])
    coverage = int(sample["paired_units"]) / observed_units if observed_units else 0.0
    coverage_by_variant = {
        key: (
            int(sample["paired_by_variant"][key]) / int(sample["observed_by_variant"][key])
            if int(sample["observed_by_variant"][key]) else 0.0
        )
        for key in sample["observed_by_variant"]
    }
    coverage_ok = coverage >= spec_baseline.min_coverage
    missing_units = observed_units - int(sample["paired_units"])

    raw_observations = [
        row for row in observations if row.experiment_id == config.experiment_id
    ]
    raw_grouped = _group_values(raw_observations, spec)
    raw_control = raw_grouped.get(config.control, [])
    raw_treatment = raw_grouped.get(treatment_key, [])
    if not raw_control or not raw_treatment:
        raise ValueError(f"指标 {spec.key} 缺少对照组或实验组观测")
    raw_result = comparison(raw_control, raw_treatment, spec.kind, alpha)
    raw_payload = _estimate_payload(raw_result)
    raw_payload.update({
        "control_n": len(raw_control),
        "treatment_n": len(raw_treatment),
        "control_variance": _round(_sample_variance(raw_control, float(raw_result["control_value"]))),
        "treatment_variance": _round(_sample_variance(raw_treatment, float(raw_result["treatment_value"]))),
    })

    status = "not_estimable"
    reason = "coverage_below_threshold"
    theta: float | None = None
    reference_mean: float | None = None
    adjusted_payload: dict[str, object] | None = None
    analysis_observations: list[Observation] | None = None

    if not coverage_ok:
        reason = "coverage_below_threshold"
    elif len(control_pairs) < 2 or len(treatment_pairs) < 2:
        reason = "insufficient_paired_units"
    else:
        estimate = cuped_estimate(
            [pair[1] for pair in control_pairs],
            [pair[2] for pair in control_pairs],
            [pair[1] for pair in treatment_pairs],
            [pair[2] for pair in treatment_pairs],
            alpha,
        )
        if not estimate["estimable"]:
            reason = "theta_not_estimable"
            reference_mean = float(estimate["reference_mean"])
        else:
            status = "adjusted"
            reason = "adjusted_on_paired_sample"
            theta = float(estimate["theta"])
            reference_mean = float(estimate["reference_mean"])
            estimate_result = estimate["result"]
            assert isinstance(estimate_result, dict)
            adjusted_payload = _estimate_payload(estimate_result)
            adjusted_payload.update({
                "control_n": len(control_pairs),
                "treatment_n": len(treatment_pairs),
                "control_variance": _round(_sample_variance(
                    [pair[2] - theta * (pair[1] - reference_mean) for pair in control_pairs],
                    float(estimate_result["control_value"]),
                )),
                "treatment_variance": _round(_sample_variance(
                    [pair[2] - theta * (pair[1] - reference_mean) for pair in treatment_pairs],
                    float(estimate_result["treatment_value"]),
                )),
            })
            adjustment_by_unit = {
                unit_id: y - theta * (x - reference_mean)
                for arms in pairs.values()
                for unit_id, x, y in arms
            }
            analysis_observations = []
            for row in observations:
                if row.experiment_id != config.experiment_id:
                    continue
                if row.unit_id not in adjustment_by_unit:
                    continue
                values = dict(row.values)
                values[spec.key] = adjustment_by_unit[row.unit_id]
                analysis_observations.append(replace(row, values=values))

    decision_statistic = "adjusted" if status == "adjusted" else "raw"
    chosen = adjusted_payload if decision_statistic == "adjusted" else raw_payload
    adjustment = CUPEDAdjustment(
        status=status,
        decision_statistic=decision_statistic,
        reason=reason,
        theta_source=THETA_SOURCE,
        theta=_round(theta) if theta is not None else None,
        reference_mean=_round(reference_mean) if reference_mean is not None else None,
        min_coverage=spec_baseline.min_coverage,
        coverage=round(coverage, 10),
        coverage_by_variant={key: round(value, 10) for key, value in coverage_by_variant.items()},
        coverage_ok=coverage_ok,
        assigned_units=int(sample["assigned_units"]),
        observed_units=observed_units,
        paired_units=int(sample["paired_units"]),
        missing_units=missing_units,
        paired_by_variant=dict(sample["paired_by_variant"]),
        baseline_file=spec_baseline.file,
        baseline_column=spec_baseline.column,
        raw=raw_payload,
        adjusted=adjusted_payload,
    )
    result = _metric_result(
        config, spec, "primary", chosen, alpha, adjustment=adjustment,
    )
    return result, analysis_observations if status == "adjusted" else observations


def _metric_result(
    config: ExperimentConfig,
    spec: MetricSpec,
    role: str,
    values: dict[str, object],
    alpha: float,
    *,
    adjustment: CUPEDAdjustment | None = None,
) -> MetricResult:
    effect = float(values["absolute_effect"])
    favorable = effect * spec.favorable_sign > 0.0
    relative = None
    control_value = float(values["control_value"])
    if abs(control_value) > 1e-12:
        relative = effect / abs(control_value)
    status = "n/a"
    degradation = 0.0
    if role == "guardrail":
        degradation = -effect if spec.orientation == "higher_is_better" else effect
        if degradation > spec.max_degradation:
            status = "breach"
        elif degradation > 0.0 and degradation > spec.max_degradation * GUARDRAIL_WATCH_RATIO:
            status = "watch"
        else:
            status = "ok"
    treatment_variants = [v.key for v in config.variants if v.key != config.control]
    treatment_key = treatment_variants[0]
    control_n = int(values.get("control_n", 0))
    treatment_n = int(values.get("treatment_n", 0))
    return MetricResult(
        key=spec.key,
        label=spec.label,
        role=role,
        kind=spec.kind,
        orientation=spec.orientation,
        control_label=config.variant_label(config.control),
        treatment_label=config.variant_label(treatment_key),
        control_n=control_n,
        treatment_n=treatment_n,
        control_sum=_round(control_value * control_n),
        treatment_sum=_round(float(values["treatment_value"]) * treatment_n),
        control_value=_round(control_value),
        treatment_value=_round(float(values["treatment_value"])),
        absolute_effect=_round(effect),
        relative_effect=_round(relative) if relative is not None else None,
        ci_low=_round(float(values["ci_low"])),
        ci_high=_round(float(values["ci_high"])),
        p_value=_round_p(float(values["p_value"])),
        alpha=alpha,
        significant=bool(values["significant"]),
        favorable=favorable,
        meets_threshold=bool(favorable and abs(effect) >= spec.min_effect),
        guardrail_status=status,
        degradation=_round(degradation),
        max_degradation=spec.max_degradation,
        adjustment=adjustment,
    )


def compute_metric(
    config: ExperimentConfig,
    observations: list[Observation],
    spec: MetricSpec,
    role: str,
    *,
    assignments: list[Assignment] | None = None,
    baseline: list[BaselineObservation] | None = None,
) -> MetricResult:
    if role == "primary" and spec.baseline is not None:
        adjusted, _ = _build_adjustment(
            config, observations, assignments, baseline, spec, spec.alpha
        )
        if adjusted is not None:
            return adjusted
    grouped = _group_values(observations, spec)
    control_values = grouped.get(config.control, [])
    treatment_variants = [v.key for v in config.variants if v.key != config.control]
    treatment_key = treatment_variants[0]
    treatment_values = grouped.get(treatment_key, [])
    if not control_values or not treatment_values:
        raise ValueError(f"指标 {spec.key} 缺少对照组或实验组观测")
    result = comparison(control_values, treatment_values, spec.kind, spec.alpha)
    values = _estimate_payload(result)
    values.update({
        "control_n": len(control_values),
        "treatment_n": len(treatment_values),
    })
    return _metric_result(config, spec, role, values, spec.alpha)


def compute_all_metrics(
    config: ExperimentConfig,
    observations: list[Observation],
    *,
    assignments: list[Assignment] | None = None,
    baseline: list[BaselineObservation] | None = None,
) -> tuple[MetricResult, list[MetricResult], list[MetricResult], list[Observation]]:
    """返回主指标、次级、护栏，以及用于主指标分层复核的分析观测。

    有 baseline 且调整成功时，分析观测是同一配对样本的调整后 Y；
    否则原样返回传入的观测，旧行为不变。
    """
    primary, analysis_observations = _build_adjustment(
        config, observations, assignments, baseline, config.primary, config.primary.alpha
    )
    if primary is None:
        primary = compute_metric(config, observations, config.primary, "primary")
        analysis_observations = observations
    secondary = [
        compute_metric(config, observations, spec, "secondary")
        for spec in config.secondary
    ]
    guardrails = [
        compute_metric(config, observations, spec, "guardrail")
        for spec in config.guardrails
    ]
    return primary, secondary, guardrails, analysis_observations
