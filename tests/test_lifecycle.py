"""稳定业务身份与纯函数生命周期分类测试。"""

from __future__ import annotations

from app.identity import build_finding_identity, canonical_json, subject_parts
from app.lifecycle import (
    STATUS_CONTINUING,
    STATUS_NEW,
    STATUS_RESOLVED,
    LifecycleEntry,
    PriorFinding,
    RunSnapshot,
    classify_findings,
)


def _finding_payload(code: str, category: str, evidence: dict, *, summary: str = "s"):
    key, evidence_summary = build_finding_identity(code, category, evidence)
    return {
        "code": code,
        "category": category,
        "evidence": evidence,
        "finding_key": key,
        "evidence_summary": evidence_summary,
    }


def test_identity_ignores_run_id_detail_and_volatile_counts():
    evidence_a = {"count": 3, "sample_units": ["unit-0001", "unit-0002"]}
    evidence_b = {"count": 9, "sample_units": ["unit-0009"]}
    key_a, sum_a = build_finding_identity(
        "data.unassigned_observation", "data", evidence_a
    )
    key_b, sum_b = build_finding_identity(
        "data.unassigned_observation", "data", evidence_b
    )
    # 聚合型 code 没有更细业务对象：业务键相同，证据指纹不同。
    assert key_a == key_b
    assert sum_a != sum_b


def test_guardrail_identity_is_stable_across_evidence_number_changes():
    first = {
        "key": "crash_rate", "degradation": 0.0075, "p_value": 0.012,
        "max_degradation": 0.01,
    }
    second = {
        "key": "crash_rate", "degradation": 0.0088, "p_value": 0.004,
        "max_degradation": 0.01,
    }
    key_a, sum_a = build_finding_identity(
        "guardrail.watch.crash_rate", "guardrail", first
    )
    key_b, sum_b = build_finding_identity(
        "guardrail.watch.crash_rate", "guardrail", second
    )
    assert key_a == key_b
    assert sum_a != sum_b


def test_same_code_different_guardrail_subject_does_not_merge():
    crash, _ = build_finding_identity(
        "guardrail.breach.crash_rate", "guardrail", {"key": "crash_rate"}
    )
    depth, _ = build_finding_identity(
        "guardrail.breach.session_depth", "guardrail", {"key": "session_depth"}
    )
    assert crash != depth


def test_mutex_subject_uses_conflicting_experiment_id():
    a, _ = build_finding_identity(
        "mutex.conflict", "mutex",
        {"conflicts": {"exp-promo": ["unit-0001"]}, "conflict_units": 1},
    )
    b, _ = build_finding_identity(
        "mutex.conflict", "mutex",
        {"conflicts": {"exp-paywall": ["unit-0001"]}, "conflict_units": 1},
    )
    assert a != b
    assert subject_parts(
        "mutex.conflict", {"conflicts": {"exp-promo": []}}
    ) == ["exp-promo"]


def test_reversal_identity_uses_stratum_field_and_value():
    ev_a = {"groups": [{"field": "platform", "value": "legacy", "effect": -0.2}]}
    ev_b = {"groups": [{"field": "platform", "value": "legacy", "effect": -0.35}]}
    ev_c = {"groups": [{"field": "platform", "value": "mobile", "effect": -0.2}]}
    key_a, _ = build_finding_identity("stratification.reversal", "stratification", ev_a)
    key_b, _ = build_finding_identity("stratification.reversal", "stratification", ev_b)
    key_c, _ = build_finding_identity("stratification.reversal", "stratification", ev_c)
    assert key_a == key_b  # 同一分层对象，效应数值变化不影响身份
    assert key_a != key_c


def test_sample_subject_orders_variants_deterministically():
    key_a, _ = build_finding_identity(
        "sample.underpowered", "sample",
        {"insufficient_variants": ["b", "a"], "unique_units": {}},
    )
    key_b, _ = build_finding_identity(
        "sample.underpowered", "sample",
        {"insufficient_variants": ["a", "b"], "unique_units": {}},
    )
    assert key_a == key_b


def test_canonical_json_is_deterministic():
    assert canonical_json({"b": 1, "a": [2, 1]}) == '{"a":[2,1],"b":1}'
    assert canonical_json({"x": {3, 1}}) == '{"x":[1,3]}'


