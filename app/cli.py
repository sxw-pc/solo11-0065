"""命令行入口：demo / audit / replay / serve / ledger。"""

from __future__ import annotations

import argparse
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import ledger, replay, snapshot
from .audit import run_audit
from .config import ConfigError, load_config_from_bytes
from .data import DataValidationError, load_dataset_from_bytes
from .reporting import write_artifacts
from .safety import PathSecurityError, ensure_local_host, ensure_output_path, resolve_under
from .snapshot import (
    ROLE_ASSIGNMENTS,
    ROLE_CONFIG,
    ROLE_OBSERVATIONS,
)
from .replay import (
    STATUS_MATCH,
    STATUS_UNREPLAYABLE,
    replay_run,
)
from .web import create_app

DEFAULT_CONFIG = "fixtures/experiment.yaml"
DEFAULT_ASSIGNMENTS = "fixtures/assignments.csv"
DEFAULT_OBSERVATIONS = "fixtures/observations.csv"
DEFAULT_OUT = "out"
DEFAULT_DB = "data/ledger.sqlite3"


def _root(value: str) -> Path:
    return Path(value).resolve()


def _run_pipeline(
    root: Path,
    config_path: Path,
    assignments_path: Path,
    observations_path: Path,
    out_dir: Path,
    db_path: Path,
) -> tuple[dict[str, Any], Path, Path]:
    # 路径约束先行：后续所有文件访问都在 root 内。
    output_root = resolve_under(root, out_dir)
    ledger_target = ensure_output_path(root, db_path)

    # 第一步即把三份输入固定为暂存快照。read_bytes 之后源文件再被替换，
    # 解析、digest、归档与台账登记也全部只认这一份字节，消除读文件与哈希之间的竞态。
    captured = snapshot.capture_snapshot(
        root, config_path, assignments_path, observations_path, output_root
    )
    try:
        config = load_config_from_bytes(
            captured.bytes_for(ROLE_CONFIG), captured.name_for(ROLE_CONFIG)
        )
        dataset = load_dataset_from_bytes(
            config,
            captured.bytes_for(ROLE_ASSIGNMENTS),
            captured.name_for(ROLE_ASSIGNMENTS),
            captured.bytes_for(ROLE_OBSERVATIONS),
            captured.name_for(ROLE_OBSERVATIONS),
        )
        now = datetime.now(timezone.utc)
        run_id = f"run-{now:%Y%m%dT%H%M%S%fZ}-{captured.input_digest[:8]}"
        result = run_audit(
            config,
            dataset.assignments,
            dataset.observations,
            run_id=run_id,
            generated_at=now.isoformat(timespec="seconds"),
            input_digest=captured.input_digest,
            config_digest=captured.config_digest,
            snapshot=captured.result_block(),
        )
        run_dir = output_root / run_id
        # 报告先全部写进暂存目录；任何失败都只回滚暂存，正式 out/ 不出现半成品。
        write_artifacts(result, captured.staging_dir)
        # 暂存目录整体原子发布：网页只能看到带齐全套产物与快照的 run 目录。
        snapshot.publish_snapshot(captured.staging_dir, run_dir)
    except BaseException:
        snapshot.discard_snapshot(captured.staging_dir)
        raise

    # 台账是“完整 run”的唯一登记处。若进程在这之前退出，run 目录不会被网页查询到；
    # 若在这之后快照丢失，回放会把该 run 标为不可重放，并保留旧报告、不替补当前源文件。
    relative = run_dir.relative_to(Path(root).resolve()).as_posix()
    ledger.save_run(ledger_target, result, relative, snapshot_bound=True)
    return result, run_dir, ledger_target


def _print_result(result: dict[str, Any], run_dir: Path, db_path: Path) -> None:
    decision = result["decision"]
    print(f"决策：{decision['title']} ({decision['card']}) · 规则 {decision['matched_rule']}")
    print(f"run_id：{result['run']['run_id']}")
    print(f"finding：{len(result['findings'])} 条；输入摘要 {result['run']['input_digest'][:16]}…")
    print(f"产物目录：{run_dir}")
    print("  audit.json / decision_card.json / report.md / report.html")
    print("  input_manifest.json / snapshot/（绑定输入，支持离线 replay）")
    print(f"SQLite 台账：{db_path}")


def _cmd_demo(args: argparse.Namespace) -> int:
    root = _root(args.root)
    result, run_dir, db_path = _run_pipeline(
        root,
        Path(args.config),
        Path(args.assignments),
        Path(args.observations),
        Path(args.out),
        Path(args.db),
    )
    _print_result(result, run_dir, db_path)
    print("下一步：python -m app serve --port 5000")
    return 0


def _cmd_audit(args: argparse.Namespace) -> int:
    root = _root(args.root)
    result, run_dir, db_path = _run_pipeline(
        root,
        Path(args.experiment),
        Path(args.assignments),
        Path(args.observations),
        Path(args.out),
        Path(args.db),
    )
    _print_result(result, run_dir, db_path)
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    if not 1 <= args.port <= 65535:
        raise ValueError("port 必须在 1-65535 之间")
    root = _root(args.root)
    out_dir = resolve_under(root, Path(args.out))
    db_path = ensure_output_path(root, Path(args.db))
    host = ensure_local_host("127.0.0.1")
    with socket.socket() as probe:
        if probe.connect_ex((host, args.port)) == 0:
            print(f"端口 {args.port} 已被占用，请改用其他端口。", file=sys.stderr)
            return 2
    ledger.init_db(db_path)
    application = create_app(root, out_dir=out_dir, db_path=db_path)
    print(f"分流验台：http://{host}:{args.port}  （仅本机，离线）")
    application.run(
        host=host,
        port=args.port,
        debug=False,
        use_reloader=False,
        threaded=True,
    )
    return 0


