"""分流健康检查：比例、重复曝光、互斥冲突与输入一致性。"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Iterable
from .models import Assignment, Check, ExperimentConfig, Finding, Observation
from .stats import chi_square_gof


PHASE_STATUS_ORDER = {"pass": 0, "unverified": 1, "warn": 2, "fail": 3}
WORST_PHASE_STATUS = {0: "pass", 1: "warn", 2: "warn", 3: "fail"}


def _primary_assignments(
    config: ExperimentConfig, assignments: Iterable[Assignment]
) -> list[Assignment]:
    return [row for row in assignments if row.experiment_id == config.experiment_id]


def _primary_observations(
    config: ExperimentConfig, observations: Iterable[Observation]
) -> list[Observation]:
    return [row for row in observations if row.experiment_id == config.experiment_id]


def assignment_phase_key(config: ExperimentConfig, row: Assignment) -> str | None:
    """按 assigned_at 的 UTC 半开区间归段；与 data 层共用同一映射函数。

    即使调用方构造的 Assignment 没有 phase_key，也会得到确定结果；
    反之，预计算的 phase_key 与时间不一致时以时间为准，避免各层口径分叉。
    """
    return config.split.phase_key_at(row.assigned_at)


def _phase_counts(
    config: ExperimentConfig, rows: list[Assignment]
) -> tuple[dict[str, dict[str, int]], list[str], list[str]]:
    """按阶段汇总唯一 unit，CSV 行序与重复行不影响结果。

    返回 (阶段 counts, 跨阶段重复 unit 的样本, 调度窗口外 unit)。
    跨阶段重复 unit 从全部阶段计数中剔除，另行报告，避免同一 unit
    因 CSV 行序被计入两个阶段或让阶段检查伪装通过。
    """
    by_unit: dict[str, list[Assignment]] = defaultdict(list)
    for row in rows:
        by_unit[row.unit_id].append(row)
    phase_keys = [phase.key for phase in config.split.phases]
    valid_phases = set(phase_keys)
    chosen_by_phase: dict[str, list[Assignment]] = {key: [] for key in phase_keys}
    conflicted: list[str] = []
    outside: list[str] = []
    for unit_id in sorted(by_unit):
        unit_rows = by_unit[unit_id]
        mapped_keys = {
            assignment_phase_key(config, row) for row in unit_rows
        } & valid_phases
        if not mapped_keys:
            outside.append(unit_id)
            continue
        if len(mapped_keys) > 1:
            conflicted.append(unit_id)
            continue
        phase_key = next(iter(mapped_keys))
        # 同阶段多条分配只计一次；同 variant 重复交由 exposure 检查。
        # 同阶段不同 variant 属于契约冲突，按 (assigned_at, variant) 稳定取首条。
        chosen = min(
            (row for row in unit_rows if assignment_phase_key(config, row) == phase_key),
            key=lambda row: (row.assigned_at, row.variant),
        )
        chosen_by_phase[phase_key].append(chosen)
    counts: dict[str, dict[str, int]] = {}
    for phase_key in phase_keys:
        tallies = {variant.key: 0 for variant in config.variants}
        for chosen in chosen_by_phase[phase_key]:
            tallies[chosen.variant] = tallies.get(chosen.variant, 0) + 1
        counts[phase_key] = tallies
    return counts, conflicted, outside


def _phase_chi(
    counts: dict[str, int], expected: dict[str, float], total: int
) -> tuple[dict[str, object], float]:
    """阶段内卡方；零期望变体单独处理，允许 100/0 这类 ramp 起点。"""
    if total <= 0:
        return (
            {
                "chi_square": 0.0,
                "degrees_of_freedom": max(1, len(expected) - 1),
                "p_value": 1.0,
                "residuals": {},
                "total": 0,
            },
            1.0,
        )
    violations = [
        key for key, probability in expected.items()
        if probability <= 0.0 and counts.get(key, 0) > 0
    ]
    positive = {key: probability for key, probability in expected.items()
                if probability > 0.0}
    if violations:
        # 声明为 0 的变体出现了真实流量：期望频数为 0，卡方统计量无定义；
        # 以 p=0 与显式违规列表表达确定性的失衡。
        return (
            {
                "chi_square": None,
                "degrees_of_freedom": max(1, len(expected) - 1),
                "p_value": 0.0,
                "residuals": {key: 0.0 for key in positive},
                "zero_expected_violations": sorted(violations),
                "total": total,
            },
            0.0,
        )
    if len(positive) <= 1:
        return (
            {
                "chi_square": 0.0,
                "degrees_of_freedom": 1,
                "p_value": 1.0,
                "residuals": {key: 0.0 for key in positive},
                "total": total,
            },
            1.0,
        )
    chi = chi_square_gof(
        {key: counts.get(key, 0) for key in positive}, positive
    )
    return chi, float(chi["p_value"])


def _assess_phased_split(
    config: ExperimentConfig, rows: list[Assignment]
) -> tuple[Check, dict[str, object], list[Finding]]:
    """逐阶段核验声明流量计划；最差阶段状态决定整体 split flag。"""
    counts_by_phase, conflicted, outside = _phase_counts(config, rows)
    tolerance = config.split.tolerance
    phase_payloads: list[dict[str, object]] = []
    findings: list[Finding] = []
    samples: list[dict[str, object]] = []
    overall_rank = 0
    worst_phase: str | None = None
    for phase in config.split.phases:
        counts = counts_by_phase[phase.key]
        total = sum(counts.values())
        shares = {
            key: (counts[key] / total if total else 0.0) for key in counts
        }
        deviations = {
            key: abs(shares[key] - phase.expected[key]) for key in counts
        }
        chi, p_value = _phase_chi(counts, phase.expected, total)
        tolerance_breach = any(
            deviations[key] > tolerance + 1e-12 for key in counts
        ) or bool(chi.get("zero_expected_violations"))
        sample_ok = total >= phase.min_assignments
        if not sample_ok:
            status = "unverified"
        elif tolerance_breach:
            status = "fail"
        elif p_value < 0.05:
            status = "warn"
        else:
            status = "pass"
        rank = PHASE_STATUS_ORDER[status]
        if rank > overall_rank:
            overall_rank = rank
            worst_phase = phase.key
        payload = {
            "key": phase.key,
            "start": phase.start.isoformat(),
            "end": phase.end.isoformat(),
            "expected": phase.expected,
            "min_assignments": phase.min_assignments,
            "counts": counts,
            "shares": {key: round(value, 8) for key, value in shares.items()},
            "deviations": {
                key: round(value, 8) for key, value in deviations.items()
            },
            "max_deviation": round(max(deviations.values(), default=0.0), 8),
            "tolerance": tolerance,
            "sample_ok": sample_ok,
            "tolerance_breach": tolerance_breach,
            "low_sample": not sample_ok,
            "total": total,
            "status": status,
            "chi_square": (
                None if chi["chi_square"] is None
                else round(float(chi["chi_square"]), 8)
            ),
            "zero_expected_violations": list(
                chi.get("zero_expected_violations", [])
            ),
            "degrees_of_freedom": int(chi["degrees_of_freedom"]),
            "p_value": round(p_value, 10),
            "residuals": {
                key: round(float(value), 8)
                for key, value in dict(chi["residuals"]).items()
            },
            "reason": _phase_reason(status, total, phase.min_assignments,
                                    tolerance_breach, p_value),
        }
        phase_payloads.append(payload)
        if status == "fail":
            findings.append(
                Finding(
                    code="split.phase_imbalance",
                    severity="high",
                    category="split",
                    title=f"阶段分流比例失衡：{phase.key}",
                    detail=(
                        f"阶段 {phase.key}（{phase.start.isoformat()} → "
                        f"{phase.end.isoformat()}）的唯一 unit {total} 个；"
                        f"实际比例偏离该阶段声明的分配计划超过容差 "
                        f"{tolerance:.2%}。汇总口径可能相互抵消，必须按阶段修复。"
                    ),
                    evidence=payload,
                )
            )
        elif status == "warn":
            findings.append(
                Finding(
                    code="split.phase_imbalance",
                    severity="medium",
                    category="split",
                    title=f"阶段分流比例提示异常：{phase.key}",
                    detail=(
                        f"阶段 {phase.key} 的比例偏差未超过容差，但卡方 "
                        f"p={p_value:.4f} 低于 0.05，需结合分桶实现复核。"
                    ),
                    evidence=payload,
                )
            )
        elif status == "unverified":
            findings.append(
                Finding(
                    code="split.phase_insufficient",
                    severity="medium",
                    category="split",
                    title=f"阶段样本不足，无法核验：{phase.key}",
                    detail=(
                        f"阶段 {phase.key} 只有 {total} 个唯一 unit，低于该阶段 "
                        f"最低可检验样本数 {phase.min_assignments}；"
                        "本阶段结论为未验证，不据此判定真实失衡。"
                    ),
                    evidence=payload,
                )
            )
    if conflicted:
        overall_rank = PHASE_STATUS_ORDER["fail"]
        if worst_phase is None:
            worst_phase = "cross-phase-duplicate"
        samples = _conflict_samples(config, rows, conflicted)
        findings.append(
            Finding(
                code="split.phase_reassignment",
                severity="high",
                category="split",
                title="unit 跨阶段重复分配",
                detail=(
                    f"{len(conflicted)} 个 unit 的分配时间落在多个流量阶段；"
                    "这些 unit 已从所有阶段计数中剔除，阶段比例检查不能据此通过。"
                ),
                evidence={
                    "count": len(conflicted),
                    "sample_units": conflicted[:20],
                    "samples": samples[:20],
                },
            )
        )
    status = WORST_PHASE_STATUS[overall_rank]
    phase_summary = "；".join(
        f"{item['key']} {item['status']}"
        for item in phase_payloads
    )
    summary = (
        f"{len(phase_payloads)} 个流量阶段逐段核验（{phase_summary}），"
        f"跨阶段重复 unit {len(conflicted)} 个"
    )
    details: dict[str, object] = {
        "mode": "phased",
        "phases": phase_payloads,
        "tolerance": tolerance,
        "worst_phase": worst_phase,
        "cross_phase_duplicate_units": len(conflicted),
        "cross_phase_duplicate_samples": samples,
        "outside_schedule_units": len(outside),
        "outside_schedule_samples": outside[:20],
    }
    check = Check("split.balance", "分流比例", status, summary, details)
    return check, details, findings


def _phase_reason(
    status: str, total: int, minimum: int, tolerance_breach: bool, p_value: float
) -> str:
    if status == "unverified":
        return f"唯一 unit {total} < 最低样本 {minimum}，结果未验证"
    if status == "fail":
        return "比例偏差超过阶段容差"
    if status == "warn":
        return f"卡方 p={p_value:.4f} < 0.05"
    return "比例偏差在容差内且卡方未提示异常"


def _conflict_samples(
    config: ExperimentConfig, rows: list[Assignment], conflicted: list[str]
) -> list[dict[str, object]]:
    wanted = set(conflicted)
    grouped: dict[str, list[Assignment]] = defaultdict(list)
    for row in rows:
        if row.unit_id in wanted:
            grouped[row.unit_id].append(row)
    samples: list[dict[str, object]] = []
    for unit_id in sorted(grouped):
        entries = sorted(
            grouped[unit_id],
            key=lambda row: (row.assigned_at, row.variant),
        )
        samples.append(
            {
                "unit_id": unit_id,
                "assignments": [
                    {
                        "variant": row.variant,
                        "assigned_at": row.assigned_at.isoformat(),
                        "phase": assignment_phase_key(config, row),
                    }
                    for row in entries
                ],
            }
        )
    return samples


def assess_split(
    config: ExperimentConfig, assignments: list[Assignment]
) -> tuple[Check, dict[str, object], list[Finding]]:
    rows = _primary_assignments(config, assignments)
    if config.split.phased:
        return _assess_phased_split(config, rows)
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
