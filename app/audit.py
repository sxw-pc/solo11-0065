"""审计编排：检查 -> 指标 -> 分层 -> flags -> 声明式决策。"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, replace
from typing import Any

from .checks import (
    assess_data_quality,
    assess_exposure,
    assess_mutex,
    assess_split,
)
from .config import ConfigError
from .decision import decide, referenced_flags
from .evaluate import (
    build_flags,
    config_payload,
    guardrail_check,
    metrics_payload,
    primary_check,
    sample_check,
)
from .identity import build_finding_identity
from .metrics import compute_all_metrics
from .models import (
    Check,
    ExperimentConfig,
    Finding,
    Observation,
    check_sort_key,
    finding_sort_key,
)
from .stratify import assess_stratification

KNOWN_FLAGS_HINT = "请检查 decision.rules 中的 flag 名称"


class AuditError(ValueError):
    """数据不足以完成审计。"""


def _fragile_finding(primary: Any) -> Finding:
    return Finding(
        code="metric.fragile", severity="medium", category="metric",
        title="主指标效应不够稳健",
        detail="效应显著，但未同时通过阈值、置信区间和分层一致性检查。",
        evidence=asdict(primary),
    )


def _with_stable_identity(findings: list[Finding]) -> list[Finding]:
    """为每条 finding 填充不依赖 run_id 与文案的稳定业务身份。"""
    enriched: list[Finding] = []
    for finding in findings:
        finding_key, evidence_summary = build_finding_identity(
            finding.code, finding.category, finding.evidence
        )
        enriched.append(
            replace(
                finding,
                finding_key=finding_key,
                evidence_summary=evidence_summary,
            )
        )
    return enriched


def run_audit(
    config: ExperimentConfig,
    assignments: list[Any],
    observations: list[Observation],
    *,
    run_id: str,
    generated_at: str,
    input_digest: str,
    config_digest: str,
) -> dict[str, Any]:
    primary_assignments = [
        row for row in assignments if row.experiment_id == config.experiment_id
    ]
    primary_observations = [
        row for row in observations if row.experiment_id == config.experiment_id
    ]
    if not primary_assignments:
        raise AuditError("没有找到本实验的分配记录")
    if not primary_observations:
        raise AuditError("没有找到本实验的曝光观测")

    checks: list[Check] = []
    findings: list[Finding] = []
    split_check, split_details, split_findings = assess_split(config, assignments)
    exposure_check, exposure_details, exposure_findings = assess_exposure(
        config, assignments, observations
    )
    mutex_check, mutex_details, mutex_findings = assess_mutex(config, assignments)
    quality_check, quality_details, quality_findings = assess_data_quality(
        config, assignments, observations
    )
    sample_check_result, sample_findings, _ = sample_check(
        config, primary_observations
    )
    checks.extend([
        split_check, exposure_check, mutex_check, quality_check, sample_check_result,
    ])
    findings.extend(
        split_findings + exposure_findings + mutex_findings
        + quality_findings + sample_findings
    )

    primary, secondary, guardrails = compute_all_metrics(config, primary_observations)
    (stratification_fields, stratification_details,
     stratification_check, stratification_findings) = assess_stratification(
        config, primary_observations, assignments, primary
    )
    checks.append(stratification_check)
    findings.extend(stratification_findings)

    favorable_share = float(stratification_details["favorable_share"])
    ci_favorable = (
        primary.ci_low > 0.0
        if config.primary.orientation == "higher_is_better"
        else primary.ci_high < 0.0
    )
    robust = bool(
        primary.significant
        and primary.favorable
        and primary.meets_threshold
        and ci_favorable
        and stratification_details["reversal_groups"] == 0
        and stratification_details["eligible_groups"] > 0
        and favorable_share >= config.stratification.min_favorable_share
    )
    primary_check_result, primary_findings = primary_check(
        primary, robust, favorable_share
    )
    checks.append(primary_check_result)
    findings.extend(primary_findings)
    if primary.favorable and primary.significant and not robust:
        guarded_codes = {
            "stratification.reversal", "metric.fragile",
            "metric.below_threshold",
        }
        if not any(item.code in guarded_codes for item in findings):
            findings.append(_fragile_finding(primary))

    guardrail_check_result, guardrail_findings = guardrail_check(guardrails)
    checks.append(guardrail_check_result)
    findings.extend(guardrail_findings)
    reversal = bool(stratification_details["reversal_groups"])
    flags = build_flags(
        split_check, exposure_check, mutex_check, quality_check,
        sample_check_result, quality_details, primary, robust, guardrails, reversal,
    )
    referenced: set[str] = set()
    for rule in config.decision.rules:
        referenced |= referenced_flags(rule.condition)
    unknown = sorted(referenced - set(flags))
    if unknown:
        raise ConfigError(f"决策规则引用了未知 flag：{', '.join(unknown)}；{KNOWN_FLAGS_HINT}")

    decision = decide(config.decision, flags)
    checks.sort(key=check_sort_key)
    findings = _with_stable_identity(findings)
    findings.sort(key=finding_sort_key)
    severity_counts = Counter(item.severity for item in findings)
    observed_by_variant = {
        variant.key: len({
            row.unit_id for row in primary_observations if row.variant == variant.key
        })
        for variant in config.variants
    }
    counts = {
        "assignment_rows": len(primary_assignments),
        "unique_assignment_units": len({row.unit_id for row in primary_assignments}),
        "observation_rows": len(primary_observations),
        "unique_observation_units": len({row.unit_id for row in primary_observations}),
        "observed_units_by_variant": observed_by_variant,
        "duplicate_assignment_units": exposure_details["duplicate_assignment_units"],
        "duplicate_exposure_units": exposure_details["duplicate_exposure_units"],
        "mutex_conflict_units": mutex_details["conflict_units"],
        "observation_coverage": quality_details["observation_coverage"],
    }
    return {
        "schema_version": 1,
        "run": {
            "run_id": run_id,
            "generated_at": generated_at,
            "input_digest": input_digest,
            "config_digest": config_digest,
        },
        "experiment": config_payload(config),
        "counts": counts,
        "flags": dict(sorted(flags.items())),
        "checks": [asdict(check) for check in checks],
        "findings": [asdict(finding) for finding in findings],
        "finding_counts": dict(sorted(severity_counts.items())),
        "metrics": metrics_payload(primary, secondary, guardrails),
        "stratification": {
            "fields": [
                {
                    "field": field["field"], "values": field["values"],
                    "truncated": field["truncated"],
                    "groups": [asdict(group) for group in field["groups"]],
                }
                for field in stratification_fields
            ],
            **{
                key: value for key, value in stratification_details.items()
                if key != "fields"
            },
        },
        "decision": decision,
        "reproducibility": {
            "deterministic": True,
            "input_digest": input_digest,
            "config_digest": config_digest,
            "note": "除 run_id 与 generated_at 外，相同输入与配置应产生相同审计内容。",
        },
        "headline": (
            f"{config.name}：{decision['title']}；"
            f"{len(findings)} 条 finding，主指标效应 "
            f"{primary.absolute_effect:+.4f}"
        ),
    }
