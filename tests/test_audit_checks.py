from dataclasses import replace
from datetime import datetime, timezone
from app.audit import run_audit
from app.checks import (
    assess_data_quality,
    assess_exposure,
    assess_mutex,
    assess_split,
)
from app.metrics import compute_metric
from app.models import Assignment, Observation


def test_audit_is_reproducible(config, dataset, audit_result):
    digest = audit_result["run"]["input_digest"]
    config_digest = audit_result["run"]["config_digest"]
    first = run_audit(config, dataset.assignments, dataset.observations,
                      run_id="run-test-repro-a", generated_at="2026-03-16T00:00:00+00:00",
                      input_digest=digest, config_digest=config_digest)
    second = run_audit(config, dataset.assignments, dataset.observations,
                       run_id="run-test-repro-b", generated_at="2026-03-17T00:00:00+00:00",
                       input_digest=digest, config_digest=config_digest)
    first.pop("run")
    second.pop("run")
    assert first == second
def test_full_audit_contract(audit_result):
    assert audit_result["decision"]["card"] == "iterate"
    assert audit_result["decision"]["matched_rule"] == "iterate-evidence"
    assert audit_result["metrics"]["primary"]["absolute_effect"] == 0.05
    assert audit_result["metrics"]["primary"]["p_value"] < 0.05
    assert audit_result["metrics"]["primary"]["meets_threshold"] is True
    assert audit_result["counts"]["unique_observation_units"] == 1600
    assert audit_result["counts"]["observation_coverage"] == 1.0
    assert len(audit_result["checks"]) == 8
    assert {finding["code"] for finding in audit_result["findings"]} == {
        "stratification.reversal",
        "guardrail.watch.crash_rate",
        "metric.fragile",
    }
    assert audit_result["flags"]["guardrail_watch"] is True
    assert audit_result["flags"]["no_stratified_reversal"] is False
    assert audit_result["decision"]["trace"][-1]["matched"] is True
def test_reversal_is_in_legacy_platform(audit_result):
    platforms = next(
        field for field in audit_result["stratification"]["fields"]
        if field["field"] == "platform"
    )
    legacy = next(group for group in platforms["groups"] if group["value"] == "legacy")
    assert legacy["control_n"] == 120
    assert legacy["treatment_n"] == 120
    assert legacy["absolute_effect"] < 0
    assert legacy["eligible"] is True
    assert legacy["reversal"] is True
    assert audit_result["stratification"]["favorable_share"] < 0.75
def test_guardrail_watch_threshold(audit_result):
    guardrail = audit_result["metrics"]["guardrails"][0]
    assert guardrail["guardrail_status"] == "watch"
    assert guardrail["degradation"] == 0.0075
    assert guardrail["max_degradation"] == 0.01

def test_duplicate_exposure_is_detected(config, dataset):
    observations = list(dataset.observations)
    primary = next(
        row for row in observations if row.experiment_id == config.experiment_id
    )
    observations.append(replace(primary))
    check, details, findings = assess_exposure(
        config, dataset.assignments, observations
    )
    assert check.status == "fail"
    assert details["duplicate_exposure_units"] == 1
    assert findings[0].code == "exposure.duplicate"


def test_mutex_conflict_is_detected(config, dataset):
    primary = next(
        row for row in dataset.assignments
        if row.experiment_id == config.experiment_id
    )
    conflict = Assignment(
        unit_id=primary.unit_id,
        experiment_id=config.mutexes[0].experiment_id,
        variant="control",
        assigned_at=datetime(2026, 3, 2, tzinfo=timezone.utc),
        strata=dict(primary.strata),
    )
    check, details, findings = assess_mutex(config, [*dataset.assignments, conflict])
    assert check.status == "fail"
    assert details["conflict_units"] == 1
    assert findings[0].severity == "critical"


def test_unassigned_observation_breaks_quality(config, dataset):
    base = next(
        row for row in dataset.observations
        if row.experiment_id == config.experiment_id
    )
    ghost = replace(base, unit_id="unit-9999")
    check, details, findings = assess_data_quality(
        config, dataset.assignments, [*dataset.observations, ghost]
    )
    assert check.status == "fail"
    assert details["unassigned_observation_units"] == 1
    assert any(item.code == "data.unassigned_observation" for item in findings)


def test_split_imbalance_is_detected(config, dataset):
    primary = [
        row for row in dataset.assignments
        if row.experiment_id == config.experiment_id
    ]
    unbalanced = [
        row for row in primary
        if row.variant == "control"
    ][:700] + [
        row for row in primary
        if row.variant == "treatment"
    ][:500]
    check, details, findings = assess_split(config, unbalanced)
    assert check.status == "fail"
    assert details["counts"] == {"control": 700, "treatment": 500}
    assert findings[0].code == "split.imbalance"


def test_negative_primary_metric(config):
    stamp = datetime(2026, 3, 2, tzinfo=timezone.utc)
    observations = [
        Observation(f"unit-{index:04d}", config.experiment_id, "control", stamp,
                    {config.primary.key: 1.0})
        for index in range(1, 121)
    ] + [
        Observation(f"unit-{index:04d}", config.experiment_id, "treatment", stamp,
                    {config.primary.key: 0.0})
        for index in range(121, 241)
    ]
    result = compute_metric(config, observations, config.primary, "primary")
    assert result.absolute_effect == -1.0
    assert result.significant is True
    assert result.favorable is False
