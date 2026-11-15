"""指标统计：按结果成熟期筛选观测，再转换成可复核的效应、区间与护栏状态。

结果成熟期口径（per-unit fixed horizon）
--------------------------------------
配置了 ``MetricSpec.outcome_horizon`` 时，每个 unit 的追踪窗从 ``assigned_at``
起算，成熟时点为 ``assigned_at + outcome_horizon``（**含边界**，即
``observed_at == mature_at`` 视为成熟）。判断基准是 ``run.generated_at``：

* ``mature``：run 时间 >= 成熟时点，且存在一条 ``mature_at <= observed_at <= run``
  的观测；同一 unit 有多条时取最早一条成熟观测。
* ``pending``：run 时间 < 成熟时点。此时即使已有更早的快照也不计入效应，
  既不计为零，也不写成永久缺数。
* ``mature_but_missing``：run 时间 >= 成熟时点，但没有任何 ``observed_at >=
  mature_at`` 的观测（记录缺失 or 迟到报告尚未到达）。这是需要暴露的 attrition。
* ``observed_before_horizon``：run 时间 >= 成熟时点，只有早于成熟时点的观测，
  不能当作固定窗结果使用。

同一 unit 的重复观测 / 重复分配不会放大分母：分母始终是唯一 assignment unit；
重复指标观测在 ``duplicate_observation_*`` 中单独计数。``observed_at`` 晚于
``run.generated_at`` 的记录在本次运行时尚不可见，不计入任何状态、只计数；
实验结束之后但在 run 之前到达、且落在该 unit 成熟窗内的报道仍然可用。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Iterable

from .models import (
    Assignment,
    ExperimentConfig,
    MetricResult,
    MetricSpec,
    Observation,
)
from .stats import two_proportion_test, welch_t_test

GUARDRAIL_WATCH_RATIO = 0.5
SAMPLE_LIMIT = 20
UNIT_DETAIL_LIMIT = 100
# audit.json / decision_card.json 里单位明细的预算上限；超出部分只保留计数，
# 计数与覆盖率始终基于全部唯一 assignment unit。
UNIT_DETAIL_BUDGET = 300


def _round(value: float, digits: int = 10) -> float:
    rounded = round(value, digits)
    return 0.0 if abs(rounded) < 1e-12 else rounded


def _as_of(value: datetime | str | None, config: ExperimentConfig) -> datetime:
    if value is None:
        moment = config.end
    elif isinstance(value, datetime):
        moment = value
    else:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


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


# ---------------------------------------------------------------------------
# 结果成熟期 cohort 分类
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UnitMeasurement:
    unit_id: str
    variant: str
    assigned_at: datetime
    mature_at: datetime
    status: str
    observed_at: datetime | None = None
    early_observed_at: datetime | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "unit_id": self.unit_id,
            "variant": self.variant,
            "assigned_at": self.assigned_at.isoformat(),
            "mature_at": self.mature_at.isoformat(),
            "status": self.status,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
            "early_observed_at": (
                self.early_observed_at.isoformat() if self.early_observed_at else None
            ),
        }


@dataclass(frozen=True)
class MeasurementCohort:
    metric_key: str
    label: str
    role: str
    outcome_horizon: float | None
    as_of: datetime
    status: str  # legacy | complete | incomplete | provisional
    units: tuple[UnitMeasurement, ...]
    eligible_observations: tuple[Observation, ...]
    counts: dict[str, Any]

    def summary(self) -> dict[str, Any]:
        """不含 unit 明细的紧凑汇总，用于重复嵌入检查/指标 evidence。"""
        return self.details(include_units=False)

    def details(self, *, include_units: bool = True,
                unit_limit: int = UNIT_DETAIL_LIMIT) -> dict[str, Any]:
        payload = {
            "metric_key": self.metric_key,
            "label": self.label,
            "role": self.role,
            "outcome_horizon": self.outcome_horizon,
            "as_of": self.as_of.isoformat(),
            "status": self.status,
            **self.counts,
        }
        if include_units:
            # 明细只保留有限条数，避免台账与 JSON 产物随样本线性膨胀；
            # 计数与覆盖率始终基于全部唯一 assignment unit。
            payload["units"] = [
                unit.payload() for unit in self.units[:unit_limit]
            ]
            payload["units_truncated"] = len(self.units) > unit_limit
        return payload


def _unique_assignments(
    config: ExperimentConfig, assignments: Iterable[Assignment]
) -> tuple[dict[str, Assignment], int]:
    """每个 unit 取输入中的第一条分配；返回 (map, 重复行数)。"""
    by_unit: dict[str, Assignment] = {}
    duplicates = 0
    for row in assignments:
        if row.experiment_id != config.experiment_id:
            continue
        if row.unit_id in by_unit:
            duplicates += 1
            continue
        by_unit[row.unit_id] = row
    return by_unit, duplicates


def _status_bucket() -> dict[str, int]:
    return {"mature": 0, "pending": 0, "mature_but_missing": 0,
            "observed_before_horizon": 0}


def assess_measurement(
    config: ExperimentConfig,
    assignments: Iterable[Assignment],
    observations: Iterable[Observation],
    spec: MetricSpec,
    role: str,
    as_of: datetime | str | None = None,
) -> MeasurementCohort:
    """按单个 MetricSpec 的 outcome_horizon 对唯一 assignment unit 分类。

    没有 horizon 的旧配置不进入本函数；``compute_metric`` 保持旧行为。
    统计对象始终是唯一 assignment unit，而不是观测行。
    """
    if spec.outcome_horizon is None:
        raise ValueError(f"指标 {spec.key} 未配置 outcome_horizon")
    moment = _as_of(as_of, config)
    assignment_by_unit, duplicate_assignments = _unique_assignments(config, assignments)
    observations_by_unit: dict[str, list[tuple[int, Observation]]] = defaultdict(list)
    index = 0
    for row in observations:
        if row.experiment_id != config.experiment_id:
            continue
        observations_by_unit[row.unit_id].append((index, row))
        index += 1

    status_by_variant: dict[str, dict[str, int]] = {
        variant.key: _status_bucket() for variant in config.variants
    }
    denominator_by_variant: dict[str, int] = {
        variant.key: 0 for variant in config.variants
    }
    units: list[UnitMeasurement] = []
    eligible_observations: list[Observation] = []
    duplicate_observation_rows = 0
    duplicate_observation_units = 0
    later_than_run_rows = 0
    late_report_rows = 0
    pre_horizon_observation_rows = 0

    for unit_id in sorted(assignment_by_unit):
        assignment = assignment_by_unit[unit_id]
        if assignment.variant not in status_by_variant:
            # 未声明的变体由结构检查处理，这里跳过以免污染分母。
            continue
        denominator_by_variant[assignment.variant] += 1
        mature_at = spec.mature_at(assignment.assigned_at)
        assert mature_at is not None
        rows = observations_by_unit.get(unit_id, [])
        if len(rows) > 1:
            duplicate_observation_units += 1
            duplicate_observation_rows += len(rows) - 1
        visible: list[tuple[int, Observation]] = []
        for order, row in rows:
            if row.observed_at > moment:
                later_than_run_rows += 1
                continue
            if row.observed_at > config.end:
                late_report_rows += 1
            visible.append((order, row))
        mature_rows = [
            (order, row) for order, row in visible
            if row.observed_at >= mature_at
        ]
        early_rows = [
            (order, row) for order, row in visible
            if row.observed_at < mature_at
        ]
        pre_horizon_observation_rows += len(early_rows)
        mature = moment >= mature_at
        chosen: Observation | None = None
        early_chosen: datetime | None = None
        if mature_rows:
            _, chosen_row = min(
                mature_rows, key=lambda item: (item[1].observed_at, item[0])
            )
            chosen = chosen_row
            status = "mature"
        elif not mature:
            status = "pending"
        elif early_rows:
            early_chosen = min(row.observed_at for _, row in early_rows)
            status = "observed_before_horizon"
        else:
            status = "mature_but_missing"
        status_by_variant[assignment.variant][status] += 1
        units.append(
            UnitMeasurement(
                unit_id=unit_id,
                variant=assignment.variant,
                assigned_at=assignment.assigned_at,
                mature_at=mature_at,
                status=status,
                observed_at=chosen.observed_at if chosen else None,
                early_observed_at=early_chosen,
            )
        )
        if chosen is not None:
            # 效应口径以分配臂为准；观测 variant 冲突由数据一致性检查报告。
            eligible_observations.append(
                chosen if chosen.variant == assignment.variant
                else replace(chosen, variant=assignment.variant)
            )

    pending_total = sum(
        bucket["pending"] for bucket in status_by_variant.values()
    )
    missing_total = sum(
        bucket["mature_but_missing"] for bucket in status_by_variant.values()
    )
    early_only_total = sum(
        bucket["observed_before_horizon"] for bucket in status_by_variant.values()
    )
    if pending_total:
        status = "provisional"
    elif missing_total or early_only_total:
        status = "incomplete"
    else:
        status = "complete"
    pending_cohorts = _pending_cohorts(units)
    eligible_by_variant = {
        variant.key: len({
            unit.unit_id for unit in units
            if unit.variant == variant.key and unit.status == "mature"
        })
        for variant in config.variants
    }
    coverage_by_variant = {
        key: (eligible_by_variant[key] / denominator_by_variant[key]
              if denominator_by_variant[key] else 0.0)
        for key in denominator_by_variant
    }
    total_denominator = sum(denominator_by_variant.values())
    total_eligible = sum(eligible_by_variant.values())
    counts = {
        "denominator_units": total_denominator,
        "denominator_units_by_variant": denominator_by_variant,
        "eligible_units": total_eligible,
        "eligible_units_by_variant": eligible_by_variant,
        "pending_units": pending_total,
        "mature_but_missing_units": missing_total,
        "observed_before_horizon_units": early_only_total,
        "status_by_variant": status_by_variant,
        "measurement_coverage": (
            round(total_eligible / total_denominator, 8)
            if total_denominator else 0.0
        ),
        "coverage_by_variant": {
            key: round(value, 8) for key, value in coverage_by_variant.items()
        },
        "eligible_observations": len(eligible_observations),
        "pending_cohorts": pending_cohorts,
        "duplicate_assignment_rows": duplicate_assignments,
        "duplicate_observation_units": duplicate_observation_units,
        "duplicate_observation_rows": duplicate_observation_rows,
        "pre_horizon_observation_rows": pre_horizon_observation_rows,
        "late_report_rows": late_report_rows,
        "later_than_run_rows": later_than_run_rows,
        "sample_units": {
            "pending": [
                unit.unit_id for unit in units if unit.status == "pending"
            ][:SAMPLE_LIMIT],
            "mature_but_missing": [
                unit.unit_id for unit in units
                if unit.status == "mature_but_missing"
            ][:SAMPLE_LIMIT],
            "observed_before_horizon": [
                unit.unit_id for unit in units
                if unit.status == "observed_before_horizon"
            ][:SAMPLE_LIMIT],
        },
    }
    return MeasurementCohort(
        metric_key=spec.key,
        label=spec.label,
        role=role,
        outcome_horizon=spec.outcome_horizon,
        as_of=moment,
        status=status,
        units=tuple(units),
        eligible_observations=tuple(eligible_observations),
        counts=counts,
    )


def _pending_cohorts(units: list[UnitMeasurement]) -> list[dict[str, Any]]:
    """按 assigned_at 日历日聚合等待中的 cohort，便于报告展示。

    同一日历日内可能因入组时刻不同而有不同的成熟时点，因此同时给出最早与
    最晚成熟时点；两者相同时只保留一个字段。
    """
    grouped: dict[str, dict[str, Any]] = {}
    for unit in units:
        if unit.status != "pending":
            continue
        key = unit.assigned_at.date().isoformat()
        entry = grouped.setdefault(
            key,
            {
                "assigned_date": key,
                "mature_at_earliest": unit.mature_at,
                "mature_at_latest": unit.mature_at,
                "units": 0,
                "by_variant": {},
            },
        )
        if unit.mature_at < entry["mature_at_earliest"]:
            entry["mature_at_earliest"] = unit.mature_at
        if unit.mature_at > entry["mature_at_latest"]:
            entry["mature_at_latest"] = unit.mature_at
        entry["units"] += 1
        entry["by_variant"][unit.variant] = entry["by_variant"].get(unit.variant, 0) + 1
    result: list[dict[str, Any]] = []
    for key in sorted(grouped):
        entry = grouped[key]
        earliest = entry["mature_at_earliest"].isoformat()
        latest = entry["mature_at_latest"].isoformat()
        payload = {
            "assigned_date": entry["assigned_date"],
            "mature_at": earliest,
            "units": entry["units"],
            "by_variant": entry["by_variant"],
        }
        if earliest != latest:
            payload["mature_at_latest"] = latest
        result.append(payload)
    return result


# ---------------------------------------------------------------------------
# 指标计算
# ---------------------------------------------------------------------------


def _legacy_measurement(spec: MetricSpec, observation_count: int) -> dict[str, Any]:
    return {
        "metric_key": spec.key,
        "label": spec.label,
        "outcome_horizon": None,
        "status": "legacy",
        "as_of": None,
        "eligible_observations": observation_count,
        "note": "未配置 outcome_horizon，沿用旧行为：全部观测直接参与指标计算。",
    }


def _empty_metric(
    config: ExperimentConfig,
    spec: MetricSpec,
    role: str,
    treatment_key: str,
    measurement: dict[str, Any],
    reason: str,
) -> MetricResult:
    measurement_status = str(measurement.get("status", "incomplete"))
    guardrail_status = "n/a"
    if role == "guardrail" and measurement_status in (
        "provisional", "incomplete", "no_data", "unverified"
    ):
        # 成熟观测不足以计算护栏时，不能记为 n/a（等价于「未评估」被忽略）；
        # 显式 pending 会被护栏检查升级为 watch。
        guardrail_status = "pending"
    return MetricResult(
        key=spec.key,
        label=spec.label,
        role=role,
        kind=spec.kind,
        orientation=spec.orientation,
        control_label=config.variant_label(config.control),
        treatment_label=config.variant_label(treatment_key),
        control_n=0,
        treatment_n=0,
        control_sum=0.0,
        treatment_sum=0.0,
        control_value=0.0,
        treatment_value=0.0,
        absolute_effect=0.0,
        relative_effect=None,
        ci_low=0.0,
        ci_high=0.0,
        p_value=1.0,
        alpha=spec.alpha,
        significant=False,
        favorable=False,
        meets_threshold=False,
        guardrail_status=guardrail_status,
        degradation=0.0,
        max_degradation=spec.max_degradation,
        outcome_horizon=spec.outcome_horizon,
        measurement_status=measurement_status,
        measurement=measurement,
        computed=False,
        insufficient_reason=reason,
    )


def _result_from_values(
    config: ExperimentConfig,
    spec: MetricSpec,
    role: str,
    control_values: list[float],
    treatment_values: list[float],
    treatment_key: str,
    measurement: dict[str, Any],
) -> MetricResult:
    if not control_values or not treatment_values:
        missing = []
        if not control_values:
            missing.append("对照组")
        if not treatment_values:
            missing.append("实验组")
        return _empty_metric(
            config, spec, role, treatment_key, measurement,
            f"成熟观测不足：{'、'.join(missing)}没有可用观测",
        )
    try:
        result = comparison(control_values, treatment_values, spec.kind, spec.alpha)
    except ValueError as exc:
        return _empty_metric(
            config, spec, role, treatment_key, measurement,
            f"成熟观测不足以完成统计检验：{exc}",
        )
    effect = float(result["absolute_effect"])
    favorable = effect * spec.favorable_sign > 0.0
    relative = None
    if abs(float(result["control_value"])) > 1e-12:
        relative = effect / abs(float(result["control_value"]))
    status = "n/a"
    degradation = 0.0
    measurement_status = str(measurement.get("status", "legacy"))
    if role == "guardrail":
        if measurement_status in ("provisional", "incomplete"):
            # 护栏的成熟观测集不完整时不能用「未越界」下结论：显式标记
            # pending，由护栏检查升级为 watch。
            status = "pending"
        else:
            degradation = -effect if spec.orientation == "higher_is_better" else effect
            if degradation > spec.max_degradation:
                status = "breach"
            elif degradation > 0.0 and degradation > spec.max_degradation * GUARDRAIL_WATCH_RATIO:
                status = "watch"
            else:
                status = "ok"
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
        degradation=_round(degradation),
        max_degradation=spec.max_degradation,
        outcome_horizon=spec.outcome_horizon,
        measurement_status=str(measurement.get("status", "legacy")),
        measurement=measurement,
        computed=True,
        insufficient_reason="",
    )


def compute_metric(
    config: ExperimentConfig,
    observations: list[Observation],
    spec: MetricSpec,
    role: str,
) -> MetricResult:
    """旧行为入口：不区分成熟度，把全部观测直接投入指标。"""
    grouped = _group_values(observations, spec)
    control_values = grouped.get(config.control, [])
    treatment_variants = [v.key for v in config.variants if v.key != config.control]
    treatment_key = treatment_variants[0]
    treatment_values = grouped.get(treatment_key, [])
    if not control_values or not treatment_values:
        raise ValueError(f"指标 {spec.key} 缺少对照组或实验组观测")
    measurement = _legacy_measurement(spec, len(observations))
    return _result_from_values(
        config, spec, role, control_values, treatment_values,
        treatment_key, measurement,
    )


def compute_metric_with_measurement(
    config: ExperimentConfig,
    observations: list[Observation],
    spec: MetricSpec,
    role: str,
    assignment_map: dict[str, Assignment],
    as_of: datetime | str | None = None,
) -> tuple[MetricResult, MeasurementCohort]:
    """按该指标自己的 outcome_horizon 只取成熟观测。"""
    cohort = assess_measurement(config, assignment_map.values(), observations, spec,
                                role, as_of)
    grouped: dict[str, list[float]] = defaultdict(list)
    for observation in cohort.eligible_observations:
        grouped[observation.variant].append(observation.values[spec.key])
    treatment_key = next(
        variant.key for variant in config.variants if variant.key != config.control
    )
    result = _result_from_values(
        config, spec, role,
        grouped.get(config.control, []),
        grouped.get(treatment_key, []),
        treatment_key,
        # 指标结果里只带汇总证据，完整 unit 明细由 collect_measurements 输出。
        cohort.details(include_units=False),
    )
    return result, cohort


def compute_all_metrics(
    config: ExperimentConfig,
    observations: list[Observation],
    assignments: Iterable[Assignment] | None = None,
    generated_at: datetime | str | None = None,
) -> tuple[MetricResult, list[MetricResult], list[MetricResult]]:
    """计算全部指标。

    ``assignments`` 与 ``generated_at`` 同时提供时，配置了 outcome_horizon 的
    指标使用各自的成熟观测集；未配置 horizon 的指标仍走旧的 ``compute_metric``。
    """
    assignment_map: dict[str, Assignment] | None = None
    if assignments is not None:
        assignment_map, _ = _unique_assignments(config, assignments)

    def run_one(spec: MetricSpec, role: str) -> MetricResult:
        if spec.outcome_horizon is not None and assignment_map is not None:
            metric, _ = compute_metric_with_measurement(
                config, observations, spec, role, assignment_map, generated_at
            )
            return metric
        if not observations:
            # 追踪配置下可能出现整个 run 尚无任何结果的情况：这是 pending /
            # 无数据，而不是异常；明确标记而不是伪造 0 值观测。
            payload = _legacy_measurement(spec, 0)
            payload["status"] = "no_data"
            return _empty_metric(
                config, spec, role,
                next(
                    variant.key for variant in config.variants
                    if variant.key != config.control
                ),
                payload,
                "本次 run 尚无任何观测记录",
            )
        if spec.outcome_horizon is not None and assignment_map is None:
            # 缺少分配信息时无法判定成熟度，保持旧的观测口径并在 payload 中说明。
            result = compute_metric(config, observations, spec, role)
            payload = _legacy_measurement(spec, len(observations))
            payload["status"] = "unverified"
            payload["note"] = (
                "配置了 outcome_horizon 但调用方未提供 assignments，"
                "无法判定成熟度，本次按全部观测计算。"
            )
            return replace(
                result, measurement_status="unverified",
                measurement=payload, outcome_horizon=spec.outcome_horizon,
            )
        return compute_metric(config, observations, spec, role)

    primary = run_one(config.primary, "primary")
    secondary = [run_one(spec, "secondary") for spec in config.secondary]
    guardrails = [run_one(spec, "guardrail") for spec in config.guardrails]
    return primary, secondary, guardrails


def collect_measurements(
    config: ExperimentConfig,
    observations: list[Observation],
    assignments: Iterable[Assignment],
    generated_at: datetime | str | None = None,
) -> list[dict[str, Any]]:
    """为报告/台账汇总每个指标的成熟度证据（未配置 horizon 的记为 legacy）。"""
    assignment_map, _ = _unique_assignments(config, assignments)
    entries: list[tuple[MetricSpec, str, MeasurementCohort | None]] = []
    for spec, role in (
        (config.primary, "primary"),
        *((item, "secondary") for item in config.secondary),
        *((item, "guardrail") for item in config.guardrails),
    ):
        if spec.outcome_horizon is None:
            entries.append((spec, role, None))
            continue
        cohort = assess_measurement(
            config, assignment_map.values(), observations, spec, role, generated_at
        )
        entries.append((spec, role, cohort))
    tracked = sum(1 for _, _, cohort in entries if cohort is not None)
    per_metric_limit = (
        max(10, UNIT_DETAIL_BUDGET // tracked) if tracked else UNIT_DETAIL_LIMIT
    )
    payloads: list[dict[str, Any]] = []
    for spec, role, cohort in entries:
        if cohort is None:
            payloads.append({
                "metric_key": spec.key,
                "label": spec.label,
                "role": role,
                "outcome_horizon": None,
                "status": "legacy",
                "as_of": None,
                "note": "未配置 outcome_horizon，沿用旧行为。",
            })
        else:
            payloads.append(cohort.details(unit_limit=per_metric_limit))
    return payloads


def analysis_observations_for(
    config: ExperimentConfig,
    observations: list[Observation],
    assignments: Iterable[Assignment],
    spec: MetricSpec,
    generated_at: datetime | str | None = None,
) -> list[Observation]:
    """返回某指标下每个 unit 的成熟观测；未配置 horizon 时原样返回输入。"""
    if spec.outcome_horizon is None:
        return list(observations)
    assignment_map, _ = _unique_assignments(config, assignments)
    cohort = assess_measurement(
        config, assignment_map.values(), observations, spec, "primary", generated_at
    )
    return list(cohort.eligible_observations)

