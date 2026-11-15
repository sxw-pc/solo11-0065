"""实验配置加载与声明式规则契约校验。"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any

import yaml

from .models import (DecisionCard, DecisionRule, DecisionSpec, ExperimentConfig,
                     ExposureSpec, MetricSpec, MutualExclusion, SplitSpec,
                     StratificationSpec, Variant)

SCHEMA_VERSION = 1
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
METRIC_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
FLAG_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
ALLOWED_DECISIONS = ("ship", "iterate", "stop", "review")


class ConfigError(ValueError):
    """配置不符合契约。"""


def _mapping(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{path} 必须是映射")
    return value


def _required(data: dict[str, Any], key: str, path: str) -> Any:
    if key not in data:
        raise ConfigError(f"{path}.{key} 缺失")
    return data[key]


def _text(value: Any, path: str, *, slug: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{path} 必须是非空字符串")
    text = value.strip()
    if slug and not SLUG_RE.match(text):
        raise ConfigError(f"{path} 必须是 3-64 位小写字母、数字或连字符")
    return text


def _number(value: Any, path: str, *, low: float | None = None, high: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{path} 必须是数字")
    number = float(value)
    if not number == number or number in (float("inf"), float("-inf")):
        raise ConfigError(f"{path} 必须是有限数字")
    if low is not None and number < low:
        raise ConfigError(f"{path} 必须 >= {low}")
    if high is not None and number > high:
        raise ConfigError(f"{path} 必须 <= {high}")
    return number


def _integer(value: Any, path: str, *, low: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < low:
        raise ConfigError(f"{path} 必须是 >= {low} 的整数")
    return value


def _timestamp(value: Any, path: str) -> datetime:
    if isinstance(value, date) and not isinstance(value, datetime):
        return datetime.combine(value, time.min, tzinfo=timezone.utc)
    if not isinstance(value, str):
        raise ConfigError(f"{path} 必须是 ISO-8601 日期或日期时间")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ConfigError(f"{path} 不是合法 ISO-8601 时间：{exc}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _metric(raw: Any, path: str, role: str) -> MetricSpec:
    data = _mapping(raw, path)
    kind = _text(_required(data, "kind", path), f"{path}.kind")
    if kind not in ("proportion", "mean"):
        raise ConfigError(f"{path}.kind 只能是 proportion 或 mean")
    orientation = _text(_required(data, "orientation", path), f"{path}.orientation")
    if orientation not in ("higher_is_better", "lower_is_better"):
        raise ConfigError(f"{path}.orientation 不受支持")
    alpha = _number(data.get("alpha", 0.05), f"{path}.alpha", low=0.001, high=0.5)
    min_effect = _number(data.get("min_effect", 0.0), f"{path}.min_effect", low=0.0)
    max_degradation = _number(data.get("max_degradation", 0.0),
                              f"{path}.max_degradation", low=0.0)
    if role == "guardrail" and kind == "proportion" and max_degradation > 1:
        raise ConfigError(f"{path}.max_degradation 不能大于 1")
    metric_key = _text(_required(data, "key", path), f"{path}.key")
    if not METRIC_KEY_RE.match(metric_key):
        raise ConfigError(f"{path}.key 必须是 2-64 位小写字母、数字或下划线")
    return MetricSpec(metric_key,
                      _text(_required(data, "column", path), f"{path}.column"),
                      _text(_required(data, "label", path), f"{path}.label"),
                      kind, orientation, alpha, min_effect, max_degradation)


def _condition(raw: Any, path: str, depth: int = 0) -> dict[str, Any]:
    if depth > 5:
        raise ConfigError(f"{path} 条件嵌套过深")
    data = _mapping(raw, path)
    if set(data) == {"flag"}:
        flag = data["flag"]
        if not isinstance(flag, str) or not FLAG_RE.match(flag):
            raise ConfigError(f"{path}.flag 不合法")
        return {"flag": flag}
    keys = set(data)
    if len(keys) != 1 or not keys <= {"all", "any", "none"}:
        raise ConfigError(f"{path} 必须包含 flag，或仅包含 all/any/none 之一")
    op = next(iter(keys))
    children = data[op]
    if not isinstance(children, list) or not children:
        raise ConfigError(f"{path}.{op} 必须是非空列表")
    return {op: [_condition(item, f"{path}.{op}[{i}]", depth + 1) for i, item in enumerate(children)]}


def _decision(raw: Any) -> DecisionSpec:
    data = _mapping(raw, "decision")
    fallback = _text(data.get("fallback", "review"), "decision.fallback")
    if fallback not in ALLOWED_DECISIONS:
        raise ConfigError("decision.fallback 不受支持")
    raw_cards = _mapping(_required(data, "cards", "decision"), "decision.cards")
    cards: dict[str, DecisionCard] = {}
    for key in ALLOWED_DECISIONS:
        if key not in raw_cards:
            raise ConfigError(f"decision.cards.{key} 缺失")
        item = _mapping(raw_cards[key], f"decision.cards.{key}")
        cards[key] = DecisionCard(
            key, _text(_required(item, "title", f"decision.cards.{key}"),
                       f"decision.cards.{key}.title"),
            _text(_required(item, "action", f"decision.cards.{key}"),
                  f"decision.cards.{key}.action"))
    rules_raw = _required(data, "rules", "decision")
    if not isinstance(rules_raw, list) or not rules_raw:
        raise ConfigError("decision.rules 必须是非空列表")
    rules: list[DecisionRule] = []
    seen: set[str] = set()
    for index, item in enumerate(rules_raw):
        path = f"decision.rules[{index}]"
        rule_data = _mapping(item, path)
        rule_id = _text(_required(rule_data, "id", path), f"{path}.id", slug=True)
        if rule_id in seen:
            raise ConfigError(f"{path}.id 重复")
        seen.add(rule_id)
        card = _text(_required(rule_data, "card", path), f"{path}.card")
        if card not in ALLOWED_DECISIONS:
            raise ConfigError(f"{path}.card 不受支持")
        priority = _integer(_required(rule_data, "priority", path), f"{path}.priority")
        rules.append(
            DecisionRule(
                id=rule_id,
                card=card,
                priority=priority,
                reason=_text(_required(rule_data, "reason", path), f"{path}.reason"),
                condition=_condition(_required(rule_data, "when", path), f"{path}.when"),
            )
        )
    return DecisionSpec(tuple(rules), cards, fallback)


def load_config(path: Path) -> ExperimentConfig:
    """读取并校验 YAML。任何未知或不完整字段都会被明确拒绝。"""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"无法读取配置 {path.name}: {exc}") from exc
    root = _mapping(raw, "root")
    version = _integer(root.get("schema_version", 0), "schema_version", low=1)
    if version != SCHEMA_VERSION:
        raise ConfigError(f"schema_version 必须为 {SCHEMA_VERSION}")
    experiment = _mapping(_required(root, "experiment", "root"), "experiment")
    start = _timestamp(_required(experiment, "start", "experiment"), "experiment.start")
    end = _timestamp(_required(experiment, "end", "experiment"), "experiment.end")
    if end <= start:
        raise ConfigError("experiment.end 必须晚于 experiment.start")
    variant_raw = _mapping(_required(root, "variants", "root"), "variants")
    variants: list[Variant] = []
    for key, value in variant_raw.items():
        if not isinstance(key, str) or not SLUG_RE.match(key):
            raise ConfigError(f"variants.{key} 不是合法变体键")
        item = _mapping(value, f"variants.{key}")
        variants.append(Variant(key, _text(item.get("label", key), f"variants.{key}.label")))
    if len(variants) != 2:
        raise ConfigError("分流验台只支持一个对照和一个实验变体")
    control = _text(_required(root, "control", "root"), "control", slug=True)
    if control not in {item.key for item in variants}:
        raise ConfigError("control 必须引用已声明变体")
    split_raw = _mapping(_required(root, "split", "root"), "split")
    expected_raw = _mapping(_required(split_raw, "expected", "split"), "split.expected")
    expected = {
        key: _number(value, f"split.expected.{key}", low=0.0, high=1.0)
        for key, value in expected_raw.items()
    }
    if set(expected) != {item.key for item in variants}:
        raise ConfigError("split.expected 必须覆盖全部变体")
    if abs(sum(expected.values()) - 1.0) > 1e-6:
        raise ConfigError("split.expected 之和必须为 1")
    split = SplitSpec(
        expected=expected,
        tolerance=_number(_required(split_raw, "tolerance", "split"),
                          "split.tolerance", low=0.0, high=0.5),
        min_assignments=_integer(_required(split_raw, "min_assignments", "split"),
                                 "split.min_assignments", low=2),
    )
    exposure_raw = _mapping(_required(root, "exposure", "root"), "exposure")
    exposure = ExposureSpec(
        max_per_unit=_integer(_required(exposure_raw, "max_per_unit", "exposure"),
                              "exposure.max_per_unit", low=1),
        tolerance=_integer(exposure_raw.get("tolerance", 0), "exposure.tolerance"),
    )
    mutex_raw = root.get("mutual_exclusions", [])
    if not isinstance(mutex_raw, list):
        raise ConfigError("mutual_exclusions 必须是列表")
    mutexes: list[MutualExclusion] = []
    for index, item in enumerate(mutex_raw):
        path = f"mutual_exclusions[{index}]"
        data = _mapping(item, path)
        mutex_id = _text(
            _required(data, "experiment_id", path), f"{path}.experiment_id", slug=True
        )
        if mutex_id in {entry.experiment_id for entry in mutexes}:
            raise ConfigError(f"{path}.experiment_id 重复")
        mutexes.append(
            MutualExclusion(
                experiment_id=mutex_id,
                label=_text(data.get("label", "互斥实验"), f"{path}.label"),
            )
        )
    metrics_raw = _mapping(_required(root, "metrics", "root"), "metrics")
    primary = _metric(_required(metrics_raw, "primary", "metrics"),
                      "metrics.primary", "primary")
    secondary_raw = metrics_raw.get("secondary", [])
    guardrail_raw = metrics_raw.get("guardrails", [])
    if not isinstance(secondary_raw, list) or not isinstance(guardrail_raw, list):
        raise ConfigError("metrics.secondary / guardrails 必须是列表")
    secondary = tuple(_metric(item, f"metrics.secondary[{i}]", "secondary")
                      for i, item in enumerate(secondary_raw))
    guardrails = tuple(_metric(item, f"metrics.guardrails[{i}]", "guardrail")
                       for i, item in enumerate(guardrail_raw))
    metric_keys = [primary.key, *(m.key for m in secondary), *(m.key for m in guardrails)]
    if len(metric_keys) != len(set(metric_keys)):
        raise ConfigError("指标 key 必须唯一")
    if not guardrails:
        raise ConfigError("至少需要声明一个护栏指标")
    strat_raw = _mapping(_required(root, "stratification", "root"), "stratification")
    fields_raw = _required(strat_raw, "fields", "stratification")
    if not isinstance(fields_raw, list) or not fields_raw:
        raise ConfigError("stratification.fields 必须是非空列表")
    fields = tuple(_text(item, f"stratification.fields[{i}]")
                   for i, item in enumerate(fields_raw))
    if len(fields) != len(set(fields)):
        raise ConfigError("stratification.fields 不得重复")
    stratification = StratificationSpec(
        fields=fields,
        min_group_size=_integer(_required(strat_raw, "min_group_size", "stratification"),
                                "stratification.min_group_size", low=2),
        max_groups=_integer(strat_raw.get("max_groups", 12), "stratification.max_groups", low=1),
        alpha=_number(strat_raw.get("alpha", 0.05), "stratification.alpha",
                      low=0.001, high=0.5),
        min_favorable_share=_number(
            strat_raw.get("min_favorable_share", 0.75),
            "stratification.min_favorable_share", low=0.0, high=1.0,
        ),
    )
    return ExperimentConfig(
        schema_version=version,
        experiment_id=_text(_required(experiment, "id", "experiment"),
                            "experiment.id", slug=True),
        name=_text(_required(experiment, "name", "experiment"), "experiment.name"),
        owner=_text(_required(experiment, "owner", "experiment"), "experiment.owner"),
        hypothesis=_text(_required(experiment, "hypothesis", "experiment"),
                         "experiment.hypothesis"),
        description=_text(
            experiment.get("description", experiment.get("hypothesis", "未提供描述")),
            "experiment.description",
        ),
        start=start,
        end=end,
        control=control,
        variants=tuple(variants),
        split=split,
        exposure=exposure,
        mutexes=tuple(mutexes),
        primary=primary,
        secondary=secondary,
        guardrails=guardrails,
        stratification=stratification,
        decision=_decision(_required(root, "decision", "root")),
    )