def _cmd_ledger(args: argparse.Namespace) -> int:
    root = _root(args.root)
    db_path = resolve_under(root, Path(args.db))
    if not db_path.exists():
        print("台账尚不存在，请先运行 demo。")
        return 0
    runs = ledger.list_runs(db_path, limit=args.limit, decision=args.decision)
    print(f"{'RUN ID':<44} {'决策':<8} {'效应':>10} {'FINDINGS':>8}  实验")
    for row in runs:
        print(
            f"{row['run_id']:<44} {row['decision']:<8} "
            f"{row['primary_effect']:>+10.4f} {row['finding_count']:>8}  "
            f"{row['experiment_name']}"
        )
    print(f"共 {len(runs)} 条")
    return 0


def _cmd_replay(args: argparse.Namespace) -> int:
    root = _root(args.root)
    out_root = resolve_under(root, Path(args.out))
    db_path = resolve_under(root, Path(args.db))

    if args.all:
        target_ids = [row["run_id"] for row in ledger.list_runs(db_path, limit=200)]
        if not target_ids:
            print("台账中没有 run，无可回放对象。")
            return 0
    else:
        if not args.run_id:
            print("错误：replay 需要 --run-id 或 --all", file=sys.stderr)
            return 2
        target_ids = [args.run_id]

    any_mismatch = False
    non_replayable_single = False
    for run_id in target_ids:
        try:
            run_dir = resolve_under(out_root, Path(run_id), must_exist=True)
        except (PathSecurityError, OSError) as exc:
            print(f"{run_id}: run 目录不可读取：{exc}", file=sys.stderr)
            any_mismatch = True
            continue
        outcome = replay_run(run_dir, db_path if db_path.exists() else None)
        if outcome.status == STATUS_MATCH:
            print(f"{run_id}: 回放一致（仅忽略 run_id/generated_at）")
        elif outcome.status == STATUS_UNREPLAYABLE:
            if not args.all:
                non_replayable_single = True
            if outcome.detail.get("reason") == "legacy_no_snapshot":
                label = "不可重放 · 旧 run 无输入快照"
            else:
                label = f"不可重放（{outcome.detail.get('reason')}）"
            print(f"{run_id}: {label}，保留原报告且不替补当前源文件")
        else:
            # STATUS_MISMATCH 与 STATUS_REPLAY_ERROR 都视为失败。
            any_mismatch = True
            if outcome.status == "replay_error":
                print(f"{run_id}: 回放执行出错：{outcome.detail.get('error')}",
                      file=sys.stderr)
            else:
                print(f"{run_id}: 回放不一致，差异 {outcome.mismatch_count} 处，"
                      f"详见 {replay.REPLAY_FILENAME}")
                for diff in outcome.detail.get("diffs", [])[:10]:
                    print(f"  - {diff['path']}（{diff['kind']}）")
    # 批量模式下存在不可重放的旧 run 属正常历史，只有真正的内容差异才返回非零；
    # 显式指定单个 run 时，不可重放同样以非零退出，便于脚本感知。
    if args.all:
        return 1 if any_mismatch else 0
    return 1 if (any_mismatch or non_replayable_single) else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app",
        description="分流验台：本地 A/B 实验分流健康审计与可复现决策。",
    )
    parser.add_argument("--root", default=".", help="项目根目录（默认当前目录）")
    sub = parser.add_subparsers(dest="command", required=True)

    demo = sub.add_parser("demo", help="用内置合成 fixture 跑完整演示")
    demo.add_argument("--config", default=DEFAULT_CONFIG)
    demo.add_argument("--assignments", default=DEFAULT_ASSIGNMENTS)
    demo.add_argument("--observations", default=DEFAULT_OBSERVATIONS)
    demo.add_argument("--out", default=DEFAULT_OUT)
    demo.add_argument("--db", default=DEFAULT_DB)
    demo.set_defaults(handler=_cmd_demo)

    audit = sub.add_parser("audit", help="审计指定本地输入")
    audit.add_argument("--experiment", default=DEFAULT_CONFIG)
    audit.add_argument("--assignments", default=DEFAULT_ASSIGNMENTS)
    audit.add_argument("--observations", default=DEFAULT_OBSERVATIONS)
    audit.add_argument("--out", default=DEFAULT_OUT)
    audit.add_argument("--db", default=DEFAULT_DB)
    audit.set_defaults(handler=_cmd_audit)

    serve = sub.add_parser("serve", help="启动仅监听 127.0.0.1 的本地 Web")
    serve.add_argument("--port", type=int, default=5000)
    serve.add_argument("--out", default=DEFAULT_OUT)
    serve.add_argument("--db", default=DEFAULT_DB)
    serve.set_defaults(handler=_cmd_serve)

    board = sub.add_parser("ledger", help="查询 SQLite 台账")
    board.add_argument("--db", default=DEFAULT_DB)
    board.add_argument("--limit", type=int, default=20)
    board.add_argument("--decision", choices=("ship", "iterate", "stop", "review"))
    board.set_defaults(handler=_cmd_ledger)

    replay_cmd = sub.add_parser(
        "replay", help="用 run 绑定的输入快照离线重算并核对原报告"
    )
    target = replay_cmd.add_mutually_exclusive_group(required=True)
    target.add_argument("--run-id", help="指定单个 run_id 回放")
    target.add_argument("--all", action="store_true", help="回放台账内全部 run")
    replay_cmd.add_argument("--out", default=DEFAULT_OUT)
    replay_cmd.add_argument("--db", default=DEFAULT_DB)
    replay_cmd.set_defaults(handler=_cmd_replay)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (
        ConfigError, DataValidationError, PathSecurityError, ValueError,
        FileNotFoundError, OSError,
    ) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
