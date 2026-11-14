"""分层复核：发现方向反转、异质性与小样本分层。"""

from __future__ import annotations

from collections import defaultdict

from .metrics import comparison
from .models import (
    Assignment,
    Check,
    ExperimentConfig,
    Finding,
    MetricResult,
    Observation,
    StratumResult,
)


def _field_groups(
    field: str,
    observations: list[Observation],
    assignments: dict[str, Assignment],
    limit: int,
) -> tuple[dict[str, list[Observation]], bool]:
    grouped: dict[str, list[Observation]] = defaultdict(list)
    for row in observations:
        assignment = assignments.get(row.unit_id)
        if assignment is not None:
            grouped[assignment.strata[field]].append(row)
    ordered = sorted(grouped, key=lambda value: (-len(grouped[value]), value))
    selected = set(ordered[:limit])
    return {value: grouped[value] for value in sorted(selected)}, len(ordered) > limit


def assess_stratification(
    config: ExperimentConfig,
    observations: list[Observation],
    assignments: list[Assignment],
    primary: MetricResult,
) -> tuple[list[dict[str, object]], dict[str, object], Check, list[Finding]]:
    assignment_map = {
        row.unit_id: row for row in assignments
        if row.experiment_id == config.experiment_id
    }
    treatment_key = next(
        variant.key for variant in config.variants if variant.key != config.control
    )
    fields_payload: list[dict[str, object]] = []
    all_groups: list[StratumResult] = []
    for field in config.stratification.fields:
        grouped, truncated = _field_groups(
            field, observations, assignment_map, config.stratification.max_groups
        )
        groups: list[StratumResult] = []
        for value, rows in grouped.items():
            control_values = [
                row.values[config.primary.key] for row in rows
                if row.variant == config.control
            ]
            treatment_values = [
                row.values[config.primary.key] for row in rows
                if row.variant == treatment_key
            ]
            eligible = (
                len(control_values) >= config.stratification.min_group_size
                and len(treatment_values) >= config.stratification.min_group_size
            )
            if eligible:
                result = comparison(
                    control_values, treatment_values,
                    config.primary.kind, config.stratification.alpha,
                )
                effect = float(result["absolute_effect"])
                p_value = float(result["p_value"])
                favorable = effect * config.primary.favorable_sign > 0.0
                reversal = (
                    abs(primary.absolute_effect) > 1e-12
                    and effect * primary.absolute_effect < 0.0
                    and p_value < config.stratification.alpha
                )
            else:
                effect = 0.0
                p_value = 1.0
                favorable = False
                reversal = False
            stratum = StratumResult(
                field=field,
                value=value,
                control_n=len(control_values),
                treatment_n=len(treatment_values),
                control_value=round(
                    sum(control_values) / len(control_values), 10
                ) if control_values else 0.0,
                treatment_value=round(
                    sum(treatment_values) / len(treatment_values), 10
                ) if treatment_values else 0.0,
                absolute_effect=round(effect, 10),
                p_value=round(p_value, 10),
                eligible=eligible,
                favorable=bool(eligible and favorable),
                reversal=reversal,
            )
            groups.append(stratum)
            all_groups.append(stratum)
        fields_payload.append(
            {
                "field": field,
                "values": len(grouped),
                "truncated": truncated,
                "groups": groups,
            }
        )
    eligible_groups = [group for group in all_groups if group.eligible]
    reversal_groups = [group for group in eligible_groups if group.reversal]
    favorable_count = sum(1 for group in eligible_groups if group.favorable)
    favorable_share = favorable_count / len(eligible_groups) if eligible_groups else 0.0
    opposite = [
        group for group in eligible_groups
        if not group.favorable and abs(group.absolute_effect) > 1e-12
    ]
    truncated_any = any(bool(field["truncated"]) for field in fields_payload)
    if reversal_groups:
        status = "fail"
    elif (
        not eligible_groups
        or favorable_share < config.stratification.min_favorable_share
        or opposite
        or truncated_any
    ):
        status = "warn"
    else:
        status = "pass"
    details: dict[str, object] = {
        "fields": fields_payload,
        "eligible_groups": len(eligible_groups),
        "reversal_groups": len(reversal_groups),
        "favorable_share": round(favorable_share, 8),
        "min_favorable_share": config.stratification.min_favorable_share,
        "opposite_groups": len(opposite),
        "truncated": truncated_any,
    }
    summary = (
        f"{len(eligible_groups)} 个可评估分层，方向反转 "
        f"{len(reversal_groups)} 个，同向占比 {favorable_share:.0%}"
    )
    check = Check("stratification.reversal", "分层稳健性", status, summary, details)
    findings: list[Finding] = []
    if reversal_groups:
        findings.append(
            Finding(
                code="stratification.reversal",
                severity="high",
                category="stratification",
                title="分层后出现显著方向反转",
                detail=(
                    "总体效应与至少一个足量分层的效应方向相反且达到显著，"
                    "上线前必须解释异质性来源。"
                ),
                evidence={
                    "groups": [
                        {
                            "field": group.field,
                            "value": group.value,
                            "effect": group.absolute_effect,
                            "p_value": group.p_value,
                            "control_n": group.control_n,
                            "treatment_n": group.treatment_n,
                        }
                        for group in reversal_groups
                    ]
                },
            )
        )
    elif status == "warn":
        findings.append(
            Finding(
                code="stratification.mixed_direction",
                severity="medium",
                category="stratification",
                title="分层证据不稳定",
                detail=(
                    "同向分层占比不足、存在方向相反的分层或分层被截断，"
                    "建议扩大样本后复核。"
                ),
                evidence={
                    "favorable_share": round(favorable_share, 8),
                    "opposite_groups": len(opposite),
                    "truncated": truncated_any,
                },
            )
        )
    return fields_payload, details, check, findings
