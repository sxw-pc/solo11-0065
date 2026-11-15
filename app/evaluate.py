"""单项检查、flags 与审计结果序列化辅助。"""

from __future__ import annotations

import math
from dataclasses import asdict
from typing import Any

from .models import Check, ExperimentConfig, Finding, Observation


def sample_check(
    config: ExperimentConfig, observations: list[Observation]
) -> tuple[Check, list[Finding], dict[str, int]]:
    units_by_variant: dict[str, set[str]] = {v.key: set() for v in config.variants}
    for row in observations:
        units_by_variant[row.variant].add(row.unit_id)
    required = max(1, math.ceil(config.split.min_assignments / len(config.variants)))
    counts = {key: len(units) for key, units in units_by_variant.items()}
    insufficient = [key for key, count in counts.items() if count < required]
    status = "fail" if insufficient else "pass"
    details = {"unique_units": counts, "required_per_variant": required,
               "insufficient_variants": insufficient}
    check = Check(
        "sample.size", "样本量", status,
        f"每组要求至少 {required} 个唯一观测单元，实际 {counts}", details,
    )
    findings: list[Finding] = []
    if insufficient:
        findings.append(
            Finding(
                code="sample.underpowered", severity="medium", category="sample",
                title="每组观测样本不足",
                detail=f"变体 {', '.join(insufficient)} 低于每组最低样本要求。",
                evidence=details,
            )
        )
    return check, findings, counts


def primary_check(
    primary: Any, robust: bool, favorable_share: float
) -> tuple[Check, list[Finding]]:
    provisional = getattr(primary, "measurement_status", "legacy") == "provisional"
    not_computed = not getattr(primary, "computed", True)
    if primary.significant and not primary.favorable:
        status = "fail"
    elif provisional or not_computed:
        # 成熟子样本即使显著为正，也不能作为最终结论。
        status = "warn"
    elif robust:
        status = "pass"
    else:
        status = "warn"
    details = {
        "absolute_effect": primary.absolute_effect,
        "ci": [primary.ci_low, primary.ci_high],
        "p_value": primary.p_value, "significant": primary.significant,
        "favorable": primary.favorable, "meets_threshold": primary.meets_threshold,
        "robust": robust, "favorable_share": favorable_share,
        "measurement_status": getattr(primary, "measurement_status", "legacy"),
        "provisional": provisional,
        "computed": not not_computed,
        "insufficient_reason": getattr(primary, "insufficient_reason", ""),
        "control_n": primary.control_n, "treatment_n": primary.treatment_n,
    }
    check = Check(
        "metric.primary", "主指标效应", status,
        f"{primary.label} 效应 {primary.absolute_effect:.4f}，"
        f"p={primary.p_value:.4f}，稳健={robust}"
        + ("，结果尚未完全成熟（provisional）" if provisional else "")
        + (
            f"，{primary.insufficient_reason}"
            if not_computed and primary.insufficient_reason else ""
        ),
        details,
    )
    findings: list[Finding] = []
    if provisional and not (primary.significant and not primary.favorable):
        findings.append(
            Finding(
                code="metric.provisional", severity="medium", category="metric",
                title="主指标结论暂定（存在未成熟 cohort）",
                detail=(
                    "当前效应只来自成熟子样本；仍有已分配 unit 未走完 "
                    "outcome_horizon，不能据此给出最终上线结论。"
                ),
                evidence={
                    "measurement_status": primary.measurement_status,
                    "measurement": primary.measurement,
                },
            )
        )
    if not_computed and not (primary.significant and not primary.favorable):
        reason = getattr(primary, "insufficient_reason", "") or (
            "成熟观测不足以计算主指标。"
        )
        findings.append(
            Finding(
                code="metric.insufficient_mature_data", severity="medium",
                category="metric",
                title="成熟观测不足以计算主指标",
                detail=reason,
                evidence={
                    "measurement_status": primary.measurement_status,
                    "measurement": primary.measurement,
                },
            )
        )
    if primary.significant and not primary.favorable:
        findings.append(
            Finding(
                code="metric.negative_significant", severity="high", category="metric",
                title="主指标出现显著负向效应",
                detail="实验组主指标方向劣于对照组，且达到统计显著。",
                evidence=asdict(primary),
            )
        )
    elif primary.significant and primary.favorable and not primary.meets_threshold:
        findings.append(
            Finding(
                code="metric.below_threshold", severity="medium", category="metric",
                title="主指标效应未达到最小业务阈值",
                detail="效应方向为正，但绝对提升低于配置中的 min_effect。",
                evidence=asdict(primary),
            )
        )
    elif primary.favorable and primary.significant and not robust and not provisional:
        findings.append(
            Finding(
                code="metric.fragile", severity="medium", category="metric",
                title="主指标效应不够稳健",
                detail="效应虽显著，但置信区间、阈值或分层一致性未全部通过。",
                evidence=asdict(primary),
            )
        )
    return check, findings


