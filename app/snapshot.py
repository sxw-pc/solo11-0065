"""输入快照：把一次 run 的解析对象、digest、产物与台账绑定到同一份不可变字节。

策略选型（两选一的前者）：**先复制到 run 暂存目录并从副本加载，完整后原子发布清单**。

- 源文件在 ``read_bytes`` 之后被并发替换也无关：解析与哈希都只针对已落盘的副本字节，
  同一次 run 的解析对象、input/config digest、归档文件和台账登记由同一份快照决定。
- 暂存目录位于 out 根下的 ``.staging-*`` 隐藏目录，在原子 rename 之前 Web 端永远看不到；
  进程在任何阶段退出都只留下一个可清理的暂存目录，不会被台账当成完整 run。
- 代价是磁盘：大输入只需再存三份输入字节的副本，峰值内存不随输入规模膨胀。
  （另一方案是把全部输入固定为内存字节快照，可避免副本解析期间被替换，但大输入可能因
  峰值内存过高而无法完成审计，故不采用。）
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .safety import resolve_under

SNAPSHOT_DIRNAME = "snapshot"
MANIFEST_NAME = "input_manifest.json"
SNAPSHOT_VERSION = 1
STAGING_PREFIX = ".staging-"

ROLE_CONFIG = "config"
ROLE_ASSIGNMENTS = "assignments"
ROLE_OBSERVATIONS = "observations"
ROLE_ORDER = (ROLE_CONFIG, ROLE_ASSIGNMENTS, ROLE_OBSERVATIONS)

# 不可重放原因码，Web/CLI/测试共用同一套稳定词表。
REASON_LEGACY = "legacy_no_snapshot"
REASON_MANIFEST_UNREADABLE = "manifest_unreadable"
REASON_FILE_MISSING = "snapshot_file_missing"
REASON_HASH_MISMATCH = "snapshot_hash_mismatch"
REASON_DIGEST_MISMATCH = "snapshot_digest_mismatch"


class SnapshotError(ValueError):
    """快照捕获、发布或校验失败。"""


@dataclass(frozen=True)
class InputFile:
    role: str
    source: Path
    logical_name: str


@dataclass(frozen=True)
class InputSnapshot:
    """暂存期的快照句柄。``staging_dir`` 在发布后即不再存在。"""

    staging_dir: Path
    files: tuple[InputFile, ...]
    bytes_by_role: dict[str, bytes]
    input_digest: str
    config_digest: str

    def bytes_for(self, role: str) -> bytes:
        return self.bytes_by_role[role]

    def name_for(self, role: str) -> str:
        return self._name(role)

    def _name(self, role: str) -> str:
        for item in self.files:
            if item.role == role:
                return item.logical_name
        raise KeyError(f"快照中没有角色 {role!r}")

    def manifest_payload(self, root: Path) -> dict[str, Any]:
        return {
            "snapshot_version": SNAPSHOT_VERSION,
            "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "input_digest": self.input_digest,
            "config_digest": self.config_digest,
            "files": [
                {
                    "role": item.role,
                    "name": item.logical_name,
                    "source_path": _display_path(root, item.source),
                    "size": len(self.bytes_by_role[item.role]),
                    "sha256": hashlib.sha256(
                        self.bytes_by_role[item.role]
                    ).hexdigest(),
                }
                for item in self.files
            ],
        }

    def result_block(self) -> dict[str, Any]:
        """写入 audit.json 的确定性快照块（不含时间戳，回放可逐字节比对）。"""
        return result_block_from_manifest(
            self.input_digest,
            self.config_digest,
            {"files": [
                {
                    "role": item.role,
                    "name": item.logical_name,
                    "size": len(self.bytes_by_role[item.role]),
                    "sha256": hashlib.sha256(
                        self.bytes_by_role[item.role]
                    ).hexdigest(),
                }
                for item in self.files
            ]},
        )


def result_block_from_manifest(
    input_digest: str, config_digest: str, manifest: dict[str, Any]
) -> dict[str, Any]:
    """从清单重建写入 audit.json 的确定性快照块（回放路径使用）。"""
    return {
        "snapshot_version": SNAPSHOT_VERSION,
        "bound": True,
        "input_digest": input_digest,
        "config_digest": config_digest,
        "manifest": MANIFEST_NAME,
        "directory": SNAPSHOT_DIRNAME,
        "files": [
            {
                "role": item["role"],
                "name": item["name"],
                "size": item["size"],
                "sha256": item["sha256"],
            }
            for item in manifest["files"]
        ],
    }


def digest_inputs(members: Iterable[tuple[str, bytes]]) -> str:
    """与旧 ``sha256_files`` 完全一致的哈希框架：name\\0bytes\\0 顺序拼接。"""
    digest = hashlib.sha256()
    for name, data in members:
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(data)
        digest.update(b"\0")
    return digest.hexdigest()


def _write_bytes_atomic(path: Path, data: bytes) -> None:
    temporary = path.with_name(f"{path.name}.tmp-{uuid.uuid4().hex[:8]}")
    with temporary.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _fsync_dir(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fresh_staging_dir(output_root: Path) -> Path:
    output_root.mkdir(parents=True, exist_ok=True)
    for _ in range(8):
        candidate = output_root / f"{STAGING_PREFIX}{uuid.uuid4().hex}"
        try:
            candidate.mkdir(parents=False, exist_ok=False)
            return candidate
        except FileExistsError:
            continue
    raise SnapshotError("无法分配唯一的快照暂存目录")


def _display_path(root: Path, target: Path) -> str:
    try:
        return target.relative_to(root).as_posix()
    except ValueError:
        return target.as_posix()


def capture_snapshot(
    root: Path,
    config_path: Path,
    assignments_path: Path,
    observations_path: Path,
    output_root: Path,
) -> InputSnapshot:
    """把三份输入一次性复制进暂存目录，并从同一份字节计算全部 digest。

    任何失败都清理暂存目录后向上抛出，不留下半成品。
    """
    targets = (
        InputFile(ROLE_CONFIG,
                  resolve_under(root, config_path, must_exist=True),
                  Path(config_path).name),
        InputFile(ROLE_ASSIGNMENTS,
                  resolve_under(root, assignments_path, must_exist=True),
                  Path(assignments_path).name),
        InputFile(ROLE_OBSERVATIONS,
                  resolve_under(root, observations_path, must_exist=True),
                  Path(observations_path).name),
    )
    names = [item.logical_name for item in targets]
    if len(set(names)) != len(names):
        raise SnapshotError("三份输入文件名必须互不相同，才能共存于快照目录")

    staging = _fresh_staging_dir(output_root)
    try:
        snap_dir = staging / SNAPSHOT_DIRNAME
        snap_dir.mkdir(parents=False, exist_ok=False)
        payload: dict[str, bytes] = {}
        for item in targets:
            # 单次读取：落盘副本、后续 YAML/CSV 解析与所有哈希共用这一份字节，
            # 读取之后源文件再被替换也不会影响本次 run。
            data = item.source.read_bytes()
            _write_bytes_atomic(snap_dir / item.logical_name, data)
            payload[item.role] = data
        input_digest = digest_inputs(
            ((item.logical_name, payload[item.role]) for item in targets)
        )
        config_digest = hashlib.sha256(payload[ROLE_CONFIG]).hexdigest()
        snapshot = InputSnapshot(
            staging_dir=staging,
            files=targets,
            bytes_by_role=payload,
            input_digest=input_digest,
            config_digest=config_digest,
        )
        manifest = snapshot.manifest_payload(Path(root).resolve())
        _write_bytes_atomic(
            staging / MANIFEST_NAME,
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8"),
        )
        _fsync_dir(snap_dir)
        _fsync_dir(staging)
        return snapshot
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def publish_snapshot(staging_dir: Path, run_dir: Path) -> None:
    """暂存目录整体原子 rename 为正式 run 目录；目标已存在时拒绝覆盖。"""
    if run_dir.exists():
        raise SnapshotError(f"run 目录已存在，拒绝覆盖：{run_dir.name}")
    if staging_dir.parent != run_dir.parent:
        raise SnapshotError("暂存目录与 run 目录必须位于同一输出根，才能原子发布")
    os.replace(staging_dir, run_dir)
    _fsync_dir(run_dir.parent)


def discard_snapshot(staging_dir: Path) -> None:
    """审计或产物写入失败时回滚：只清理本次 run 自己的暂存目录。"""
    if staging_dir.name.startswith(STAGING_PREFIX):
        shutil.rmtree(staging_dir, ignore_errors=True)


def load_manifest(run_dir: Path) -> dict[str, Any]:
    path = run_dir / MANIFEST_NAME
    with path.open("r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    if not isinstance(manifest, dict) or "files" not in manifest:
        raise SnapshotError("快照清单结构不合法")
    return manifest


def verify_snapshot(run_dir: Path) -> dict[str, Any]:
    """校验 run 目录内的快照完整且自洽，返回清单；失败时抛 :class:`SnapshotError`。"""
    manifest = load_manifest(run_dir)
    snap_dir = run_dir / SNAPSHOT_DIRNAME
    members: list[tuple[str, bytes]] = []
    for entry in manifest["files"]:
        target = snap_dir / entry["name"]
        if not target.is_file():
            raise SnapshotError(f"{REASON_FILE_MISSING}: {entry['name']}")
        data = target.read_bytes()
        actual = hashlib.sha256(data).hexdigest()
        if actual != entry["sha256"]:
            raise SnapshotError(f"{REASON_HASH_MISMATCH}: {entry['name']}")
        if len(data) != entry["size"]:
            raise SnapshotError(f"{REASON_HASH_MISMATCH}: {entry['name']} 大小不一致")
        members.append((entry["name"], data))
    if digest_inputs(members) != manifest["input_digest"]:
        raise SnapshotError(REASON_DIGEST_MISMATCH)
    config_entry = next(
        item for item in manifest["files"] if item["role"] == ROLE_CONFIG
    )
    if hashlib.sha256(
        (snap_dir / config_entry["name"]).read_bytes()
    ).hexdigest() != manifest["config_digest"]:
        raise SnapshotError(REASON_DIGEST_MISMATCH)
    return manifest


def read_snapshot_bytes(run_dir: Path) -> tuple[dict[str, Any], dict[str, bytes]]:
    """校验通过后按角色返回快照字节，回放只能从这里取输入。"""
    manifest = verify_snapshot(run_dir)
    snap_dir = run_dir / SNAPSHOT_DIRNAME
    payload = {
        entry["role"]: (snap_dir / entry["name"]).read_bytes()
        for entry in manifest["files"]
    }
    return manifest, payload


def snapshot_status(run_dir: Path) -> dict[str, Any]:
    """供 Web/CLI 使用的快照可回放状态判定，不抛异常。

    旧 run（没有清单）继续可查询，但明确标记为不可重放；绝不回退读取当前源文件。
    """
    manifest_path = run_dir / MANIFEST_NAME
    if not manifest_path.is_file():
        return {
            "snapshot_bound": False,
            "replayable": False,
            "reason": REASON_LEGACY,
            "manifest": None,
        }
    try:
        verify_snapshot(run_dir)
    except SnapshotError as exc:
        reason = str(exc).split(":", 1)[0]
        return {
            "snapshot_bound": True,
            "replayable": False,
            "reason": reason,
            "manifest": _safe_manifest(run_dir),
        }
    except (OSError, ValueError) as exc:
        return {
            "snapshot_bound": True,
            "replayable": False,
            "reason": REASON_MANIFEST_UNREADABLE,
            "manifest": None,
            "detail": str(exc),
        }
    return {
        "snapshot_bound": True,
        "replayable": True,
        "reason": None,
        "manifest": _safe_manifest(run_dir),
    }


def _safe_manifest(run_dir: Path) -> dict[str, Any] | None:
    try:
        return load_manifest(run_dir)
    except (OSError, ValueError):
        return None
