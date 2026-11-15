"""CSV 输入契约与解析。所有数据都是合成 fixture，不做任何外部访问。

解析同时提供字节入口（``io.BytesIO``），让 run 可以从已固定的快照字节解析，
而不是再次触碰可能已被替换的源文件。
"""

from __future__ import annotations

import csv
import hashlib
import io
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
        return _rows_from_bytes(path.read_bytes(), path.name, required)
    except OSError as exc:
        raise DataValidationError(f"无法读取 {path.name}: {exc}") from exc


def _rows_from_bytes(
    payload: bytes, display_name: str, required: Iterable[str]
) -> list[dict[str, str]]:
    """从固定字节解析 CSV；调用方负责保证字节与 digest 来自同一份快照。"""
    handle = io.TextIOWrapper(io.BytesIO(payload), encoding="utf-8-sig", newline="")
    reader = csv.DictReader(handle)
    headers = tuple(reader.fieldnames or ())
    missing = [name for name in required if name not in headers]
    if missing:
        raise DataValidationError(f"{display_name} 缺少列：{', '.join(missing)}")
    rows = list(reader)
    if not rows:
        raise DataValidationError(f"{display_name} 没有数据行")
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
    return load_assignments_from_bytes(
        path.read_bytes(), path.name, config
    )


def load_assignments_from_bytes(
    payload: bytes, display_name: str, config: ExperimentConfig
) -> list[Assignment]:
    strata_fields = list(config.stratification.fields)
    rows = _rows_from_bytes(payload, display_name, (*BASE_ASSIGNMENT_COLUMNS, *strata_fields))
    variant_keys = {variant.key for variant in config.variants}
    assignments: list[Assignment] = []
    for index, row in enumerate(rows, start=2):
        unit_id = (row.get("unit_id") or "").strip()
        experiment_id = (row.get("experiment_id") or "").strip()
        variant = (row.get("variant") or "").strip()
        if not UNIT_RE.match(unit_id):
            raise DataValidationError(
                f"{display_name}:{index} unit_id 必须形如 unit-0001"
            )
        if not SLUG_RE.match(experiment_id):
            raise DataValidationError(f"{display_name}:{index} experiment_id 不合法")
        if experiment_id == config.experiment_id and variant not in variant_keys:
            raise DataValidationError(
                f"{display_name}:{index} 未知变体：{variant!r}"
            )
        if not variant:
            raise DataValidationError(f"{display_name}:{index} variant 不能为空")
        strata: dict[str, str] = {}
        for field in strata_fields:
            value = (row.get(field) or "").strip()
            if not LABEL_RE.match(value):
                raise DataValidationError(
                    f"{display_name}:{index} 分层字段 {field} 必须是安全短标签"
                )
            strata[field] = value
        assignments.append(
            Assignment(
                unit_id=unit_id,
                experiment_id=experiment_id,
                variant=variant,
                assigned_at=_parse_time(row["assigned_at"], display_name, index),
                strata=strata,
            )
        )
    return assignments


def load_observations(path: Path, config: ExperimentConfig) -> list[Observation]:
    return load_observations_from_bytes(path.read_bytes(), path.name, config)


def load_observations_from_bytes(
    payload: bytes, display_name: str, config: ExperimentConfig
) -> list[Observation]:
    specs = (config.primary, *config.secondary, *config.guardrails)
    columns = [spec.column for spec in specs]
    rows = _rows_from_bytes(payload, display_name, (*BASE_OBSERVATION_COLUMNS, *columns))
    variant_keys = {variant.key for variant in config.variants}
    observations: list[Observation] = []
    for index, row in enumerate(rows, start=2):
        unit_id = (row.get("unit_id") or "").strip()
        experiment_id = (row.get("experiment_id") or "").strip()
        variant = (row.get("variant") or "").strip()
        if not UNIT_RE.match(unit_id):
            raise DataValidationError(
                f"{display_name}:{index} unit_id 必须形如 unit-0001"
            )
        if not SLUG_RE.match(experiment_id):
            raise DataValidationError(f"{display_name}:{index} experiment_id 不合法")
        if experiment_id == config.experiment_id and variant not in variant_keys:
            raise DataValidationError(
                f"{display_name}:{index} 未知变体：{variant!r}"
            )
        if not variant:
            raise DataValidationError(f"{display_name}:{index} variant 不能为空")
        values = {
            spec.key: _metric_value(
                row[spec.column], spec.kind, display_name, index, spec.column
            )
            for spec in specs
        }
        observations.append(
            Observation(
                unit_id=unit_id,
                experiment_id=experiment_id,
                variant=variant,
                observed_at=_parse_time(row["observed_at"], display_name, index),
                values=values,
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


def load_dataset_from_bytes(
    config: ExperimentConfig,
    assignments_payload: bytes,
    assignments_name: str,
    observations_payload: bytes,
    observations_name: str,
) -> Dataset:
    """从快照字节构建数据集；整个 run 不再重新打开源 CSV。"""
    return Dataset(
        assignments=load_assignments_from_bytes(
            assignments_payload, assignments_name, config
        ),
        observations=load_observations_from_bytes(
            observations_payload, observations_name, config
        ),
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