def guardrail_check(guardrails: list[Any]) -> tuple[Check, list[Finding]]:
    breaches = [item for item in guardrails if item.guardrail_status == "breach"]
    watches = [item for item in guardrails if item.guardrail_status == "watch"]
    pending = [item for item in guardrails if item.guardrail_status == "pending"]
    if breaches:
        status = "fail"
    elif watches or pending:
        status = "warn"
    else:
        status = "pass"
    details = {
        "metrics": [
            {"key": item.key, "label": item.label, "status": item.guardrail_status,
             "degradation": item.degradation,
             "max_degradation": item.max_degradation, "p_value": item.p_value}
            for item in guardrails
        ],
        "breaches": [item.key for item in breaches],
        "watches": [item.key for item in watches],
        "pending": [item.key for item in pending],
    }
    check = Check(
        "guardrail.status", "护栏指标", status,
        f"护栏越界 {len(breaches)} 项，临界 {len(watches)} 项"
        + (f"，成熟度不足 {len(pending)} 项" if pending else ""), details,
    )
    findings: list[Finding] = []
    for item in guardrails:
        if item.guardrail_status == "breach":
            findings.append(
                Finding(
                    code=f"guardrail.breach.{item.key}", severity="critical",
                    category="guardrail", title=f"护栏指标越界：{item.label}",
                    detail=f"劣化幅度 {item.degradation:.4%} 超过阈值 "
                           f"{item.max_degradation:.4%}。",
                    evidence=asdict(item),
                )
            )
        elif item.guardrail_status == "pending":
            findings.append(
                Finding(
                    code=f"guardrail.pending.{item.key}", severity="medium",
                    category="guardrail", title=f"护栏指标成熟度不足：{item.label}",
                    detail=(
                        "该护栏的 outcome_horizon 尚未走完或成熟观测缺失，"
                        "当前「未越界」不能作为最终结论。"
                    ),
                    evidence={
                        "measurement_status": item.measurement_status,
                        "measurement": item.measurement,
                    },
                )
            )
        elif item.guardrail_status == "watch":
            findings.append(
                Finding(
                    code=f"guardrail.watch.{item.key}", severity="medium",
                    category="guardrail", title=f"护栏指标接近阈值：{item.label}",
                    detail=f"劣化幅度 {item.degradation:.4%} 已达阈值 "
                           f"{item.max_degradation:.4%} 的一半以上。",
                    evidence=asdict(item),
                )
            )
    return check, findings


def metrics_payload(primary: Any, secondary: list[Any], guardrails: list[Any]) -> dict[str, Any]:
    return {
        "primary": asdict(primary),
        "secondary": [asdict(item) for item in secondary],
        "guardrails": [asdict(item) for item in guardrails],
    }


def measurement_payload(
    metrics: list[dict[str, Any]], checks: list[dict[str, Any]]
) -> dict[str, Any]:
    """报告与台账共用的成熟度证据汇总。"""
    check = next(
        (item for item in checks if item.get("code") == "measurement.maturity"),
        None,
    )
    tracked = [
        item for item in metrics if item.get("outcome_horizon") is not None
    ]
    return {
        "tracked": bool(tracked),
        "metrics": metrics,
        "primary": next(
            (item for item in metrics if item.get("role") == "primary"),
            None,
        ),
        "check_status": check.get("status") if check else None,
        "check_summary": check.get("summary") if check else None,
    }


def config_payload(config: ExperimentConfig) -> dict[str, Any]:
    return {
        "schema_version": config.schema_version,
        "experiment": {
            "id": config.experiment_id, "name": config.name, "owner": config.owner,
            "hypothesis": config.hypothesis, "description": config.description,
            "start": config.start.isoformat(), "end": config.end.isoformat(),
        },
        "control": config.control,
        "variants": [asdict(variant) for variant in config.variants],
        "split": {
            "expected": config.split.expected, "tolerance": config.split.tolerance,
            "min_assignments": config.split.min_assignments,
        },
        "exposure": asdict(config.exposure),
        "mutual_exclusions": [asdict(item) for item in config.mutexes],
        "metrics": {
            "primary": {
                "key": config.primary.key, "label": config.primary.label,
                "min_effect": config.primary.min_effect, "alpha": config.primary.alpha,
                "outcome_horizon": config.primary.outcome_horizon,
            },
            "secondary": [
                {"key": item.key, "label": item.label,
                 "outcome_horizon": item.outcome_horizon}
                for item in config.secondary
            ],
            "guardrails": [
                {"key": item.key, "label": item.label,
                 "max_degradation": item.max_degradation,
                 "outcome_horizon": item.outcome_horizon}
                for item in config.guardrails
            ],
        },
        "stratification": {
            "fields": list(config.stratification.fields),
            "min_group_size": config.stratification.min_group_size,
            "min_favorable_share": config.stratification.min_favorable_share,
        },
    }


