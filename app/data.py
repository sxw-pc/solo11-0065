"""CSV 输入契约与解析。所有数据都是合成 fixture，不做任何外部访问。"""

from __future__ import annotations

import csv
import hashlib
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .models import (Assignment, BaselineObservation, Dataset, ExperimentConfig,
                     Observation)

UNIT_RE = re.compile(r"^unit-\d{4,8}$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
BASE_ASSIGNMENT_COLUMNS = (
    "unit_id", "experiment_id", "variant", "assigned_at",
)
BASE_OBSERVATION_COLUMNS = (
    "unit_id", "experiment_id", "variant", "observed_at",
)
BASE_BASELINE_COLUMNS = (
    "unit_id", "experiment_id", "metric_key", "measured_at",
)
BASELINE_FORBIDDEN_COLUMNS = ("variant", "observed_at")


class DataValidationError(ValueError):
    """CSV 数据不符合输入契约。"""


def _parse_time(value: str, path: str, line: int) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise DataValidationError(f"{path}:{line} 时间不合法：{value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _rows(path: Path, required: Iterable[str]) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            headers = tuple(reader.fieldnames or ())
            missing = [name for name in required if name not in headers]
            if missing:
                raise DataValidationError(f"{path.name} 缺少列：{', '.join(missing)}")
            rows = list(reader)
    except OSError as exc:
        raise DataValidationError(f"无法读取 {path.name}: {exc}") from exc
    if not rows:
        raise DataValidationError(f"{path.name} 没有数据行")
    return rows


def _metric_value(raw: str, kind: str, path: str, line: int, column: str) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise DataValidationError(f"{path}:{line} 列 {column} 不是数字") from exc
    if not math.isfinite(value):
        raise DataValidationError(f"{path}:{line} 列 {column} 必须是有限数字")
    if kind == "proportion" and value not in (0.0, 1.0):
        raise DataValidationError(f"{path}:{line} 列 {column} 必须是 0 或 1")
    return value


def load_assignments(path: Path, config: ExperimentConfig) -> list[Assignment]:
    strata_fields = list(config.stratification.fields)
    rows = _rows(path, (*BASE_ASSIGNMENT_COLUMNS, *strata_fields))
    variant_keys = {variant.key for variant in config.variants}
    assignments: list[Assignment] = []
    for index, row in enumerate(rows, start=2):
        unit_id = (row.get("unit_id") or "").strip()
        experiment_id = (row.get("experiment_id") or "").strip()
        variant = (row.get("variant") or "").strip()
        if not UNIT_RE.match(unit_id):
            raise DataValidationError(
                f"{path.name}:{index} unit_id 必须形如 unit-0001"
            )
        if not SLUG_RE.match(experiment_id):
            raise DataValidationError(f"{path.name}:{index} experiment_id 不合法")
        if experiment_id == config.experiment_id and variant not in variant_keys:
            raise DataValidationError(
                f"{path.name}:{index} 未知变体：{variant!r}"
            )
        if not variant:
            raise DataValidationError(f"{path.name}:{index} variant 不能为空")
        strata: dict[str, str] = {}
        for field in strata_fields:
            value = (row.get(field) or "").strip()
            if not LABEL_RE.match(value):
                raise DataValidationError(
                    f"{path.name}:{index} 分层字段 {field} 必须是安全短标签"
                )
            strata[field] = value
        assignments.append(
            Assignment(
                unit_id=unit_id,
                experiment_id=experiment_id,
                variant=variant,
                assigned_at=_parse_time(row["assigned_at"], path.name, index),
                strata=strata,
            )
        )
    return assignments


def load_observations(path: Path, config: ExperimentConfig) -> list[Observation]:
    specs = (config.primary, *config.secondary, *config.guardrails)
    columns = [spec.column for spec in specs]
    rows = _rows(path, (*BASE_OBSERVATION_COLUMNS, *columns))
    variant_keys = {variant.key for variant in config.variants}
    observations: list[Observation] = []
    for index, row in enumerate(rows, start=2):
        unit_id = (row.get("unit_id") or "").strip()
        experiment_id = (row.get("experiment_id") or "").strip()
        variant = (row.get("variant") or "").strip()
        if not UNIT_RE.match(unit_id):
            raise DataValidationError(
                f"{path.name}:{index} unit_id 必须形如 unit-0001"
            )
        if not SLUG_RE.match(experiment_id):
            raise DataValidationError(f"{path.name}:{index} experiment_id 不合法")
        if experiment_id == config.experiment_id and variant not in variant_keys:
            raise DataValidationError(
                f"{path.name}:{index} 未知变体：{variant!r}"
            )
        if not variant:
            raise DataValidationError(f"{path.name}:{index} variant 不能为空")
        values = {
            spec.key: _metric_value(
                row[spec.column], spec.kind, path.name, index, spec.column
            )
            for spec in specs
        }
        observations.append(
            Observation(
                unit_id=unit_id,
                experiment_id=experiment_id,
                variant=variant,
                observed_at=_parse_time(row["observed_at"], path.name, index),
                values=values,
            )
        )
    return observations


def load_baseline(
    path: Path,
    config: ExperimentConfig,
    *,
    assignments: list[Assignment] | None = None,
) -> list[BaselineObservation]:
    """读取实验前协变量。

    身份权威是 ``(experiment_id, unit_id, metric_key)``：
    - 文件必须独立声明 ``measured_at``，且不得出现 ``variant`` / ``observed_at`` 列；
    - ``measured_at`` 必须早于 ``experiment.start``（因此也必然在结果窗口之前）；
    - 只能关联同一实验、同一指标的已分配 unit，不使用治疗标签做匹配；
    - 重复键、未分配 unit、越窗与非有限值都会按 ``文件:行`` 拒绝。

    分配但缺少结果的 unit 不在这里报错：它会作为“未配对”进入覆盖统计，
    由覆盖门槛决定整次调整是否可估计。
    """
    spec = config.primary.baseline
    if spec is None:
        return []
    rows = _rows(path, (*BASE_BASELINE_COLUMNS, spec.column))
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            headers = tuple(csv.DictReader(handle).fieldnames or ())
    except OSError as exc:
        raise DataValidationError(f"无法读取 {path.name}: {exc}") from exc
    leaked = [name for name in BASELINE_FORBIDDEN_COLUMNS if name in headers]
    if leaked:
        raise DataValidationError(
            f"{path.name} 不得包含 {', '.join(leaked)}：baseline 只能承载实验前协变量"
        )
    experiment_id = config.experiment_id
    assigned_units = (
        {row.unit_id for row in assignments if row.experiment_id == experiment_id}
        if assignments is not None else None
    )
    baseline: list[BaselineObservation] = []
    seen: dict[tuple[str, str, str], int] = {}
    for index, row in enumerate(rows, start=2):
        unit_id = (row.get("unit_id") or "").strip()
        row_experiment = (row.get("experiment_id") or "").strip()
        metric_key = (row.get("metric_key") or "").strip()
        if not UNIT_RE.match(unit_id):
            raise DataValidationError(
                f"{path.name}:{index} unit_id 必须形如 unit-0001"
            )
        if not SLUG_RE.match(row_experiment):
            raise DataValidationError(f"{path.name}:{index} experiment_id 不合法")
        if row_experiment != experiment_id:
            raise DataValidationError(
                f"{path.name}:{index} experiment_id 必须为 {experiment_id}"
            )
        if metric_key != config.primary.key:
            raise DataValidationError(
                f"{path.name}:{index} metric_key 只能引用主指标 {config.primary.key}"
            )
        if assigned_units is not None and unit_id not in assigned_units:
            raise DataValidationError(
                f"{path.name}:{index} unit {unit_id} 未分配到本实验"
            )
        measured_at = _parse_time(row["measured_at"], path.name, index)
        if measured_at >= config.start:
            raise DataValidationError(
                f"{path.name}:{index} measured_at 必须早于 experiment.start"
            )
        value = _metric_value(row[spec.column], "mean", path.name, index, spec.column)
        key = (row_experiment, unit_id, metric_key)
        if key in seen:
            raise DataValidationError(
                f"{path.name}:{index} 重复键 (experiment_id, unit_id, metric_key)，"
                f"首次出现在第 {seen[key]} 行"
            )
        seen[key] = index
        baseline.append(
            BaselineObservation(
                unit_id=unit_id,
                experiment_id=row_experiment,
                metric_key=metric_key,
                measured_at=measured_at,
                value=value,
            )
        )
    return baseline


def load_dataset(
    config: ExperimentConfig,
    assignments_path: Path,
    observations_path: Path,
    baseline_path: Path | None = None,
) -> Dataset:
    assignments = load_assignments(assignments_path, config)
    observations = load_observations(observations_path, config)
    baseline = (
        load_baseline(baseline_path, config, assignments=assignments)
        if baseline_path is not None else []
    )
    return Dataset(assignments=assignments, observations=observations,
                   baseline=baseline)


def sha256_files(paths: Iterable[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()
