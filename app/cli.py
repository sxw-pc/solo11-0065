"""命令行入口：demo / audit / serve / ledger。"""

from __future__ import annotations

import argparse
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import ledger
from .audit import run_audit
from .config import ConfigError, load_config
from .data import DataValidationError, load_dataset, sha256_bytes, sha256_files
from .reporting import write_artifacts
from .safety import PathSecurityError, ensure_local_host, ensure_output_path, resolve_under
from .web import create_app

DEFAULT_CONFIG = "fixtures/experiment.yaml"
DEFAULT_ASSIGNMENTS = "fixtures/assignments.csv"
DEFAULT_OBSERVATIONS = "fixtures/observations.csv"
DEFAULT_OUT = "out"
DEFAULT_DB = "data/ledger.sqlite3"


def _root(value: str) -> Path:
    return Path(value).resolve()


def _parse_as_of(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"--as-of 不是合法 ISO-8601 时间：{exc}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _run_pipeline(
    root: Path,
    config_path: Path,
    assignments_path: Path,
    observations_path: Path,
    out_dir: Path,
    db_path: Path,
    as_of: datetime | None = None,
) -> tuple[dict[str, Any], Path, Path]:
    config_target = resolve_under(root, config_path, must_exist=True)
    assignments_target = resolve_under(root, assignments_path, must_exist=True)
    observations_target = resolve_under(root, observations_path, must_exist=True)
    output_root = resolve_under(root, out_dir)
    ledger_target = ensure_output_path(root, db_path)
    config = load_config(config_target)
    dataset = load_dataset(config, assignments_target, observations_target)
    now = as_of or datetime.now(timezone.utc)
    input_digest = sha256_files(
        [config_target, assignments_target, observations_target]
    )
    config_digest = sha256_bytes(config_target.read_bytes())
    run_id = f"run-{now:%Y%m%dT%H%M%S%fZ}-{input_digest[:8]}"
    result = run_audit(
        config,
        dataset.assignments,
        dataset.observations,
        run_id=run_id,
        generated_at=now.isoformat(timespec="seconds"),
        input_digest=input_digest,
        config_digest=config_digest,
    )
    run_dir = output_root / run_id
    ensure_output_path(root, run_dir / "audit.json")
    write_artifacts(result, run_dir)
    relative = run_dir.relative_to(root).as_posix()
    ledger.save_run(ledger_target, result, relative)
    return result, run_dir, ledger_target


def _print_result(result: dict[str, Any], run_dir: Path, db_path: Path) -> None:
    decision = result["decision"]
    print(f"决策：{decision['title']} ({decision['card']}) · 规则 {decision['matched_rule']}")
    print(f"run_id：{result['run']['run_id']}")
    print(f"finding：{len(result['findings'])} 条；输入摘要 {result['run']['input_digest'][:16]}…")
    print(f"产物目录：{run_dir}")
    print("  audit.json / decision_card.json / report.md / report.html")
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
        _parse_as_of(args.as_of),
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
        _parse_as_of(args.as_of),
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
    demo.add_argument(
        "--as-of", default=None,
        help="结果成熟度的判定时点（ISO-8601，默认当前 UTC 时间）",
    )
    demo.set_defaults(handler=_cmd_demo)

    audit = sub.add_parser("audit", help="审计指定本地输入")
    audit.add_argument("--experiment", default=DEFAULT_CONFIG)
    audit.add_argument("--assignments", default=DEFAULT_ASSIGNMENTS)
    audit.add_argument("--observations", default=DEFAULT_OBSERVATIONS)
    audit.add_argument("--out", default=DEFAULT_OUT)
    audit.add_argument("--db", default=DEFAULT_DB)
    audit.add_argument(
        "--as-of", default=None,
        help="结果成熟度的判定时点（ISO-8601，默认当前 UTC 时间）",
    )
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
