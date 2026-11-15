"""领域模型：只承载数据，不依赖 Flask 或数据库。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

SCHEMA_VERSION = 1

# 结果成熟度：以「唯一 assignment unit」为分母的逐指标 cohort 状态。
MEASUREMENT_STATUSES = (
    "mature",
    "pending",
    "mature_but_missing",
    "observed_before_horizon",
)
MEASUREMENT_STATUS_LABELS = {
    "mature": "成熟可用",
    "pending": "等待成熟",
    "mature_but_missing": "成熟缺数",
    "observed_before_horizon": "早于窗口",
    # 指标级聚合状态
    "complete": "全部成熟",
    "incomplete": "成熟缺数",
    "provisional": "等待成熟",
    "legacy": "旧口径",
    "unverified": "未校验",
    "no_data": "无观测",
}

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
    column: str
    label: str
    kind: str  # proportion | mean
    orientation: str  # higher_is_better | lower_is_better
    alpha: float = 0.05
    min_effect: float = 0.0
    max_degradation: float = 0.0
    # 结果成熟期（天）。None 表示未配置，沿用旧行为：全部观测直接入指标。
    outcome_horizon: float | None = None

    @property
    def favorable_sign(self) -> int:
        return 1 if self.orientation == "higher_is_better" else -1

    @property
    def has_horizon(self) -> bool:
        return self.outcome_horizon is not None

    def mature_at(self, assigned_at: datetime) -> datetime | None:
        """每个 unit 自 assigned_at 起的固定追踪窗终点；边界含等号。"""
        if self.outcome_horizon is None:
            return None
        return assigned_at + timedelta(days=self.outcome_horizon)


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
    values: dict[str, float]


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
    control_sum: float
    treatment_sum: float
    control_value: float
    treatment_value: float
    absolute_effect: float
    relative_effect: float | None
    ci_low: float
    ci_high: float
    p_value: float
    alpha: float
    significant: bool
    favorable: bool
    meets_threshold: bool
    guardrail_status: str
    degradation: float
    max_degradation: float
    # 结果成熟期相关。默认值保证未配置 horizon 的调用方完全兼容。
    outcome_horizon: float | None = None
    measurement_status: str = "legacy"  # legacy | complete | incomplete | provisional
    measurement: dict[str, Any] = field(default_factory=dict)
    computed: bool = True
    insufficient_reason: str = ""


@dataclass(frozen=True)
class StratumResult:
    field: str
    value: str
    control_n: int
    treatment_n: int
    control_value: float
    treatment_value: float
    absolute_effect: float
    p_value: float
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
