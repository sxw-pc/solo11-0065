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


def metric_payload(item: Any) -> dict[str, Any]:
    """序列化指标结果；未配置 baseline 的旧结果保持原有字段集合。"""
    payload = asdict(item)
    if payload.get("adjustment") is None:
        payload.pop("adjustment", None)
    return payload


def primary_check(
    primary: Any, robust: bool, favorable_share: float
) -> tuple[Check, list[Finding]]:
    if primary.significant and not primary.favorable:
        status = "fail"
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
    }
    if primary.adjustment is not None:
        details["statistic"] = primary.adjustment.decision_statistic
        details["adjustment_status"] = primary.adjustment.status
        details["adjustment_reason"] = primary.adjustment.reason
    check = Check(
        "metric.primary", "主指标效应", status,
        f"{primary.label} 效应 {primary.absolute_effect:.4f}，"
        f"p={primary.p_value:.4f}，稳健={robust}", details,
    )
    findings: list[Finding] = []
    if primary.significant and not primary.favorable:
        findings.append(
            Finding(
                code="metric.negative_significant", severity="high", category="metric",
                title="主指标出现显著负向效应",
                detail="实验组主指标方向劣于对照组，且达到统计显著。",
                evidence=metric_payload(primary),
            )
        )
    elif primary.significant and primary.favorable and not primary.meets_threshold:
        findings.append(
            Finding(
                code="metric.below_threshold", severity="medium", category="metric",
                title="主指标效应未达到最小业务阈值",
                detail="效应方向为正，但绝对提升低于配置中的 min_effect。",
                evidence=metric_payload(primary),
            )
        )
    elif primary.favorable and primary.significant and not robust:
        findings.append(
            Finding(
                code="metric.fragile", severity="medium", category="metric",
                title="主指标效应不够稳健",
                detail="效应虽显著，但置信区间、阈值或分层一致性未全部通过。",
                evidence=metric_payload(primary),
            )
        )
    return check, findings


def baseline_check(primary: Any) -> tuple[Check | None, list[Finding]]:
    """把 CUPED 口径写入检查矩阵，确保回退时不会被误读为已调整。"""
    adjustment = getattr(primary, "adjustment", None)
    if adjustment is None:
        return None, []
    adjusted = adjustment.status == "adjusted"
    status = "pass" if adjusted else "warn"
    details = asdict(adjustment)
    decision_stat = adjustment.decision_statistic
    summary = (
        f"配对 {adjustment.paired_units}/{adjustment.observed_units} 个观测单元"
        f"（覆盖 {adjustment.coverage:.1%}，门槛 {adjustment.min_coverage:.0%}）；"
        f"决策统计 {decision_stat}"
    )
    if not adjusted:
        summary += f"；调整不可估计（{adjustment.reason}）"
    check = Check("baseline.adjustment", "实验前协变量调整", status, summary, details)
    findings: list[Finding] = []
    if not adjusted:
        findings.append(
            Finding(
                code="baseline.not_estimable",
                severity="medium",
                category="baseline",
                title="CUPED 调整不可估计，使用原始全量检验",
                detail=(
                    f"实验前协变量未能形成可用配对样本（原因：{adjustment.reason}）。"
                    "决策统计回退为全量未调整 Welch，结论不代表已做方差校正。"
                ),
                evidence=details,
            )
        )
    return check, findings


def guardrail_check(guardrails: list[Any]) -> tuple[Check, list[Finding]]:
    breaches = [item for item in guardrails if item.guardrail_status == "breach"]
    watches = [item for item in guardrails if item.guardrail_status == "watch"]
    status = "fail" if breaches else ("warn" if watches else "pass")
    details = {
        "metrics": [
            {"key": item.key, "label": item.label, "status": item.guardrail_status,
             "degradation": item.degradation,
             "max_degradation": item.max_degradation, "p_value": item.p_value}
            for item in guardrails
        ],
        "breaches": [item.key for item in breaches],
        "watches": [item.key for item in watches],
    }
    check = Check(
        "guardrail.status", "护栏指标", status,
        f"护栏越界 {len(breaches)} 项，临界 {len(watches)} 项", details,
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
                    evidence=metric_payload(item),
                )
            )
        elif item.guardrail_status == "watch":
            findings.append(
                Finding(
                    code=f"guardrail.watch.{item.key}", severity="medium",
                    category="guardrail", title=f"护栏指标接近阈值：{item.label}",
                    detail=f"劣化幅度 {item.degradation:.4%} 已达阈值 "
                           f"{item.max_degradation:.4%} 的一半以上。",
                    evidence=metric_payload(item),
                )
            )
    return check, findings


def metrics_payload(primary: Any, secondary: list[Any], guardrails: list[Any]) -> dict[str, Any]:
    return {
        "primary": metric_payload(primary),
        "secondary": [metric_payload(item) for item in secondary],
        "guardrails": [metric_payload(item) for item in guardrails],
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
                **(
                    {"baseline": {
                        "file": config.primary.baseline.file,
                        "column": config.primary.baseline.column,
                        "min_coverage": config.primary.baseline.min_coverage,
                    }}
                    if config.primary.baseline is not None else {}
                ),
            },
            "guardrails": [
                {"key": item.key, "label": item.label,
                 "max_degradation": item.max_degradation}
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
) -> dict[str, bool]:
    guardrail_breach = any(item.guardrail_status == "breach" for item in guardrails)
    guardrail_watch = any(item.guardrail_status == "watch" for item in guardrails)
    adjustment = getattr(primary, "adjustment", None)
    flags = {
        "split_balanced": split.status == "pass",
        "split_imbalanced": split.status != "pass",
        "duplicate_exposure": exposure.status != "pass",
        "no_duplicate_exposure": exposure.status == "pass",
        "mutex_conflict": mutex.status == "fail",
        "no_mutex_conflict": mutex.status == "pass",
        "data_quality_ok": quality.status == "pass",
        "data_quality_warning": quality.status == "warn",
        "data_quality_failed": quality.status == "fail",
        "coverage_ok": quality_details["observation_coverage"] >= 0.9
        and quality.status != "fail",
        "sample_sufficient": sample.status == "pass",
        "sample_insufficient": sample.status != "pass",
        "primary_positive_significant": primary.significant and primary.favorable,
        "primary_negative_significant": primary.significant and not primary.favorable,
        "primary_inconclusive": not primary.significant,
        "effect_meets_threshold": primary.meets_threshold,
        "primary_effect_robust": robust,
        "effect_fragile": primary.significant and primary.favorable and not robust,
        "stratified_reversal": reversal,
        "no_stratified_reversal": not reversal,
        "guardrail_breach": guardrail_breach,
        "no_guardrail_breach": not guardrail_breach,
        "guardrail_watch": guardrail_watch,
        "no_guardrail_watch": not guardrail_watch,
    }
    if adjustment is not None:
        adjusted = adjustment.status == "adjusted"
        flags["cuped_adjusted"] = adjusted
        flags["cuped_fallback_raw"] = not adjusted
    return flags
