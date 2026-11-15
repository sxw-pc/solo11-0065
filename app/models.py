"""领域模型：只承载数据，不依赖 Flask 或数据库。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

SCHEMA_VERSION = 1

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
SEVERITY_LABELS = {
    "critical": "严重",
    "high": "高",
    "medium": "中",
    "low": "低",
    "info": "提示",
}
CHECK_LABELS = {"pass": "通过", "warn": "关注", "fail": "未通过"}
DECISION_LABELS = {
    "ship": "可上线",
    "iterate": "继续迭代",
    "stop": "停止",
    "review": "人工复核",
}


@dataclass(frozen=True)
class Variant:
    key: str
    label: str


@dataclass(frozen=True)
class MetricSpec:
    key: str
    column: str | None
    label: str
    kind: str  # proportion | mean | ratio
    orientation: str  # higher_is_better | lower_is_better
    alpha: float = 0.05
    min_effect: float = 0.0
    max_degradation: float = 0.0
    # 仅 kind == "ratio" 使用：同一 unit 内不可拆开的分子/分母样本对。
    numerator_column: str | None = None
    denominator_column: str | None = None

    @property
    def favorable_sign(self) -> int:
        return 1 if self.orientation == "higher_is_better" else -1

    @property
    def is_ratio(self) -> bool:
        return self.kind == "ratio"


@dataclass(frozen=True)
class SplitSpec:
    expected: dict[str, float]
    tolerance: float
    min_assignments: int


@dataclass(frozen=True)
class ExposureSpec:
    max_per_unit: int
    tolerance: int


@dataclass(frozen=True)
class MutualExclusion:
    experiment_id: str
    label: str


@dataclass(frozen=True)
class StratificationSpec:
    fields: tuple[str, ...]
    min_group_size: int
    max_groups: int
    alpha: float
    min_favorable_share: float


@dataclass(frozen=True)
class DecisionCard:
    key: str
    title: str
    action: str


@dataclass(frozen=True)
class DecisionRule:
    id: str
    card: str
    priority: int
    reason: str
    condition: dict[str, Any]


@dataclass(frozen=True)
class DecisionSpec:
    rules: tuple[DecisionRule, ...]
    cards: dict[str, DecisionCard]
    fallback: str


@dataclass(frozen=True)
class ExperimentConfig:
    schema_version: int
    experiment_id: str
    name: str
    owner: str
    hypothesis: str
    description: str
    start: datetime
    end: datetime
    control: str
    variants: tuple[Variant, ...]
    split: SplitSpec
    exposure: ExposureSpec
    mutexes: tuple[MutualExclusion, ...]
    primary: MetricSpec
    secondary: tuple[MetricSpec, ...]
    guardrails: tuple[MetricSpec, ...]
    stratification: StratificationSpec
    decision: DecisionSpec

    def variant_label(self, key: str) -> str:
        for variant in self.variants:
            if variant.key == key:
                return variant.label
        return key


@dataclass(frozen=True)
class Assignment:
    unit_id: str
    experiment_id: str
    variant: str
    assigned_at: datetime
    strata: dict[str, str]


@dataclass(frozen=True)
class Observation:
    unit_id: str
    experiment_id: str
    variant: str
    observed_at: datetime
    values: dict[str, float] = field(default_factory=dict)
    # ratio 指标的成对样本：metric key -> (numerator, denominator)，
    # 两数来自同一行，统计时必须作为不可拆开的样本对使用。
    pairs: dict[str, tuple[float, float]] = field(default_factory=dict)


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    category: str
    title: str
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Check:
    code: str
    label: str
    status: str
    summary: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MetricResult:
    key: str
    label: str
    role: str
    kind: str
    orientation: str
    control_label: str
    treatment_label: str
    control_n: int
    treatment_n: int
    control_sum: float | None
    treatment_sum: float | None
    control_value: float | None
    treatment_value: float | None
    absolute_effect: float | None
    relative_effect: float | None
    ci_low: float | None
    ci_high: float | None
    p_value: float | None
    alpha: float
    significant: bool
    favorable: bool
    meets_threshold: bool
    guardrail_status: str
    degradation: float | None
    max_degradation: float
    # ratio 指标专属：pooled ratio（Σnumerator/Σdenominator）与配对 delta-method 信息。
    estimable: bool = True
    control_numerator_sum: float = 0.0
    control_denominator_sum: float = 0.0
    treatment_numerator_sum: float = 0.0
    treatment_denominator_sum: float = 0.0
    control_zero_denominator_units: int = 0
    treatment_zero_denominator_units: int = 0
    control_effective_units: int = 0
    treatment_effective_units: int = 0


@dataclass(frozen=True)
class StratumResult:
    field: str
    value: str
    control_n: int
    treatment_n: int
    control_value: float | None
    treatment_value: float | None
    absolute_effect: float | None
    p_value: float | None
    eligible: bool
    favorable: bool
    reversal: bool


@dataclass
class Dataset:
    assignments: list[Assignment]
    observations: list[Observation]

    @property
    def primary_assignments(self) -> list[Assignment]:
        raise NotImplementedError("由调用方按 experiment_id 过滤")


def check_sort_key(check: Check) -> tuple[int, str]:
    order = {"fail": 0, "warn": 1, "pass": 2}
    return (order.get(check.status, 3), check.code)


def finding_sort_key(finding: Finding) -> tuple[int, str, str]:
    return (
        SEVERITY_ORDER.get(finding.severity, 9),
        finding.category,
        finding.code,
    )
