"""指标统计：把观测转换成可复核的效应、区间与护栏状态。"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

from .models import ExperimentConfig, MetricResult, MetricSpec, Observation
from .stats import pooled_ratio_test, two_proportion_test, welch_t_test

GUARDRAIL_WATCH_RATIO = 0.5


def _round(value: float, digits: int = 10) -> float:
    rounded = round(value, digits)
    return 0.0 if abs(rounded) < 1e-12 else rounded


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


def ratio_comparison(
    control_pairs: list[tuple[float, float]],
    treatment_pairs: list[tuple[float, float]],
    alpha: float,
) -> dict[str, Any]:
    """pooled ratio：同一 unit 的 (numerator, denominator) 成对进入统计器。"""
    return pooled_ratio_test(control_pairs, treatment_pairs, alpha)


def _group_values(
    observations: Iterable[Observation], spec: MetricSpec
) -> dict[str, list[float]]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for observation in observations:
        grouped[observation.variant].append(observation.values[spec.key])
    return grouped


def _group_pairs(
    observations: Iterable[Observation], spec: MetricSpec
) -> dict[str, list[tuple[float, float]]]:
    grouped: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for observation in observations:
        # 同一行的分子分母作为不可拆开的样本对，不拆成两个独立检验。
        grouped[observation.variant].append(observation.pairs[spec.key])
    return grouped


def _guardrail_state(
    spec: MetricSpec, role: str, effect: float | None
) -> tuple[str, float | None]:
    if role != "guardrail":
        # 兼容旧契约：非护栏指标固定携带 degradation=0.0。
        return "n/a", 0.0
    if effect is None:
        return "not_estimable", None
    degradation = -effect if spec.orientation == "higher_is_better" else effect
    if degradation > spec.max_degradation:
        return "breach", degradation
    if degradation > 0.0 and degradation > spec.max_degradation * GUARDRAIL_WATCH_RATIO:
        return "watch", degradation
    return "ok", degradation


def _compute_ratio_metric(
    config: ExperimentConfig,
    observations: list[Observation],
    spec: MetricSpec,
    role: str,
    treatment_key: str,
) -> MetricResult:
    grouped = _group_pairs(observations, spec)
    control_pairs = grouped.get(config.control, [])
    treatment_pairs = grouped.get(treatment_key, [])
    if not control_pairs or not treatment_pairs:
        raise ValueError(f"指标 {spec.key} 缺少对照组或实验组观测")
    result = ratio_comparison(control_pairs, treatment_pairs, spec.alpha)
    if not result["estimable"]:
        # 整臂总分母为 0：不可估计，不能记为 0 或伪造显著性。
        status, _ = _guardrail_state(spec, role, None)
        return MetricResult(
            key=spec.key,
            label=spec.label,
            role=role,
            kind=spec.kind,
            orientation=spec.orientation,
            control_label=config.variant_label(config.control),
            treatment_label=config.variant_label(treatment_key),
            control_n=len(control_pairs),
            treatment_n=len(treatment_pairs),
            control_sum=None,
            treatment_sum=None,
            control_value=None,
            treatment_value=None,
            absolute_effect=None,
            relative_effect=None,
            ci_low=None,
            ci_high=None,
            p_value=None,
            alpha=spec.alpha,
            significant=False,
            favorable=False,
            meets_threshold=False,
            guardrail_status=status,
            degradation=None,
            max_degradation=spec.max_degradation,
            estimable=False,
            control_numerator_sum=_round(float(result["control_numerator_sum"])),
            control_denominator_sum=_round(float(result["control_denominator_sum"])),
            treatment_numerator_sum=_round(float(result["treatment_numerator_sum"])),
            treatment_denominator_sum=_round(
                float(result["treatment_denominator_sum"])
            ),
            control_zero_denominator_units=int(
                result["control_zero_denominator_units"]
            ),
            treatment_zero_denominator_units=int(
                result["treatment_zero_denominator_units"]
            ),
            control_effective_units=int(result["control_effective_units"]),
            treatment_effective_units=int(result["treatment_effective_units"]),
        )
    effect = float(result["absolute_effect"])
    favorable = effect * spec.favorable_sign > 0.0
    relative = None
    if abs(float(result["control_value"])) > 1e-12:
        relative = effect / abs(float(result["control_value"]))
    status, degradation = _guardrail_state(spec, role, effect)
    return MetricResult(
        key=spec.key,
        label=spec.label,
        role=role,
        kind=spec.kind,
        orientation=spec.orientation,
        control_label=config.variant_label(config.control),
        treatment_label=config.variant_label(treatment_key),
        control_n=len(control_pairs),
        treatment_n=len(treatment_pairs),
        control_sum=_round(float(result["control_numerator_sum"])),
        treatment_sum=_round(float(result["treatment_numerator_sum"])),
        control_value=_round(float(result["control_value"])),
        treatment_value=_round(float(result["treatment_value"])),
        absolute_effect=_round(effect),
        relative_effect=_round(relative) if relative is not None else None,
        ci_low=_round(float(result["ci_low"])),
        ci_high=_round(float(result["ci_high"])),
        p_value=_round(float(result["p_value"])),
        alpha=spec.alpha,
        significant=bool(result["significant"]),
        favorable=favorable,
        meets_threshold=bool(favorable and abs(effect) >= spec.min_effect),
        guardrail_status=status,
        degradation=_round(degradation) if degradation is not None else None,
        max_degradation=spec.max_degradation,
        estimable=True,
        control_numerator_sum=_round(float(result["control_numerator_sum"])),
        control_denominator_sum=_round(float(result["control_denominator_sum"])),
        treatment_numerator_sum=_round(float(result["treatment_numerator_sum"])),
        treatment_denominator_sum=_round(float(result["treatment_denominator_sum"])),
        control_zero_denominator_units=int(result["control_zero_denominator_units"]),
        treatment_zero_denominator_units=int(
            result["treatment_zero_denominator_units"]
        ),
        control_effective_units=int(result["control_effective_units"]),
        treatment_effective_units=int(result["treatment_effective_units"]),
    )


def compute_metric(
    config: ExperimentConfig,
    observations: list[Observation],
    spec: MetricSpec,
    role: str,
) -> MetricResult:
    treatment_variants = [v.key for v in config.variants if v.key != config.control]
    treatment_key = treatment_variants[0]
    if spec.is_ratio:
        return _compute_ratio_metric(
            config, observations, spec, role, treatment_key
        )
    grouped = _group_values(observations, spec)
    control_values = grouped.get(config.control, [])
    treatment_values = grouped.get(treatment_key, [])
    if not control_values or not treatment_values:
        raise ValueError(f"指标 {spec.key} 缺少对照组或实验组观测")
    result = comparison(control_values, treatment_values, spec.kind, spec.alpha)
    effect = float(result["absolute_effect"])
    favorable = effect * spec.favorable_sign > 0.0
    relative = None
    if abs(float(result["control_value"])) > 1e-12:
        relative = effect / abs(float(result["control_value"]))
    status, degradation = _guardrail_state(spec, role, effect)
    return MetricResult(
        key=spec.key,
        label=spec.label,
        role=role,
        kind=spec.kind,
        orientation=spec.orientation,
        control_label=config.variant_label(config.control),
        treatment_label=config.variant_label(treatment_key),
        control_n=len(control_values),
        treatment_n=len(treatment_values),
        control_sum=_round(sum(control_values)),
        treatment_sum=_round(sum(treatment_values)),
        control_value=_round(float(result["control_value"])),
        treatment_value=_round(float(result["treatment_value"])),
        absolute_effect=_round(effect),
        relative_effect=_round(relative) if relative is not None else None,
        ci_low=_round(float(result["ci_low"])),
        ci_high=_round(float(result["ci_high"])),
        p_value=_round(float(result["p_value"])),
        alpha=spec.alpha,
        significant=bool(result["significant"]),
        favorable=favorable,
        meets_threshold=bool(favorable and abs(effect) >= spec.min_effect),
        guardrail_status=status,
        degradation=_round(degradation) if degradation is not None else None,
        max_degradation=spec.max_degradation,
    )


def compute_all_metrics(
    config: ExperimentConfig, observations: list[Observation]
) -> tuple[MetricResult, list[MetricResult], list[MetricResult]]:
    primary = compute_metric(config, observations, config.primary, "primary")
    secondary = [
        compute_metric(config, observations, spec, "secondary")
        for spec in config.secondary
    ]
    guardrails = [
        compute_metric(config, observations, spec, "guardrail")
        for spec in config.guardrails
    ]
    return primary, secondary, guardrails
