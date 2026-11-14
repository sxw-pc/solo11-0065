"""声明式决策规则引擎。只认识布尔 flag，不执行任意表达式。"""

from __future__ import annotations

from typing import Any

from .models import DecisionSpec


def evaluate_condition(condition: dict[str, Any], flags: dict[str, bool]) -> bool:
    if "flag" in condition:
        return bool(flags.get(condition["flag"], False))
    if "all" in condition:
        return all(evaluate_condition(item, flags) for item in condition["all"])
    if "any" in condition:
        return any(evaluate_condition(item, flags) for item in condition["any"])
    if "none" in condition:
        return not any(evaluate_condition(item, flags) for item in condition["none"])
    raise ValueError("未知条件节点")


def referenced_flags(condition: dict[str, Any]) -> set[str]:
    if "flag" in condition:
        return {condition["flag"]}
    result: set[str] = set()
    for key in ("all", "any", "none"):
        for child in condition.get(key, []):
            result |= referenced_flags(child)
    return result


def decide(spec: DecisionSpec, flags: dict[str, bool]) -> dict[str, Any]:
    ordered = sorted(spec.rules, key=lambda rule: (-rule.priority, rule.id))
    trace: list[dict[str, Any]] = []
    selected = None
    for rule in ordered:
        matched = evaluate_condition(rule.condition, flags)
        trace.append(
            {
                "rule_id": rule.id,
                "card": rule.card,
                "priority": rule.priority,
                "reason": rule.reason,
                "matched": matched,
            }
        )
        if matched and selected is None:
            selected = rule
    if selected is not None:
        card_key = selected.card
        matched_rule = selected.id
        priority = selected.priority
        reason = selected.reason
    else:
        card_key = spec.fallback
        matched_rule = "fallback"
        priority = 0
        reason = "没有规则命中，使用兜底决策。"
    card = spec.cards[card_key]
    return {
        "card": card_key,
        "title": card.title,
        "action": card.action,
        "matched_rule": matched_rule,
        "priority": priority,
        "reason": reason,
        "trace": trace,
    }
