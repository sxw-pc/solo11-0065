"""审计编排：检查 -> 指标 -> 分层 -> flags -> 声明式决策。"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from .checks import (
    assess_data_quality,
    assess_exposure,
    assess_measurement_quality,
    assess_mutex,
    assess_split,
)
from .config import ConfigError
from .decision import decide, referenced_flags
from .evaluate import (
    build_flags,
    config_payload,
    guardrail_check,
    measurement_payload,
    metrics_payload,
    primary_check,
    sample_check,
)
from .metrics import (
    analysis_observations_for,
    collect_measurements,
    compute_all_metrics,
)
from .models import (
    Check,
    ExperimentConfig,
    Finding,
    Observation,
    check_sort_key,
    finding_sort_key,
)
from .stratify import assess_stratification

KNOWN_FLAGS_HINT = "请检查 decision.rules 中的 flag 名称"


class AuditError(ValueError):
    """数据不足以完成审计。"""


def _fragile_finding(primary: Any) -> Finding:
    return Finding(
        code="metric.fragile", severity="medium", category="metric",
        title="主指标效应不够稳健",
        detail="效应显著，但未同时通过阈值、置信区间和分层一致性检查。",
        evidence=asdict(primary),
    )


def _run_moment(generated_at: str | datetime | None, config: ExperimentConfig) -> datetime:
    if isinstance(generated_at, datetime):
        moment = generated_at
    elif generated_at:
        moment = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    else:
        return config.end
    # 统一成 UTC aware，避免与 CSV 中 aware 时间比较时报 naive/aware 错误。
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def run_audit(
    config: ExperimentConfig,
    assignments: list[Any],
    observations: list[Observation],
    *,
    run_id: str,
    generated_at: str,
    input_digest: str,
    config_digest: str,
) -> dict[str, Any]:
    primary_assignments = [
        row for row in assignments if row.experiment_id == config.experiment_id
    ]
    primary_observations = [
        row for row in observations if row.experiment_id == config.experiment_id
    ]
    if not primary_assignments:
        raise AuditError("没有找到本实验的分配记录")
    horizon_tracked = any(
        spec.outcome_horizon is not None
        for spec in (config.primary, *config.secondary, *config.guardrails)
    )
    if not primary_observations and not horizon_tracked:
        raise AuditError("没有找到本实验的曝光观测")
    # 配置了成熟期时，一个刚启动的 run 可能全部 cohort 仍在等待；
    # 这属于 pending，而不是「没有数据」错误，继续走审计流程。

    moment = _run_moment(generated_at, config)
    measurements = collect_measurements(
        config, primary_observations, primary_assignments, moment
    )
    measurement_tracked = any(
        item.get("outcome_horizon") is not None for item in measurements
    )

    checks: list[Check] = []
    findings: list[Finding] = []
    split_check, split_details, split_findings = assess_split(config, assignments)
    exposure_check, exposure_details, exposure_findings = assess_exposure(
        config, assignments, observations,
        # 固定追踪窗下，同一 unit 的多条时间快照是预期数据形态，
        # 只有同一时刻的重复行才算重复曝光。
        snapshot_mode=measurement_tracked,
    )
    mutex_check, mutex_details, mutex_findings = assess_mutex(config, assignments)
    primary, secondary, guardrails = compute_all_metrics(
        config, primary_observations, primary_assignments, moment
    )
    if measurement_tracked:
        # 分层复核与样本检查必须使用与主指标相同的成熟观测集，否则会把
        # 早于窗口的快照混入方向反转或样本充分性判断。
        primary_measurement = next(
            (item for item in measurements if item.get("role") == "primary"), {}
        )
        analysis_observations = analysis_observations_for(
            config, primary_observations, primary_assignments,
            config.primary, moment,
        )
    else:
        primary_measurement = {}
        analysis_observations = primary_observations
    quality_check, quality_details, quality_findings = assess_data_quality(
        config, assignments, observations,
        generated_at=moment, measurements=measurements,
    )
    sample_check_result, sample_findings, _ = sample_check(
        config, analysis_observations
    )
    checks.extend([
        split_check, exposure_check, mutex_check, quality_check, sample_check_result,
    ])
    findings.extend(
        split_findings + exposure_findings + mutex_findings
        + quality_findings + sample_findings
    )
    if measurement_tracked:
        measurement_check, measurement_details, measurement_findings = (
            assess_measurement_quality(config, measurements)
        )
        checks.append(measurement_check)
        findings.extend(measurement_findings)
    (stratification_fields, stratification_details,
     stratification_check, stratification_findings) = assess_stratification(
        config, analysis_observations, assignments, primary
    )
    checks.append(stratification_check)
    findings.extend(stratification_findings)

    provisional = (
        measurement_tracked
        and primary.measurement_status == "provisional"
    )
    favorable_share = float(stratification_details["favorable_share"])
    ci_favorable = (
        primary.ci_low > 0.0
        if config.primary.orientation == "higher_is_better"
        else primary.ci_high < 0.0
    )
    robust = bool(
        primary.significant
        and primary.favorable
        and primary.meets_threshold
        and ci_favorable
        and stratification_details["reversal_groups"] == 0
        and stratification_details["eligible_groups"] > 0
        and favorable_share >= config.stratification.min_favorable_share
        and not provisional
    )
    primary_check_result, primary_findings = primary_check(
        primary, robust, favorable_share
    )
    checks.append(primary_check_result)
    findings.extend(primary_findings)
    if (
        primary.favorable and primary.significant and not robust
        and primary.measurement_status != "provisional"
    ):
        guarded_codes = {
            "stratification.reversal", "metric.fragile",
            "metric.below_threshold",
        }
        if not any(item.code in guarded_codes for item in findings):
            findings.append(_fragile_finding(primary))

    guardrail_check_result, guardrail_findings = guardrail_check(guardrails)
    checks.append(guardrail_check_result)
    findings.extend(guardrail_findings)
    reversal = bool(stratification_details["reversal_groups"])
    measurement_evidence = measurement_payload(
        measurements,
        [asdict(check) for check in checks],
    )
    flags = build_flags(
        split_check, exposure_check, mutex_check, quality_check,
        sample_check_result, quality_details, primary, robust, guardrails, reversal,
        measurement_evidence,
    )
    referenced: set[str] = set()
    for rule in config.decision.rules:
        referenced |= referenced_flags(rule.condition)
    unknown = sorted(referenced - set(flags))
    if unknown:
        raise ConfigError(f"决策规则引用了未知 flag：{', '.join(unknown)}；{KNOWN_FLAGS_HINT}")

    decision = decide(config.decision, flags)
    if provisional:
        # 只要主指标仍有 pending cohort，就不能输出最终上线卡。
        # ship（以及依赖完整证据的 iterate）统一降级为人工复核；
        # stop 来自已经成熟的负向/护栏证据，保留以免弱化风险信号，
        # 但仍标记 provisional。
        ruled_card = decision["card"]
        decision = dict(decision)
        decision["provisional"] = True
        decision["rule_card"] = ruled_card
        if decision["card"] in ("ship", "iterate"):
            decision["card"] = "review"
            decision["title"] = config.decision.cards["review"].title
            decision["action"] = config.decision.cards["review"].action
            decision["reason"] = (
                "主指标仍有未成熟 cohort（pending），规则候选为 "
                f"{ruled_card}，决策标记为 provisional，"
                "需等待全部 unit 走完 outcome_horizon 后复核。"
            )
    else:
        decision = dict(decision)
        decision["provisional"] = False
        decision["rule_card"] = decision["card"]
    decision["measurement_status"] = (
        primary.measurement_status if measurement_tracked else "legacy"
    )
    checks.sort(key=check_sort_key)
    findings.sort(key=finding_sort_key)
    severity_counts = Counter(item.severity for item in findings)
    observed_by_variant = {
        variant.key: len({
            row.unit_id for row in primary_observations if row.variant == variant.key
        })
        for variant in config.variants
    }
    analysis_by_variant = {
        variant.key: int(primary.measurement.get("eligible_units_by_variant", {}).get(
            variant.key, 0
        ))
        for variant in config.variants
    } if measurement_tracked else {}
    counts = {
        "assignment_rows": len(primary_assignments),
        "unique_assignment_units": len({row.unit_id for row in primary_assignments}),
        "observation_rows": len(primary_observations),
        "unique_observation_units": len({row.unit_id for row in primary_observations}),
        "observed_units_by_variant": observed_by_variant,
        "duplicate_assignment_units": exposure_details["duplicate_assignment_units"],
        "duplicate_exposure_units": exposure_details["duplicate_exposure_units"],
        "mutex_conflict_units": mutex_details["conflict_units"],
        "observation_coverage": quality_details["observation_coverage"],
    }
    if measurement_tracked:
        # 聚合计数只以主指标为口径，避免把不同 horizon 的指标相加造成误解；
        # 每个指标自己的分母/等待/缺数在 measurement.metrics 中逐项给出。
        counts.update({
            "measurement_tracked": True,
            "maturity_coverage": primary_measurement.get("measurement_coverage", 0.0),
            "analysis_units_by_variant": analysis_by_variant,
            "pending_units": int(primary_measurement.get("pending_units", 0) or 0),
            "mature_but_missing_units": int(
                primary_measurement.get("mature_but_missing_units", 0) or 0
            ),
            "observed_before_horizon_units": int(
                primary_measurement.get("observed_before_horizon_units", 0) or 0
            ),
        })
    else:
        counts["measurement_tracked"] = False
    provisional_note = (
        "；主指标存在 pending cohort，决策为 provisional" if provisional else ""
    )
    return {
        "schema_version": 1,
        "run": {
            "run_id": run_id,
            "generated_at": generated_at,
            "provisional": provisional,
            "measurement_as_of": moment.isoformat(),
            "input_digest": input_digest,
            "config_digest": config_digest,
        },
        "experiment": config_payload(config),
        "counts": counts,
        "flags": dict(sorted(flags.items())),
        "checks": [asdict(check) for check in checks],
        "findings": [asdict(finding) for finding in findings],
        "finding_counts": dict(sorted(severity_counts.items())),
        "metrics": metrics_payload(primary, secondary, guardrails),
        "measurement": measurement_evidence,
        "stratification": {
            "fields": [
                {
                    "field": field["field"], "values": field["values"],
                    "truncated": field["truncated"],
                    "groups": [asdict(group) for group in field["groups"]],
                }
                for field in stratification_fields
            ],
            **{
                key: value for key, value in stratification_details.items()
                if key != "fields"
            },
        },
        "decision": decision,
        "reproducibility": {
            "deterministic": True,
            "input_digest": input_digest,
            "config_digest": config_digest,
            "note": "除 run_id 与 generated_at 外，相同输入与配置应产生相同审计内容。"
                    "结果成熟度按 run.generated_at 判定。",
        },
        "headline": (
            f"{config.name}：{decision['title']}；"
            f"{len(findings)} 条 finding，主指标效应 "
            f"{primary.absolute_effect:+.4f}{provisional_note}"
        ),
    }
