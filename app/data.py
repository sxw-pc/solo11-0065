"""CSV 输入契约与解析。所有数据都是合成 fixture，不做任何外部访问。"""

from __future__ import annotations

import csv
import hashlib
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .models import Assignment, Dataset, ExperimentConfig, Observation

UNIT_RE = re.compile(r"^unit-\d{4,8}$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
BASE_ASSIGNMENT_COLUMNS = (
    "unit_id", "experiment_id", "variant", "assigned_at",
)
BASE_OBSERVATION_COLUMNS = (
    "unit_id", "experiment_id", "variant", "observed_at",
)


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


def _finite_value(raw: str, path: str, line: int, column: str) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise DataValidationError(f"{path}:{line} 列 {column} 不是数字") from exc
    if not math.isfinite(value):
        raise DataValidationError(f"{path}:{line} 列 {column} 必须是有限数字")
    return value


def _ratio_pair(
    row: dict[str, str], spec: Any, path: str, line: int
) -> tuple[float, float]:
    """按同一行解析 numerator/denominator 样本对并执行分母契约。"""
    numerator = _finite_value(
        row[spec.numerator_column], path, line, spec.numerator_column
    )
    denominator = _finite_value(
        row[spec.denominator_column], path, line, spec.denominator_column
    )
    if denominator < 0.0:
        raise DataValidationError(
            f"{path}:{line} 指标 {spec.key} 的分母列 {spec.denominator_column} "
            "不能为负数"
        )
    if denominator == 0.0 and numerator != 0.0:
        raise DataValidationError(
            f"{path}:{line} 指标 {spec.key} 分母为 0 时分子必须也为 0"
        )
    return numerator, denominator


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
    columns: list[str] = []
    for spec in specs:
        if spec.is_ratio:
            columns.extend([spec.numerator_column, spec.denominator_column])
        else:
            columns.append(spec.column)
    rows = _rows(path, (*BASE_OBSERVATION_COLUMNS, *dict.fromkeys(columns)))
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
        values: dict[str, float] = {}
        pairs: dict[str, tuple[float, float]] = {}
        for spec in specs:
            if spec.is_ratio:
                # numerator 与 denominator 来自同一行，作为不可拆开的样本对保存。
                pairs[spec.key] = _ratio_pair(row, spec, path.name, index)
            else:
                values[spec.key] = _metric_value(
                    row[spec.column], spec.kind, path.name, index, spec.column
                )
        observations.append(
            Observation(
                unit_id=unit_id,
                experiment_id=experiment_id,
                variant=variant,
                observed_at=_parse_time(row["observed_at"], path.name, index),
                values=values,
                pairs=pairs,
            )
        )
    return observations


def load_dataset(
    config: ExperimentConfig, assignments_path: Path, observations_path: Path
) -> Dataset:
    return Dataset(
        assignments=load_assignments(assignments_path, config),
        observations=load_observations(observations_path, config),
    )


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
