"""指标统计：把观测转换成可复核的效应、区间与护栏状态。"""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from .models import ExperimentConfig, MetricResult, MetricSpec, Observation
from .stats import newcombe_wilson_interval, two_proportion_test, welch_t_test


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


def _group_values(
    observations: Iterable[Observation], spec: MetricSpec
) -> dict[str, list[float]]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for observation in observations:
        grouped[observation.variant].append(observation.values[spec.key])
    return grouped


def guardrail_status(
    degradation_ci_low: float, degradation_ci_high: float, max_degradation: float
) -> str:
    """护栏三态证据契约：基于劣化区间与业务上限 max_degradation 的关系。

    - breach：劣化置信下界高于阈值，越界已被证实；
    - ok：劣化置信上界低于阈值，安全已被证实；
    - watch：区间跨过阈值（含样本不足导致的宽区间），证据不足。
    """
    if degradation_ci_low > max_degradation:
        return "breach"
    if degradation_ci_high < max_degradation:
        return "ok"
    return "watch"


def degradation_interval(
    orientation: str, ci_low: float, ci_high: float
) -> tuple[float, float]:
    """把（实验组-对照组）效应区间按指标方向翻转成劣化区间。

    higher_is_better 的劣化是效应取负，上下界随之互换；
    lower_is_better 的劣化就是效应本身，区间方向不变。
    """
    if orientation == "higher_is_better":
        return -ci_high, -ci_low
    return ci_low, ci_high


def compute_metric(
    config: ExperimentConfig,
    observations: list[Observation],
    spec: MetricSpec,
    role: str,
) -> MetricResult:
    grouped = _group_values(observations, spec)
    control_values = grouped.get(config.control, [])
    treatment_variants = [v.key for v in config.variants if v.key != config.control]
    treatment_key = treatment_variants[0]
    treatment_values = grouped.get(treatment_key, [])
    if not control_values or not treatment_values:
        raise ValueError(f"指标 {spec.key} 缺少对照组或实验组观测")
    result = comparison(control_values, treatment_values, spec.kind, spec.alpha)
    effect = float(result["absolute_effect"])
    favorable = effect * spec.favorable_sign > 0.0
    relative = None
    if abs(float(result["control_value"])) > 1e-12:
        relative = effect / abs(float(result["control_value"]))
    ci_low = float(result["ci_low"])
    ci_high = float(result["ci_high"])
    status = "n/a"
    degradation = 0.0
    if role == "guardrail":
        degradation = -effect if spec.orientation == "higher_is_better" else effect
        if spec.kind == "proportion":
            # Wald 差值区间在比例接近 0/1 或小样本时可能越出可行范围；
            # 护栏判级与报告区间改用 Newcombe-Wilson score 区间。
            ci_low, ci_high = newcombe_wilson_interval(
                int(round(sum(control_values))),
                len(control_values),
                int(round(sum(treatment_values))),
                len(treatment_values),
                spec.alpha,
            )
        degradation_ci_low, degradation_ci_high = degradation_interval(
            spec.orientation, ci_low, ci_high
        )
        status = guardrail_status(
            degradation_ci_low, degradation_ci_high, spec.max_degradation
        )
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
        ci_low=_round(ci_low),
        ci_high=_round(ci_high),
        p_value=_round(float(result["p_value"])),
        alpha=spec.alpha,
        significant=bool(result["significant"]),
        favorable=favorable,
        meets_threshold=bool(favorable and abs(effect) >= spec.min_effect),
        guardrail_status=status,
        degradation=_round(degradation),
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
