"""跨运行 finding 生命周期分类（纯函数，不依赖数据库）。

基准选择策略（已选定）：**沿相同 config_digest 找最近的成功 run，跨过配置变化
比较**，而不是只与紧邻成功 run 比较。

- 同 config_digest 下，finding_key 在上一个同配置 run 出现即 continuing；
  暂时消失（被 resolved）后再次出现记为 continuing 并置 reappeared，可回查到
  它上一次出现的 run，而不是被当成全新问题。
- 配置 digest 改变后，旧配置下的 finding 不会被误判为 resolved；在新配置里
  首次出现的同名 finding 标 new，run 级比较标 incomparable（紧邻 run 配置
  不同，无法直接比较），同时仍可沿同配置历史判断重现。
- 比较严格限定在同一 experiment_id 与同一 config_digest 之内，避免把不同
  输入下同名 code 自动合并。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 状态标签
STATUS_NEW = "new"
STATUS_CONTINUING = "continuing"
STATUS_RESOLVED = "resolved"
STATUS_INCOMPARABLE = "incomparable"

LIFECYCLE_STATUSES = (
    STATUS_NEW,
    STATUS_CONTINUING,
    STATUS_RESOLVED,
    STATUS_INCOMPARABLE,
)
LIFECYCLE_LABELS = {
    STATUS_NEW: "新出现",
    STATUS_CONTINUING: "持续存在",
    STATUS_RESOLVED: "已消退",
    STATUS_INCOMPARABLE: "不可比较",
}


@dataclass(frozen=True)
class PriorFinding:
    finding_key: str
    code: str
    severity: str
    title: str
    evidence_summary: str


@dataclass(frozen=True)
class RunSnapshot:
    run_id: str
    generated_at: str
    config_digest: str


@dataclass
class LifecycleEntry:
    finding_key: str
    status: str
    baseline_run_id: str | None
    previous_run_id: str | None
    first_seen_run_id: str
    prior_evidence_summary: str | None
    evidence_changed: bool
    reappeared: bool
    # 基准 run 与当前 run 之间跨过配置 digest 不同的 run（配置回滚后重现）。
    cross_config_gap: bool = False
    prior_finding: PriorFinding | None = None


@dataclass
class LifecycleResult:
    entries: dict[str, LifecycleEntry] = field(default_factory=dict)
    resolved: list[LifecycleEntry] = field(default_factory=list)


def classify_findings(
    current_run: RunSnapshot,
    current_findings: list[dict[str, Any]],
    same_config_runs: list[RunSnapshot],
    prior_findings_by_run: dict[str, list[PriorFinding]],
    first_seen_by_key: dict[str, str],
    cross_config_gap: bool = False,
) -> LifecycleResult:
    """计算本次 run 每个 finding 的生命周期状态及 resolved 项。

    参数约定：
    - same_config_runs：同 experiment、同 config_digest、严格早于当前 run 的
      成功 run，按时间升序（末尾为最近一次同配置 run，即基准 run）。
    - prior_findings_by_run：这些 run 的 finding 记录。
    - first_seen_by_key：同 config_digest 历史中每个 finding_key 最早出现的
      run_id（调用方从全量同配置历史汇总）。
    - cross_config_gap：当前 run 与同配置基准 run 之间是否存在配置 digest
      不同的 run（即配置回滚后再次审计）。
    """
    result = LifecycleResult()
    current_keys = {item["finding_key"] for item in current_findings}

    baseline = same_config_runs[-1] if same_config_runs else None
    baseline_keys = {
        item.finding_key
        for item in prior_findings_by_run.get(baseline.run_id, [])
    } if baseline else set()

    # 沿同配置历史（由近及远）找到每个 key 上一次真正出现的 run 与记录，
    # 自动跨过该 finding 缺失（已消退）的中间 run。
    previous_occurrence: dict[str, tuple[str, PriorFinding]] = {}
    for snapshot in reversed(same_config_runs):
        for prior in prior_findings_by_run.get(snapshot.run_id, []):
            previous_occurrence.setdefault(
                prior.finding_key, (snapshot.run_id, prior)
            )

    for item in current_findings:
        key = item["finding_key"]
        summary = item["evidence_summary"]
        occurrence = previous_occurrence.get(key)
        if occurrence is None:
            entry = LifecycleEntry(
                finding_key=key,
                status=STATUS_NEW,
                baseline_run_id=baseline.run_id if baseline else None,
                previous_run_id=None,
                first_seen_run_id=first_seen_by_key.get(key, current_run.run_id),
                prior_evidence_summary=None,
                evidence_changed=False,
                reappeared=False,
            )
        else:
            previous_run_id, prior = occurrence
            entry = LifecycleEntry(
                finding_key=key,
                status=STATUS_CONTINUING,
                baseline_run_id=baseline.run_id if baseline else None,
                previous_run_id=previous_run_id,
                first_seen_run_id=first_seen_by_key.get(key, current_run.run_id),
                prior_evidence_summary=prior.evidence_summary,
                evidence_changed=prior.evidence_summary != summary,
                # 最近同配置基准 run 中不存在 -> 问题曾短暂消失后再次出现
                reappeared=key not in baseline_keys,
                cross_config_gap=cross_config_gap,
                prior_finding=prior,
            )
        result.entries[key] = entry

    # 最近同配置基准 run 中存在、本次不再产生的 finding 记为 resolved。
    if baseline:
        for prior in prior_findings_by_run.get(baseline.run_id, []):
            if prior.finding_key not in current_keys:
                result.resolved.append(
                    LifecycleEntry(
                        finding_key=prior.finding_key,
                        status=STATUS_RESOLVED,
                        baseline_run_id=baseline.run_id,
                        previous_run_id=baseline.run_id,
                        first_seen_run_id=first_seen_by_key.get(
                            prior.finding_key, baseline.run_id
                        ),
                        prior_evidence_summary=prior.evidence_summary,
                        evidence_changed=False,
                        reappeared=False,
                        prior_finding=prior,
                    )
                )
    return result
