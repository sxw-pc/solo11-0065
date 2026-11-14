"""分流健康检查：比例、重复曝光、互斥冲突与输入一致性。"""

from __future__ import annotations

from collections import Counter
from typing import Iterable
from .models import Assignment, Check, ExperimentConfig, Finding, Observation
from .stats import chi_square_gof


def _primary_assignments(
    config: ExperimentConfig, assignments: Iterable[Assignment]
) -> list[Assignment]:
    return [row for row in assignments if row.experiment_id == config.experiment_id]


def _primary_observations(
    config: ExperimentConfig, observations: Iterable[Observation]
) -> list[Observation]:
    return [row for row in observations if row.experiment_id == config.experiment_id]


def assess_split(
    config: ExperimentConfig, assignments: list[Assignment]
) -> tuple[Check, dict[str, object], list[Finding]]:
    rows = _primary_assignments(config, assignments)
    by_unit: dict[str, Assignment] = {}
    for row in rows:
        by_unit.setdefault(row.unit_id, row)
    counts = {variant.key: 0 for variant in config.variants}
    for row in by_unit.values():
        counts[row.variant] = counts.get(row.variant, 0) + 1
    total = len(by_unit)
    shares = {
        key: (counts[key] / total if total else 0.0) for key in counts
    }
    deviations = {
        key: abs(shares[key] - config.split.expected[key]) for key in counts
    }
    chi = chi_square_gof(counts, config.split.expected) if total else {
        "chi_square": 0.0, "degrees_of_freedom": 0, "p_value": 1.0,
        "residuals": {}, "total": 0,
    }
    tolerance_breach = any(
        deviations[key] > config.split.tolerance + 1e-12 for key in counts
    )
    sample_ok = total >= config.split.min_assignments
    p_value = float(chi["p_value"])
    if not sample_ok or tolerance_breach:
        status = "fail"
    elif p_value < 0.05:
        status = "warn"
    else:
        status = "pass"
    summary = (
        f"{total} 个唯一分配单元，最大比例偏差 "
        f"{max(deviations.values(), default=0.0):.2%}，卡方 p={p_value:.4f}"
    )
    details = {
        "counts": counts,
        "shares": {key: round(value, 8) for key, value in shares.items()},
        "expected": config.split.expected,
        "deviations": {key: round(value, 8) for key, value in deviations.items()},
        "tolerance": config.split.tolerance,
        "min_assignments": config.split.min_assignments,
        "chi_square": round(float(chi["chi_square"]), 8),
        "p_value": round(p_value, 10),
        "residuals": {
            key: round(float(value), 8)
            for key, value in dict(chi["residuals"]).items()
        },
    }
    check = Check("split.balance", "分流比例", status, summary, details)
    findings: list[Finding] = []
    if status != "pass":
        findings.append(
            Finding(
                code="split.imbalance",
                severity="high" if status == "fail" else "medium",
                category="split",
                title="分流比例失衡",
                detail=(
                    f"唯一分配单元 {total} 个；比例偏差超过容差或卡方检验提示异常。"
                    "请核查随机分桶与流量截断。"
                ),
                evidence=details,
            )
        )
    return check, details, findings


def assess_exposure(
    config: ExperimentConfig, assignments: list[Assignment], observations: list[Observation]
) -> tuple[Check, dict[str, object], list[Finding]]:
    assignment_counts = Counter(
        row.unit_id for row in _primary_assignments(config, assignments)
    )
    observation_counts = Counter(
        row.unit_id for row in _primary_observations(config, observations)
    )
    assignment_duplicates = {
        unit: count for unit, count in assignment_counts.items()
        if count > config.exposure.max_per_unit
    }
    observation_duplicates = {
        unit: count for unit, count in observation_counts.items()
        if count > config.exposure.max_per_unit
    }
    extra_assignments = sum(
        count - config.exposure.max_per_unit for count in assignment_duplicates.values()
    )
    extra_exposures = sum(
        count - config.exposure.max_per_unit for count in observation_duplicates.values()
    )
    total_extra = extra_assignments + extra_exposures
    status = "fail" if total_extra > config.exposure.tolerance else "pass"
    details = {
        "max_per_unit": config.exposure.max_per_unit,
        "tolerance": config.exposure.tolerance,
        "duplicate_assignment_units": len(assignment_duplicates),
        "duplicate_exposure_units": len(observation_duplicates),
        "extra_assignments": extra_assignments,
        "extra_exposures": extra_exposures,
        "sample_units": sorted(observation_duplicates)[:10],
    }
    summary = (
        f"重复分配单元 {len(assignment_duplicates)} 个，"
        f"超出 {config.exposure.max_per_unit} 次曝光的单元 {len(observation_duplicates)} 个"
    )
    check = Check("exposure.duplicate", "重复曝光", status, summary, details)
    findings: list[Finding] = []
    if status == "fail":
        findings.append(
            Finding(
                code="exposure.duplicate",
                severity="high",
                category="exposure",
                title="存在重复分配或重复曝光",
                detail=(
                    "同一 unit 被多次分配或产生多条曝光记录，指标权重会失真。"
                    "应先按去重后的首条曝光复核结论。"
                ),
                evidence=details,
            )
        )
    return check, details, findings

