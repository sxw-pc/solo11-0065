"""分流健康检查：比例、重复曝光、互斥冲突、输入一致性与结果成熟期。"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Iterable

from .metrics import assess_measurement
from .models import (
    Assignment,
    Check,
    ExperimentConfig,
    Finding,
    Observation,
)
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
    config: ExperimentConfig,
    assignments: list[Assignment],
    observations: list[Observation],
    *,
    snapshot_mode: bool = False,
) -> tuple[Check, dict[str, object], list[Finding]]:
    assignment_counts = Counter(
        row.unit_id for row in _primary_assignments(config, assignments)
    )
    if snapshot_mode:
        # 固定追踪窗下，同一 unit 在不同时间点产生多条快照是预期行为；
        # 只有同一时刻的重复行才算重复曝光。
        observation_counts = Counter(
            (row.unit_id, row.observed_at)
            for row in _primary_observations(config, observations)
        )
    else:
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
    duplicate_units = {
        unit[0] if isinstance(unit, tuple) else unit
        for unit in observation_duplicates
    }
    details = {
        "max_per_unit": config.exposure.max_per_unit,
        "tolerance": config.exposure.tolerance,
        "snapshot_mode": snapshot_mode,
        "duplicate_assignment_units": len(assignment_duplicates),
        "duplicate_exposure_units": len(duplicate_units),
        "extra_assignments": extra_assignments,
        "extra_exposures": extra_exposures,
        "sample_units": sorted(duplicate_units)[:10],
    }
    summary = (
        f"重复分配单元 {len(assignment_duplicates)} 个，"
        f"超出 {config.exposure.max_per_unit} 次曝光的单元 {len(duplicate_units)} 个"
        + ("（按同一时刻判重）" if snapshot_mode else "")
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
    *,
    generated_at: datetime | None = None,
    measurements: list[dict[str, object]] | None = None,
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
    tracked = measurements is not None and any(
        item.get("outcome_horizon") is not None for item in measurements
    )
    as_of = generated_at or config.end
    # 固定追踪窗允许在实验结束后才到齐的成熟结果：end 之后、run 之前到达的
    # 观测是迟到报告（不是异常）。未配置 horizon 时保持旧口径：以 end 为界。
    late_reports = sorted(
        row.unit_id for row in primary_observations
        if tracked and config.end < row.observed_at <= as_of
    )
    window_end = as_of if tracked else config.end
    observation_outside = sorted(
        row.unit_id for row in primary_observations
        if row.observed_at < config.start or row.observed_at > window_end
    )
    assigned_units = len(assignment_by_unit)
    coverage = len(observed_units) / assigned_units if assigned_units else 0.0
    structural = bool(unassigned or variant_mismatch)
    time_issue = bool(assignment_outside or observation_outside)
    maturity_coverage = _primary_maturity_coverage(measurements)
    primary_measure = _primary_measurement(measurements or [])
    if tracked:
        # 配置 horizon 后，pending cohort 尚未到应观察时间，不应拉低数据质量；
        # 只有「已到应观察时间」的 cohort 才进入数据质量覆盖率分母。
        mature = int(primary_measure.get("eligible_units", 0) or 0)
        pending = int(primary_measure.get("pending_units", 0) or 0)
        evaluable = int(primary_measure.get("denominator_units", 0) or 0) - pending
        quality_coverage = mature / evaluable if evaluable else 1.0
    else:
        quality_coverage = coverage
    if structural or time_issue:
        status = "fail"
    elif quality_coverage < 0.9:
        status = "warn"
    else:
        status = "pass"
    details: dict[str, object] = {
        "assigned_units": assigned_units,
        "observed_units": len(observed_units),
        "observation_coverage": round(coverage, 8),
        "coverage_for_flags": round(quality_coverage, 8),
        "unassigned_observation_units": len(unassigned),
        "variant_mismatch_units": len(variant_mismatch),
        "assignment_outside_window": len(assignment_outside),
        "observation_outside_window": len(observation_outside),
        "sample_units": {
            "unassigned": unassigned[:10],
            "variant_mismatch": variant_mismatch[:10],
        },
    }
    if tracked:
        # 全局 coverage 仍保留作输入完整性参考；成熟度覆盖率在
        # measurement.maturity 检查与各指标结果中单独给出。
        # data.quality 只嵌入紧凑汇总，完整 unit 明细在 audit.measurement。
        details["measurement"] = [
            {
                key: value for key, value in dict(item).items() if key != "units"
            }
            for item in (measurements or [])
        ]
        details["measurement_tracked"] = True
        details["maturity_coverage"] = maturity_coverage
        details["late_report_units"] = len(late_reports)
        details["sample_units"]["late_report"] = late_reports[:10]
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
        if tracked:
            detail = (
                f"已到应观察时间的 cohort 中，成熟观测覆盖 "
                f"{quality_coverage:.1%}，低于 90% 建议阈值；"
                "等待中的 cohort 不计入该分母。"
            )
        else:
            detail = f"观测覆盖率 {coverage:.1%}，低于 90% 建议阈值。"
        findings.append(
            Finding(
                code="data.low_coverage",
                severity="medium",
                category="data",
                title="观测覆盖率偏低",
                detail=detail,
                evidence=details,
            )
        )
    return check, details, findings


def _primary_measurement(measurements: list[dict[str, object]]) -> dict[str, object]:
    for item in measurements:
        if item.get("role") == "primary":
            return item
    return {}


def _primary_maturity_coverage(
    measurements: list[dict[str, object]] | None,
) -> float | None:
    if not measurements:
        return None
    primary = _primary_measurement(measurements)
    if primary.get("outcome_horizon") is None:
        return None
    return float(primary.get("measurement_coverage", 0.0))


def assess_measurement_quality(
    config: ExperimentConfig,
    measurements: list[dict[str, object]],
) -> tuple[Check, dict[str, object], list[Finding]]:
    """逐指标汇总成熟度 cohort，生成 pending / incomplete 证据。

    只统计配置了 ``outcome_horizon`` 的指标；没有 horizon 时调用方不应调用本函数。
    """
    tracked = [
        item for item in measurements if item.get("outcome_horizon") is not None
    ]
    primary = _primary_measurement(measurements) if tracked else {}
    if primary.get("outcome_horizon") is None:
        # 主指标没有 horizon 时，不应把它的旧口径字段混入成熟度证据。
        primary = {}
    pending_primary = int(primary.get("pending_units", 0) or 0)
    missing_primary = int(primary.get("mature_but_missing_units", 0) or 0)
    early_primary = int(primary.get("observed_before_horizon_units", 0) or 0)
    pending_any = sum(int(item.get("pending_units", 0) or 0) for item in tracked)
    missing_any = sum(
        int(item.get("mature_but_missing_units", 0) or 0)
        + int(item.get("observed_before_horizon_units", 0) or 0)
        for item in tracked
    )
    if pending_primary:
        status = "warn"
    elif missing_primary or early_primary:
        status = "warn"
    elif pending_any or missing_any:
        status = "warn"
    else:
        status = "pass"
    metric_details = [
        {
            "metric_key": item.get("metric_key"),
            "label": item.get("label"),
            "role": item.get("role"),
            "outcome_horizon": item.get("outcome_horizon"),
            "status": item.get("status"),
            "denominator_units": item.get("denominator_units", 0),
            "eligible_units": item.get("eligible_units", 0),
            "measurement_coverage": item.get("measurement_coverage", 0.0),
            "pending_units": item.get("pending_units", 0),
            "mature_but_missing_units": item.get("mature_but_missing_units", 0),
            "observed_before_horizon_units": item.get(
                "observed_before_horizon_units", 0
            ),
            "eligible_units_by_variant": item.get("eligible_units_by_variant", {}),
            "denominator_units_by_variant": item.get(
                "denominator_units_by_variant", {}
            ),
            "pending_cohorts": item.get("pending_cohorts", []),
            "as_of": item.get("as_of"),
        }
        for item in tracked
    ]
    details: dict[str, object] = {
        "tracked_metrics": len(tracked),
        "primary_metric": primary.get("metric_key") if tracked else None,
        "primary_status": primary.get("status") if tracked else "legacy",
        "primary_pending_units": pending_primary,
        "primary_mature_but_missing_units": missing_primary,
        "primary_observed_before_horizon_units": early_primary,
        "pending_units": pending_any,
        "incomplete_units": missing_any,
        "metrics": metric_details,
        # 逐指标紧凑汇总，避免检查 details 里重复嵌入 unit 明细。
        "metric_summaries": [
            {
                key: value for key, value in dict(item).items()
                if key != "units"
            }
            for item in tracked
        ],
    }
    if primary:
        summary = (
            f"主指标 {primary.get('metric_key')} 等待成熟 {pending_primary} 个 unit，"
            f"成熟缺数 {missing_primary} 个，成熟覆盖率 "
            f"{float(primary.get('measurement_coverage', 0.0)):.1%}"
        )
    elif tracked:
        summary = (
            f"{len(tracked)} 个指标配置了 outcome_horizon，"
            f"等待中 {pending_any} 个 unit，成熟缺数 {missing_any} 个"
        )
    else:
        summary = "未配置 outcome_horizon，沿用全部观测口径"
    check = Check("measurement.maturity", "结果成熟度", status, summary, details)
    findings: list[Finding] = []
    if tracked and pending_any:
        pending_metric_keys = [
            item.get("metric_key") for item in tracked
            if int(item.get("pending_units", 0) or 0) > 0
        ]
        pending_summary = "；".join(
            f"{item.get('label')} {int(item.get('pending_units', 0) or 0)}"
            for item in tracked
            if int(item.get("pending_units", 0) or 0) > 0
        )
        findings.append(
            Finding(
                code="measurement_pending",
                severity="medium",
                category="measurement",
                title="存在尚未成熟的结果 cohort",
                detail=(
                    f"按各指标自己的 horizon，等待中的 unit：{pending_summary}。"
                    "这些结果不计为零、也不写成永久缺数；"
                    "当前效应只代表成熟子样本，主指标仍有 pending 时决策只能是 provisional。"
                ),
                evidence={
                    "pending_units_by_metric": {
                        item.get("metric_key"): int(
                            item.get("pending_units", 0) or 0
                        )
                        for item in tracked
                    },
                    "metric_keys": pending_metric_keys,
                    "primary_pending_units": pending_primary,
                    "primary_pending_cohorts": (
                        primary.get("pending_cohorts", []) if pending_primary else []
                    ),
                },
            )
        )
    if tracked and (missing_any or early_primary):
        incomplete_by_metric = "；".join(
            f"{item.get('label')} 缺数 {int(item.get('mature_but_missing_units', 0) or 0)}、"
            f"早于窗口 {int(item.get('observed_before_horizon_units', 0) or 0)}"
            for item in tracked
            if int(item.get("mature_but_missing_units", 0) or 0)
            or int(item.get("observed_before_horizon_units", 0) or 0)
        )
        findings.append(
            Finding(
                code="measurement_incomplete",
                severity="medium",
                category="measurement",
                title="成熟结果不完整",
                detail=(
                    f"{incomplete_by_metric}。"
                    "成熟后仍缺数属于真实的观测流失（attrition），"
                    "不应与等待中的 cohort 混为一谈。"
                ),
                evidence={
                    "primary_mature_but_missing_units": missing_primary,
                    "primary_observed_before_horizon_units": early_primary,
                    "incomplete_by_metric": {
                        item.get("metric_key"): {
                            "mature_but_missing_units": int(
                                item.get("mature_but_missing_units", 0) or 0
                            ),
                            "observed_before_horizon_units": int(
                                item.get("observed_before_horizon_units", 0) or 0
                            ),
                        }
                        for item in tracked
                    },
                    "metrics": metric_details,
                    "metric_summaries": [
                        {
                            key: value for key, value in dict(item).items()
                            if key != "units"
                        }
                        for item in tracked
                    ],
                },
            )
        )
    return check, details, findings
