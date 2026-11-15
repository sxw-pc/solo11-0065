"""Finding 稳定业务身份。

finding_key 不依赖 run_id、generated_at，也不依赖易变的 title/detail 文案与
evidence 中的测量数值；它由 code、category 与 evidence 中标识“业务对象”的
稳定字段（如护栏指标 key、互斥实验 id、分层字段与取值、不足样本的变体）派生。

evidence_summary 是整份 evidence 的观测指纹：同一业务问题在两次 run 之间证据
数值变化时 finding_key 保持不变、evidence_summary 改变，从而区分“同一持续问题
证据更新”与“不同问题”。

限制：对没有更细业务对象维度的聚合型 code（例如 data.unassigned_observation），
不同输入下的同类问题只能共享同一 finding_key；此时必须依靠 evidence_summary
与基准 run 回查，不能自动合并为完全相同的观测。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

KEY_LENGTH = 16


def canonical_json(value: Any) -> str:
    """确定性 JSON 序列化：键排序、无空白，供哈希使用。"""
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=_json_fallback,
    )


def _json_fallback(value: Any) -> Any:
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    if isinstance(value, tuple):
        return list(value)
    raise TypeError(f"无法规范化类型：{type(value)!r}")


def _subject_mutex(evidence: dict[str, Any]) -> list[Any]:
    conflicts = evidence.get("conflicts") or {}
    if isinstance(conflicts, dict):
        return sorted(str(key) for key in conflicts)
    return []


def _subject_sample(evidence: dict[str, Any]) -> list[Any]:
    variants = evidence.get("insufficient_variants") or []
    return sorted(str(item) for item in variants)


def _subject_reversal(evidence: dict[str, Any]) -> list[Any]:
    groups = evidence.get("groups") or []
    pairs = sorted(
        (str(group.get("field", "")), str(group.get("value", "")))
        for group in groups
        if isinstance(group, dict)
    )
    return pairs


def _subject_metric_key(evidence: dict[str, Any]) -> list[Any]:
    key = evidence.get("key")
    return [str(key)] if key else []


# 仅收录能标识业务对象的稳定字段；计数、p 值、效应量、样本 unit 列表等易变观测
# 一律不进入 finding_key，只通过 evidence_summary 体现。
_SUBJECT_EXTRACTORS = {
    "mutex.conflict": _subject_mutex,
    "sample.underpowered": _subject_sample,
    "stratification.reversal": _subject_reversal,
    "metric.negative_significant": _subject_metric_key,
    "metric.below_threshold": _subject_metric_key,
    "metric.fragile": _subject_metric_key,
}


def subject_parts(code: str, evidence: dict[str, Any]) -> list[Any]:
    """返回 evidence 中参与稳定身份的业务对象标识，没有则为空列表。"""
    extractor = _SUBJECT_EXTRACTORS.get(code)
    if extractor is not None:
        return extractor(evidence)
    if code.startswith("guardrail."):
        return _subject_metric_key(evidence)
    return []


def build_finding_identity(
    code: str, category: str, evidence: dict[str, Any]
) -> tuple[str, str]:
    """返回 (finding_key, evidence_summary)。"""
    identity_payload = {
        "code": code,
        "category": category,
        "subject": subject_parts(code, evidence or {}),
    }
    finding_key = hashlib.sha256(
        canonical_json(identity_payload).encode("utf-8")
    ).hexdigest()[:KEY_LENGTH]
    evidence_summary = hashlib.sha256(
        canonical_json(evidence or {}).encode("utf-8")
    ).hexdigest()[:KEY_LENGTH]
    return finding_key, evidence_summary