def build_flags(
    split: Check,
    exposure: Check,
    mutex: Check,
    quality: Check,
    sample: Check,
    quality_details: dict[str, Any],
    primary: Any,
    robust: bool,
    guardrails: list[Any],
    reversal: bool,
    measurement: dict[str, Any] | None = None,
) -> dict[str, bool]:
    guardrail_breach = any(item.guardrail_status == "breach" for item in guardrails)
    # 成熟度不足的护栏不能用「未越界」下结论，按 watch 处理。
    guardrail_watch = any(
        item.guardrail_status in ("watch", "pending") for item in guardrails
    )
    guardrail_pending = any(
        item.guardrail_status == "pending" for item in guardrails
    )
    measurement = measurement or {"tracked": False, "metrics": [], "primary": None}
    primary_measurement = measurement.get("primary") or {}
    tracked = bool(measurement.get("tracked"))
    primary_pending = int(primary_measurement.get("pending_units", 0) or 0) > 0
    primary_missing = (
        int(primary_measurement.get("mature_but_missing_units", 0) or 0) > 0
        or int(primary_measurement.get("observed_before_horizon_units", 0) or 0) > 0
    )
    any_pending = any(
        int(item.get("pending_units", 0) or 0) > 0
        for item in measurement.get("metrics", [])
        if item.get("outcome_horizon") is not None
    )
    measurement_status = getattr(primary, "measurement_status", "legacy")
    provisional = tracked and measurement_status == "provisional"
    measurement_coverage = float(
        primary_measurement.get("measurement_coverage", 0.0) or 0.0
    )
    # 配置了 outcome_horizon 时，coverage_ok 必须看每个 unit 的成熟结果，
    # 而不是「有没有任何一行观测」的全局覆盖率。
    observed_coverage = float(quality_details.get("observation_coverage", 0.0))
    coverage_for_flags = float(
        quality_details.get("coverage_for_flags", observed_coverage)
    )
    return {
        "split_balanced": split.status == "pass",
        "split_imbalanced": split.status != "pass",
        "duplicate_exposure": exposure.status != "pass",
        "no_duplicate_exposure": exposure.status == "pass",
        "mutex_conflict": mutex.status == "fail",
        "no_mutex_conflict": mutex.status == "pass",
        "data_quality_ok": quality.status == "pass",
        "data_quality_warning": quality.status == "warn",
        "data_quality_failed": quality.status == "fail",
        "coverage_ok": coverage_for_flags >= 0.9
        and quality.status != "fail"
        and (not tracked or measurement_coverage >= 0.9),
        "sample_sufficient": sample.status == "pass",
        "sample_insufficient": sample.status != "pass",
        "measurement_tracked": tracked,
        "measurement_complete": bool(
            tracked and measurement_status == "complete"
        ),
        "measurement_provisional": provisional,
        "measurement_pending": primary_pending,
        "measurement_incomplete": primary_missing,
        "measurement_missing": primary_missing,
        "any_measurement_pending": any_pending,
        "maturity_coverage_ok": (
            not tracked or measurement_coverage >= 0.9
        ),
        "primary_positive_significant": (
            primary.significant and primary.favorable and not provisional
        ),
        "primary_negative_significant": primary.significant and not primary.favorable,
        "primary_inconclusive": not primary.significant,
        "effect_meets_threshold": primary.meets_threshold and not provisional,
        "primary_effect_robust": robust and not provisional,
        "effect_fragile": primary.significant and primary.favorable and not robust,
        "stratified_reversal": reversal,
        "no_stratified_reversal": not reversal,
        "guardrail_breach": guardrail_breach,
        "no_guardrail_breach": not guardrail_breach,
        "guardrail_watch": guardrail_watch,
        "no_guardrail_watch": not guardrail_watch,
        "guardrail_measurement_pending": guardrail_pending,
    }
