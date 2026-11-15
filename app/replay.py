"""离线回放：用 run 绑定的快照字节重算，并与原 run 的记录逐项核对。

硬约束：
- 只从该 run 自己的 ``snapshot/`` 目录取输入，绝不回退读取当前源文件；
- 重算结果只写入同目录下的 ``replay_status.json`` 与台账回放列，
  原 audit.json / decision_card.json / report.md / report.html 永不覆盖；
- 比对只忽略 run_id 与 generated_at；统计、findings、检查、规则命中与决策
  以及输入/配置 digest 全部要求一致。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .audit import run_audit
from .config import load_config_from_bytes
from .data import load_dataset_from_bytes
from . import ledger
from .snapshot import (
    ROLE_ASSIGNMENTS,
    ROLE_CONFIG,
    ROLE_OBSERVATIONS,
    REASON_LEGACY,
    SnapshotError,
    read_snapshot_bytes,
    result_block_from_manifest,
    snapshot_status,
)

REPLAY_FILENAME = "replay_status.json"

# 回放结论状态词表（稳定字符串，供 Web、CLI 与测试引用）。
STATUS_MATCH = "match"
STATUS_MISMATCH = "mismatch"
STATUS_UNREPLAYABLE = "unreplayable"
STATUS_REPLAY_ERROR = "replay_error"

# 与原记录比对时必须忽略、且仅允许忽略的键。
IGNORED_RUN_KEYS = ("run_id", "generated_at")
COMPARED_TOP_LEVEL = (
    "schema_version",
    "experiment",
    "counts",
    "flags",
    "checks",
    "findings",
    "finding_counts",
    "metrics",
    "stratification",
    "decision",
    "headline",
    "snapshot",
    "reproducibility",
)


@dataclass
class ReplayOutcome:
    run_id: str
    status: str
    mismatch_count: int | None
    detail: dict[str, Any]
    replayable: bool
    snapshot_bound: bool

    def to_record(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "replayable": self.replayable,
            "snapshot_bound": self.snapshot_bound,
            "mismatch_count": self.mismatch_count,
            "detail": self.detail,
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_original_audit(run_dir: Path) -> dict[str, Any]:
    return json.loads((run_dir / "audit.json").read_text(encoding="utf-8"))


def _diff_values(path: str, expected: Any, actual: Any) -> list[dict[str, Any]]:
    """递归深度比对，输出稳定的差异路径列表。"""
    if isinstance(expected, dict) and isinstance(actual, dict):
        diffs: list[dict[str, Any]] = []
        for key in sorted(set(expected) | set(actual)):
            child = f"{path}.{key}" if path else key
            if key not in expected:
                diffs.append({"path": child, "kind": "unexpected_in_replay",
                              "expected": None, "actual": actual[key]})
            elif key not in actual:
                diffs.append({"path": child, "kind": "missing_in_replay",
                              "expected": expected[key], "actual": None})
            else:
                diffs.extend(_diff_values(child, expected[key], actual[key]))
        return diffs
    if isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            return [{"path": path, "kind": "length",
                     "expected": len(expected), "actual": len(actual)}]
        diffs = []
        for index, (left, right) in enumerate(zip(expected, actual)):
            diffs.extend(_diff_values(f"{path}[{index}]", left, right))
        return diffs
    if expected != actual:
        return [{"path": path, "kind": "value",
                 "expected": expected, "actual": actual}]
    return []


def _compare_results(
    original: dict[str, Any], replayed: dict[str, Any]
) -> list[dict[str, Any]]:
    diffs: list[dict[str, Any]] = []
    original_run = {
        key: value for key, value in original["run"].items()
        if key not in IGNORED_RUN_KEYS
    }
    replayed_run = {
        key: value for key, value in replayed["run"].items()
        if key not in IGNORED_RUN_KEYS
    }
    diffs.extend(_diff_values("run", original_run, replayed_run))
    for key in COMPARED_TOP_LEVEL:
        if key not in original or key not in replayed:
            diffs.append({"path": key, "kind": "missing_section",
                          "expected": key in original, "actual": key in replayed})
            continue
        diffs.extend(_diff_values(key, original[key], replayed[key]))
    # reproducibility 块只含 digest 与固定说明文字，同样要求一致；
    # 旧报告可能没有 snapshot 块，缺失时由逐键 diff 自然记录，不做豁免。
    return diffs


def replay_run(run_dir: Path, db_path: Path | None = None) -> ReplayOutcome:
    """对单个 run 执行离线回放并落盘结论。"""
    run_dir = Path(run_dir)
    run_id = run_dir.name
    checked_at = _now()
    status_info = snapshot_status(run_dir)

    if not status_info["replayable"]:
        detail = {
            "reason": status_info["reason"],
            "snapshot_bound": status_info["snapshot_bound"],
            "checked_at": checked_at,
            "note": "该 run 没有可用的绑定输入快照；保留原报告，且不使用当前源文件替补。",
        }
        outcome = ReplayOutcome(
            run_id=run_id,
            status=STATUS_UNREPLAYABLE,
            mismatch_count=None,
            detail=detail,
            replayable=False,
            snapshot_bound=status_info["snapshot_bound"],
        )
        _persist(run_dir, db_path, outcome, checked_at)
        return outcome

    try:
        original = _load_original_audit(run_dir)
        manifest, payload = read_snapshot_bytes(run_dir)
        config = load_config_from_bytes(payload[ROLE_CONFIG], manifest_config_name(manifest))
        dataset = load_dataset_from_bytes(
            config,
            payload[ROLE_ASSIGNMENTS],
            manifest_name(manifest, ROLE_ASSIGNMENTS),
            payload[ROLE_OBSERVATIONS],
            manifest_name(manifest, ROLE_OBSERVATIONS),
        )
        # run_id / generated_at 是允许漂移的唯一项；复用原值让其余输出直接可比。
        replayed = run_audit(
            config,
            dataset.assignments,
            dataset.observations,
            run_id=original["run"]["run_id"],
            generated_at=original["run"]["generated_at"],
            input_digest=manifest["input_digest"],
            config_digest=manifest["config_digest"],
            snapshot=result_block_from_manifest(
                manifest["input_digest"], manifest["config_digest"], manifest
            ),
        )
        diffs = _compare_results(original, replayed)
        digest_checks = {
            "input_digest": manifest["input_digest"] == original["run"]["input_digest"],
            "config_digest": manifest["config_digest"] == original["run"]["config_digest"],
        }
        if not all(digest_checks.values()):
            for key, ok in digest_checks.items():
                if not ok:
                    diffs.append({
                        "path": f"run.{key}",
                        "kind": "value",
                        "expected": original["run"][key],
                        "actual": manifest[key],
                    })
        if diffs:
            outcome = ReplayOutcome(
                run_id=run_id,
                status=STATUS_MISMATCH,
                mismatch_count=len(diffs),
                detail={
                    "checked_at": checked_at,
                    "manifest_input_digest": manifest["input_digest"],
                    "recorded_input_digest": original["run"]["input_digest"],
                    "ignored_keys": list(IGNORED_RUN_KEYS),
                    "diffs": diffs,
                },
                replayable=True,
                snapshot_bound=True,
            )
        else:
            outcome = ReplayOutcome(
                run_id=run_id,
                status=STATUS_MATCH,
                mismatch_count=0,
                detail={
                    "checked_at": checked_at,
                    "manifest_input_digest": manifest["input_digest"],
                    "recorded_input_digest": original["run"]["input_digest"],
                    "ignored_keys": list(IGNORED_RUN_KEYS),
                    "diffs": [],
                },
                replayable=True,
                snapshot_bound=True,
            )
    except (OSError, ValueError, KeyError, SnapshotError) as exc:
        outcome = ReplayOutcome(
            run_id=run_id,
            status=STATUS_REPLAY_ERROR,
            mismatch_count=None,
            detail={"checked_at": checked_at, "error": str(exc)},
            replayable=status_info["replayable"],
            snapshot_bound=True,
        )
    _persist(run_dir, db_path, outcome, checked_at)
    return outcome


def manifest_config_name(manifest: dict[str, Any]) -> str:
    return manifest_name(manifest, ROLE_CONFIG)


def manifest_name(manifest: dict[str, Any], role: str) -> str:
    for entry in manifest["files"]:
        if entry["role"] == role:
            return entry["name"]
    raise KeyError(f"快照清单缺少角色 {role!r}")


def _persist(
    run_dir: Path,
    db_path: Path | None,
    outcome: ReplayOutcome,
    checked_at: str,
) -> None:
    record = outcome.to_record()
    record["written_at"] = checked_at
    temporary = run_dir / f"{REPLAY_FILENAME}.tmp"
    temporary.write_text(
        json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(run_dir / REPLAY_FILENAME)
    if db_path is not None:
        try:
            ledger.update_replay_status(
                db_path,
                outcome.run_id,
                replay_status=outcome.status,
                checked_at=checked_at,
                mismatch_count=outcome.mismatch_count,
                detail=outcome.detail,
                snapshot_bound=outcome.snapshot_bound,
            )
        except (KeyError, OSError):
            # 台账缺失该 run（例如台账尚未提交的中断场景）时，文件结论仍然落盘，
            # 不影响快照自身的可回查性。
            pass


def load_replay_record(run_dir: Path) -> dict[str, Any] | None:
    path = run_dir / REPLAY_FILENAME
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def is_legacy(run_dir: Path) -> bool:
    return snapshot_status(run_dir)["reason"] == REASON_LEGACY
