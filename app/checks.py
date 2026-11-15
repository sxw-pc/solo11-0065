"""分流健康检查：比例、重复曝光、互斥冲突与输入一致性。"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any, Iterable
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

def _activity_segments(rows: list[Assignment]) -> list[dict[str, Any]]:
    """把同一 (experiment_id, unit_id) 的分配记录合并为确定性的活动区间。

    语义（快照式 ended_at 路线）：

    - 每条记录是半开区间 ``[assigned_at, ended_at)``；``ended_at`` 为空表示开口
      区间（仍活动）。``ended_at < assigned_at`` 已在载入时拒绝；
      ``ended_at == assigned_at`` 是零长度区间，不产生任何重叠，直接丢弃。
    - 同一 unit 的后续分配会闭合前一段开口区间：开口记录的有效结束时刻取下一
      条 ``assigned_at`` 严格更大的记录的开始时刻（同一 unit 不能同时持有两段
      分配），推导出的结束时刻视为已知。
    - 同一时刻的多条记录取并集，开口记录优先（保守视为仍活动）。
    - 相接（前一段结束等于后一段开始）或相交的区间合并为一段；合并结果与输入
      行序无关。
    """
    ordered = sorted(
        rows,
        key=lambda row: (
            row.assigned_at,
            row.ended_at is None,
            row.ended_at or row.assigned_at,
            row.source_line or 0,
        ),
    )
    effective: list[dict[str, Any]] = []
    for index, row in enumerate(ordered):
        end = row.ended_at
        end_known = row.ended_at_known
        if end is None:
            later = next(
                (
                    other.assigned_at
                    for other in ordered[index + 1:]
                    if other.assigned_at > row.assigned_at
                ),
                None,
            )
            if later is not None:
                end = later
                end_known = True  # 由后续分配推导出的结束时刻
        if end is not None and end <= row.assigned_at:
            continue  # 零长度区间，不产生重叠
        effective.append(
            {
                "start": row.assigned_at,
                "end": end,
                "end_known": end_known,
                "lines": [row.source_line] if row.source_line else [],
            }
        )
    merged: list[dict[str, Any]] = []
    for segment in effective:
        if merged:
            last = merged[-1]
            if last["end"] is None or segment["start"] <= last["end"]:
                if last["end"] is not None and segment["end"] is not None:
                    last["end"] = max(last["end"], segment["end"])
                else:
                    last["end"] = None
                last["end_known"] = last["end_known"] and segment["end_known"]
                last["lines"] = sorted(last["lines"] + segment["lines"])
                continue
        merged.append(dict(segment))
    return merged


def _classify_segment(
    segment: dict[str, Any], window_start: datetime, window_end: datetime
) -> tuple[str, tuple[datetime, datetime] | None]:
    """把活动区间对本实验窗口 [window_start, window_end] 分类。

    返回 (state, overlap)：state 为 conflict / unknown / clear；overlap 仅在
    conflict 时给出交集起止。半开区间 [start, end) 与闭窗口相交的判定为
    ``start <= window_end`` 且 ``end > window_start``（开口区间 end 视为正无穷）。
    """
    start = segment["start"]
    end = segment["end"]
    if start > window_end:
        return "clear", None
    if end is not None and end <= window_start:
        return "clear", None
    if end is None and not segment["end_known"]:
        return "unknown", None
    overlap_end = min(end, window_end) if end is not None else window_end
    return "conflict", (max(start, window_start), overlap_end)


_MUTEX_STATE_ORDER = {"conflict": 0, "unknown": 1, "clear": 2}
_MUTEX_EVIDENCE_LIMIT = 20


def _segment_payload(
    segment: dict[str, Any],
    state: str,
    overlap: tuple[datetime, datetime] | None,
) -> dict[str, Any]:
    return {
        "assigned_at": segment["start"].isoformat(),
        "ended_at": segment["end"].isoformat() if segment["end"] is not None else None,
        "end_known": segment["end_known"],
        "state": state,
        "overlap_start": overlap[0].isoformat() if overlap else None,
        "overlap_end": overlap[1].isoformat() if overlap else None,
        "source_lines": list(segment["lines"]),
    }


def assess_mutex(
    config: ExperimentConfig, assignments: list[Assignment]
) -> tuple[Check, dict[str, object], list[Finding]]:
    """互斥检查：按跨实验参与区间与本实验窗口的交集判定。

    结论分三态：conflict（活动区间与 [config.start, config.end] 相交）、
    unknown（旧数据缺少 ended_at，无法证明不相交）、clear（可证明不相交）。
    不会把其它实验的同 unit 记录折叠成一个布尔值。
    """
    primary_units = {
        row.unit_id for row in _primary_assignments(config, assignments)
    }
    window = {"start": config.start.isoformat(), "end": config.end.isoformat()}
    unit_entries: list[dict[str, Any]] = []
    counts = {"conflict": 0, "unknown": 0, "clear": 0}
    conflict_evidence: list[tuple[Any, list[dict[str, Any]]]] = []
    unknown_evidence: list[tuple[Any, list[dict[str, Any]]]] = []
    for mutex in config.mutexes:
        rows_by_unit: dict[str, list[Assignment]] = {}
        for row in assignments:
            if row.experiment_id == mutex.experiment_id and row.unit_id in primary_units:
                rows_by_unit.setdefault(row.unit_id, []).append(row)
        conflict_units: list[dict[str, Any]] = []
        unknown_units: list[dict[str, Any]] = []
        for unit_id in sorted(rows_by_unit):
            segments = _activity_segments(rows_by_unit[unit_id])
            classified = [
                (segment, *_classify_segment(segment, config.start, config.end))
                for segment in segments
            ]
            state = "clear"
            for _, segment_state, _ in classified:
                if _MUTEX_STATE_ORDER[segment_state] < _MUTEX_STATE_ORDER[state]:
                    state = segment_state
            entry = {
                "unit_id": unit_id,
                "experiment_id": mutex.experiment_id,
                "state": state,
                "segments": [
                    _segment_payload(segment, segment_state, overlap)
                    for segment, segment_state, overlap in classified
                ],
            }
            counts[state] += 1
            unit_entries.append(entry)
            if state == "conflict":
                conflict_units.append(entry)
            elif state == "unknown":
                unknown_units.append(entry)
        if conflict_units:
            conflict_evidence.append((mutex, conflict_units))
        if unknown_units:
            unknown_evidence.append((mutex, unknown_units))
    unit_entries.sort(
        key=lambda entry: (
            _MUTEX_STATE_ORDER[entry["state"]],
            entry["experiment_id"],
            entry["unit_id"],
        )
    )
    status = "pass"
    if counts["unknown"]:
        status = "warn"
    if counts["conflict"]:
        status = "fail"
    details: dict[str, Any] = {
        "configured": [
            {"experiment_id": item.experiment_id, "label": item.label}
            for item in config.mutexes
        ],
        "window": window,
        "conflict_units": counts["conflict"],
        "unknown_units": counts["unknown"],
        "clear_units": counts["clear"],
        "units": unit_entries[:_MUTEX_EVIDENCE_LIMIT],
        "units_truncated": len(unit_entries) > _MUTEX_EVIDENCE_LIMIT,
    }
    summary_parts = []
    if counts["conflict"]:
        summary_parts.append(f"{counts['conflict']} 个 unit 参与区间相交")
    if counts["unknown"]:
        summary_parts.append(f"{counts['unknown']} 个 unit 参与区间未知")
    if summary_parts:
        summary = "；".join(summary_parts)
    else:
        summary = "未发现互斥实验参与区间重叠"
    if counts["clear"]:
        summary += f"；{counts['clear']} 个历史参与已结束"
    check = Check("mutex.conflict", "互斥实验", status, summary, details)
    findings: list[Finding] = []
    for mutex, units in conflict_evidence:
        example = units[0]
        overlap_segment = next(
            segment for segment in example["segments"] if segment["state"] == "conflict"
        )
        lines = ", ".join(str(line) for line in overlap_segment["source_lines"]) or "未知"
        findings.append(
            Finding(
                code="mutex.conflict",
                severity="critical",
                category="mutex",
                title=f"互斥实验发生参与区间重叠：{mutex.label}",
                detail=(
                    f"{len(units)} 个 unit 的活动区间与互斥实验 "
                    f"{mutex.experiment_id} 在本实验窗口 "
                    f"{window['start']}–{window['end']} 内相交。示例："
                    f"{example['unit_id']} 的区间 "
                    f"[{overlap_segment['assigned_at']}, "
                    f"{overlap_segment['ended_at'] or '开口'}] 与窗口交集为 "
                    f"[{overlap_segment['overlap_start']}, "
                    f"{overlap_segment['overlap_end']}]（源行 {lines}）。"
                ),
                evidence={
                    "experiment_id": mutex.experiment_id,
                    "label": mutex.label,
                    "window": window,
                    "units": units[:_MUTEX_EVIDENCE_LIMIT],
                    "truncated": len(units) > _MUTEX_EVIDENCE_LIMIT,
                },
            )
        )
    for mutex, units in unknown_evidence:
        findings.append(
            Finding(
                code="mutex.membership_unknown",
                severity="high",
                category="mutex",
                title=f"互斥参与区间无法确认：{mutex.label}",
                detail=(
                    f"{len(units)} 个 unit 在互斥实验 {mutex.experiment_id} 的"
                    "参与结束时刻未知：分配 CSV 缺少 ended_at 列，无法证明其参与"
                    "未与本实验窗口重叠。按 membership_unknown 处理：阻断可上线"
                    "结论，但不判定存在实际重叠。"
                ),
                evidence={
                    "experiment_id": mutex.experiment_id,
                    "label": mutex.label,
                    "window": window,
                    "units": units[:_MUTEX_EVIDENCE_LIMIT],
                    "truncated": len(units) > _MUTEX_EVIDENCE_LIMIT,
                },
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