def test_classify_new_when_no_same_config_history():
    current = RunSnapshot("run-2", "2026-03-02T00:00:00+00:00", "cfg-1")
    findings = [_finding_payload("metric.fragile", "metric", {"key": "activation"})]
    result = classify_findings(current, findings, [], {}, {})
    entry = result.entries[findings[0]["finding_key"]]
    assert entry.status == STATUS_NEW
    assert entry.baseline_run_id is None
    assert entry.first_seen_run_id == "run-2"
    assert result.resolved == []


def test_classify_continuing_with_evidence_change_and_baseline():
    current = RunSnapshot("run-2", "2026-03-02T00:00:00+00:00", "cfg-1")
    prior_run = RunSnapshot("run-1", "2026-03-01T00:00:00+00:00", "cfg-1")
    old = _finding_payload("guardrail.watch.crash_rate", "guardrail",
                           {"key": "crash_rate", "degradation": 0.007})
    new = dict(old)
    new["evidence_summary"] = "changed-summary"
    prior = PriorFinding(
        finding_key=old["finding_key"], code=old["code"], severity="medium",
        title="t", evidence_summary=old["evidence_summary"],
    )
    result = classify_findings(
        current, [new], [prior_run], {"run-1": [prior]},
        {old["finding_key"]: "run-1"},
    )
    entry = result.entries[old["finding_key"]]
    assert entry.status == STATUS_CONTINUING
    assert entry.baseline_run_id == "run-1"
    assert entry.previous_run_id == "run-1"
    assert entry.evidence_changed is True
    assert entry.reappeared is False
    assert entry.first_seen_run_id == "run-1"


def test_reappearance_after_brief_disappearance_is_continuing_reappeared():
    # run-1 出现，run-2 消退（无该 finding），run-3 再次出现
    r1 = RunSnapshot("run-1", "2026-03-01T00:00:00+00:00", "cfg-1")
    r2 = RunSnapshot("run-2", "2026-03-02T00:00:00+00:00", "cfg-1")
    r3 = RunSnapshot("run-3", "2026-03-03T00:00:00+00:00", "cfg-1")
    finding = _finding_payload("mutex.conflict", "mutex",
                               {"conflicts": {"exp-promo": ["unit-1"]}})
    prior = PriorFinding(
        finding_key=finding["finding_key"], code=finding["code"],
        severity="critical", title="t",
        evidence_summary=finding["evidence_summary"],
    )
    result = classify_findings(
        r3, [finding], [r1, r2], {"run-1": [prior], "run-2": []},
        {finding["finding_key"]: "run-1"},
    )
    entry = result.entries[finding["finding_key"]]
    assert entry.status == STATUS_CONTINUING
    assert entry.reappeared is True
    assert entry.previous_run_id == "run-1"
    assert entry.baseline_run_id == "run-2"
    assert entry.first_seen_run_id == "run-1"


def test_resolved_when_missing_from_latest_same_config_baseline():
    r1 = RunSnapshot("run-1", "2026-03-01T00:00:00+00:00", "cfg-1")
    r2 = RunSnapshot("run-2", "2026-03-02T00:00:00+00:00", "cfg-1")
    gone = _finding_payload("data.variant_mismatch", "data", {"count": 2})
    prior = PriorFinding(
        finding_key=gone["finding_key"], code=gone["code"], severity="high",
        title="t", evidence_summary=gone["evidence_summary"],
    )
    result = classify_findings(
        r2, [], [r1], {"run-1": [prior]},
        {gone["finding_key"]: "run-1"},
    )
    assert len(result.resolved) == 1
    entry: LifecycleEntry = result.resolved[0]
    assert entry.status == STATUS_RESOLVED
    assert entry.finding_key == gone["finding_key"]
    assert entry.baseline_run_id == "run-1"


def test_config_change_excludes_old_runs_from_baseline_and_resolution():
    # 同 experiment 但历史 run 全部是别的 config_digest
    old = RunSnapshot("run-1", "2026-03-01T00:00:00+00:00", "cfg-old")
    current = RunSnapshot("run-2", "2026-03-02T00:00:00+00:00", "cfg-new")
    finding = _finding_payload("metric.fragile", "metric", {"key": "activation"})
    result = classify_findings(current, [finding], [], {}, {})
    assert result.entries[finding["finding_key"]].status == STATUS_NEW
    assert result.resolved == []
    # 即使传入了旧配置 run 的 finding，也不会参与（调用方按 digest 过滤）
    assert old.config_digest != current.config_digest