def assess_mutex(
    config: ExperimentConfig, assignments: list[Assignment]
) -> tuple[Check, dict[str, object], list[Finding]]:
    primary_units = {
        row.unit_id for row in _primary_assignments(config, assignments)
    }
    conflicts: dict[str, list[str]] = {}
    conflict_units = 0
    for mutex in config.mutexes:
        overlap = sorted(
            primary_units
            & {row.unit_id for row in assignments
               if row.experiment_id == mutex.experiment_id}
        )
        if overlap:
            conflict_units += len(overlap)
            conflicts[mutex.experiment_id] = overlap[:20]
    status = "fail" if conflicts else "pass"
    details = {
        "configured": [
            {"experiment_id": item.experiment_id, "label": item.label}
            for item in config.mutexes
        ],
        "conflicts": conflicts,
        "conflict_units": conflict_units,
    }
    summary = (
        f"检查 {len(config.mutexes)} 个互斥实验，冲突 unit {details['conflict_units']} 个"
    )
    check = Check("mutex.conflict", "互斥实验", status, summary, details)
    findings: list[Finding] = []
    if conflicts:
        findings.append(
            Finding(
                code="mutex.conflict",
                severity="critical",
                category="mutex",
                title="互斥实验发生流量重叠",
                detail=(
                    "同一 unit 同时进入本实验与声明为互斥的实验，"
                    "组间差异可能被其他策略污染。需要人工复核分桶关系。"
                ),
                evidence=details,
            )
        )
    return check, details, findings


def assess_data_quality(
    config: ExperimentConfig,
    assignments: list[Assignment],
    observations: list[Observation],
) -> tuple[Check, dict[str, object], list[Finding]]:
    primary_assignments = _primary_assignments(config, assignments)
    primary_observations = _primary_observations(config, observations)
    assignment_by_unit: dict[str, Assignment] = {}
    for row in primary_assignments:
        assignment_by_unit.setdefault(row.unit_id, row)
    observed_units = {row.unit_id for row in primary_observations}
    unassigned = sorted(observed_units - set(assignment_by_unit))
    variant_mismatch = sorted(
        {
            row.unit_id for row in primary_observations
            if row.unit_id in assignment_by_unit
            and assignment_by_unit[row.unit_id].variant != row.variant
        }
    )
    assignment_outside = sorted(
        row.unit_id for row in primary_assignments
        if row.assigned_at < config.start or row.assigned_at > config.end
    )
    observation_outside = sorted(
        row.unit_id for row in primary_observations
        if row.observed_at < config.start or row.observed_at > config.end
    )
    assigned_units = len(assignment_by_unit)
    coverage = len(observed_units) / assigned_units if assigned_units else 0.0
    structural = bool(unassigned or variant_mismatch)
    time_issue = bool(assignment_outside or observation_outside)
    if structural or time_issue:
        status = "fail"
    elif coverage < 0.9:
        status = "warn"
    else:
        status = "pass"
    details = {
        "assigned_units": assigned_units,
        "observed_units": len(observed_units),
        "observation_coverage": round(coverage, 8),
        "unassigned_observation_units": len(unassigned),
        "variant_mismatch_units": len(variant_mismatch),
        "assignment_outside_window": len(assignment_outside),
        "observation_outside_window": len(observation_outside),
        "sample_units": {
            "unassigned": unassigned[:10],
            "variant_mismatch": variant_mismatch[:10],
        },
    }
    summary = (
        f"观测覆盖 {len(observed_units)}/{assigned_units} 个分配单元"
        f"（{coverage:.1%}），结构性异常 {len(unassigned) + len(variant_mismatch)} 个"
    )
    check = Check("data.quality", "数据一致性", status, summary, details)
    findings: list[Finding] = []
    if unassigned:
        findings.append(
            Finding(
                code="data.unassigned_observation",
                severity="high",
                category="data",
                title="观测记录缺少对应分配",
                detail=f"{len(unassigned)} 个 unit 的观测无法关联到分配记录。",
                evidence={"count": len(unassigned), "sample_units": unassigned[:20]},
            )
        )
    if variant_mismatch:
        findings.append(
            Finding(
                code="data.variant_mismatch",
                severity="high",
                category="data",
                title="分配与观测变体不一致",
                detail=f"{len(variant_mismatch)} 个 unit 的变体字段前后冲突。",
                evidence={"count": len(variant_mismatch),
                          "sample_units": variant_mismatch[:20]},
            )
        )
    if time_issue:
        findings.append(
            Finding(
                code="data.outside_window",
                severity="medium",
                category="data",
                title="存在实验窗口外记录",
                detail="部分分配或观测时间不在实验声明窗口内。",
                evidence={
                    "assignment_units": assignment_outside[:20],
                    "observation_units": observation_outside[:20],
                },
            )
        )
    if status == "warn":
        findings.append(
            Finding(
                code="data.low_coverage",
                severity="medium",
                category="data",
                title="观测覆盖率偏低",
                detail=f"观测覆盖率 {coverage:.1%}，低于 90% 建议阈值。",
                evidence=details,
            )
        )
    return check, details, findings
