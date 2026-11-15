"""本地只读 Web 展示。仅监听回环地址，只读取台账、out/ 与内置模板。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from flask import Flask, abort, jsonify, render_template, request, send_file

from . import ledger
from .models import CHECK_LABELS, DECISION_LABELS, SEVERITY_LABELS
from .reporting import ARTIFACTS, fmt_num, fmt_p, fmt_pct
from .replay import load_replay_record
from .safety import PathSecurityError, resolve_under
from .snapshot import (
    SNAPSHOT_DIRNAME,
    SnapshotError,
    load_manifest,
    snapshot_status,
)

ALLOWED_DECISIONS = set(DECISION_LABELS)

REPLAY_BADGES = {
    "match": ("回放一致", "pass"),
    "mismatch": ("回放不一致", "fail"),
    "unreplayable": ("不可重放", "neutral"),
    "replay_error": ("回放出错", "fail"),
}


def _snapshot_view(root: Path, out_path: Path, row: dict[str, Any]) -> dict[str, Any]:
    """汇总 run 目录实况与台账回放结论；旧 run 明确标记为不可重放。"""
    run_dir = out_path / row["run_id"]
    status = snapshot_status(run_dir)
    replay_record = load_replay_record(run_dir)
    ledger_status = row.get("replay_status")
    # 文件系统实况优先：台账曾记录“回放一致”但快照事后丢失时，仍显示不可重放。
    if not status["replayable"]:
        effective = "unreplayable"
    else:
        effective = ledger_status  # None 表示已绑定但尚未回放
    badge = REPLAY_BADGES.get(effective or "", ("未回放", "neutral"))
    manifest = status["manifest"]
    return {
        "snapshot_bound": status["snapshot_bound"],
        "replayable": status["replayable"],
        "reason": status["reason"],
        "replay_status": effective,
        "replay_badge_label": badge[0],
        "replay_badge_class": badge[1],
        "replay_checked_at": row.get("replay_checked_at"),
        "replay_mismatch_count": row.get("replay_mismatch_count"),
        "replay_record": replay_record,
        "manifest": manifest,
        "snapshot_files": manifest["files"] if manifest else [],
    }


def _load_audit(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        abort(404, description=f"审计产物不可读取：{exc}")


def create_app(
    root: Path,
    out_dir: Path | None = None,
    db_path: Path | None = None,
) -> Flask:
    root = Path(root).resolve()
    out_path = resolve_under(root, out_dir or "out")
    ledger_path = resolve_under(root, db_path or "data/ledger.sqlite3")
    app = Flask(__name__)
    app.config.update(
        ROOT=root,
        OUT_DIR=out_path,
        LEDGER_PATH=ledger_path,
        MAX_CONTENT_LENGTH=1 * 1024 * 1024,
    )
    app.json.ensure_ascii = False
    for name, function in (
        ("pct", fmt_pct), ("num", fmt_num), ("pval", fmt_p),
    ):
        app.add_template_filter(function, name)

    @app.after_request
    def secure_headers(response: Any) -> Any:
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; script-src 'none'; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.context_processor
    def inject_labels() -> dict[str, Any]:
        return {
            "DECISION_LABELS": DECISION_LABELS,
            "SEVERITY_LABELS": SEVERITY_LABELS,
            "CHECK_LABELS": CHECK_LABELS,
        }

    @app.get("/")
    def index() -> str:
        decision = request.args.get("decision", "").strip()
        if decision and decision not in ALLOWED_DECISIONS:
            abort(400, description="未知决策筛选条件")
        runs = ledger.list_runs(ledger_path, limit=100, decision=decision or None)
        all_runs = ledger.list_runs(ledger_path, limit=200)
        counts = {key: 0 for key in ALLOWED_DECISIONS}
        for row in all_runs:
            if row["decision"] in counts:
                counts[row["decision"]] += 1
        snapshot_views = {
            row["run_id"]: _snapshot_view(root, out_path, row) for row in all_runs
        }
        return render_template(
            "index.html",
            runs=runs,
            total=len(all_runs),
            counts=counts,
            selected=decision,
            latest=all_runs[0] if all_runs else None,
            db_exists=ledger_path.exists(),
            snapshot_views=snapshot_views,
        )

    def _run_row(run_id: str) -> dict[str, Any]:
        row = ledger.get_run(ledger_path, run_id)
        if row is None:
            abort(404, description="台账中没有该 run")
        return row

    @app.get("/runs/<run_id>")
    def run_detail(run_id: str) -> str:
        row = _run_row(run_id)
        try:
            artifact_path = resolve_under(
                out_path, Path(run_id) / "audit.json", must_exist=True
            )
        except (PathSecurityError, OSError) as exc:
            abort(404, description=f"审计产物不可读取：{exc}")
        return render_template(
            "run.html",
            row=row,
            result=_load_audit(artifact_path),
            snapshot_view=_snapshot_view(root, out_path, row),
        )

    @app.get("/runs/<run_id>/artifacts/<name>")
    def artifact(run_id: str, name: str) -> Any:
        _run_row(run_id)
        if name not in ARTIFACTS:
            abort(404, description="不允许的产物名称")
        try:
            path = resolve_under(out_path, Path(run_id) / name, must_exist=True)
        except (PathSecurityError, OSError) as exc:
            abort(404, description=str(exc))
        mimetypes = {
            "audit.json": "application/json",
            "decision_card.json": "application/json",
            "report.md": "text/markdown",
            "report.html": "text/html",
        }
        return send_file(path, mimetype=mimetypes[name], as_attachment=False,
                         download_name=name)

    @app.get("/runs/<run_id>/snapshot/manifest")
    def snapshot_manifest(run_id: str) -> Any:
        _run_row(run_id)
        try:
            run_dir = resolve_under(out_path, Path(run_id), must_exist=True)
            return jsonify(load_manifest(run_dir))
        except (PathSecurityError, OSError, ValueError, SnapshotError):
            abort(404, description="该 run 没有可读的输入快照清单")

    @app.get("/runs/<run_id>/snapshot/files/<name>")
    def snapshot_file(run_id: str, name: str) -> Any:
        _run_row(run_id)
        # 只允许下载清单中登记过的快照文件，避免任意路径读取。
        try:
            run_dir = resolve_under(out_path, Path(run_id), must_exist=True)
            allowed = {entry["name"] for entry in load_manifest(run_dir)["files"]}
        except (PathSecurityError, OSError, ValueError, SnapshotError):
            abort(404, description="该 run 没有可读的输入快照清单")
        if name not in allowed:
            abort(404, description="快照清单中没有该文件")
        try:
            path = resolve_under(
                out_path, Path(run_id) / SNAPSHOT_DIRNAME / name, must_exist=True
            )
        except (PathSecurityError, OSError) as exc:
            abort(404, description=str(exc))
        return send_file(path, mimetype="text/plain", as_attachment=True,
                         download_name=name)

    @app.get("/runs/<run_id>/replay")
    def replay_status(run_id: str) -> Any:
        row = _run_row(run_id)
        run_dir = resolve_under(out_path, Path(run_id))
        return jsonify(_snapshot_view(root, out_path, row) | {
            "replay_file": load_replay_record(run_dir),
        })

    @app.get("/api/runs")
    def api_runs() -> Any:
        runs = ledger.list_runs(ledger_path, limit=100)
        return jsonify({
            "runs": [
                {
                    "run_id": row["run_id"],
                    "generated_at": row["generated_at"],
                    "experiment_id": row["experiment_id"],
                    "experiment_name": row["experiment_name"],
                    "decision": row["decision"],
                    "headline": row["headline"],
                    "finding_count": row["finding_count"],
                    "input_digest": row["input_digest"],
                    "snapshot_bound": bool(row["snapshot_bound"]),
                    "replay_status": row["replay_status"],
                }
                for row in runs
            ]
        })

    @app.get("/api/runs/<run_id>")
    def api_run(run_id: str) -> Any:
        _run_row(run_id)
        try:
            artifact_path = resolve_under(
                out_path, Path(run_id) / "audit.json", must_exist=True
            )
        except (PathSecurityError, OSError) as exc:
            abort(404, description=str(exc))
        return jsonify(_load_audit(artifact_path))

    @app.get("/healthz")
    def healthz() -> Any:
        return jsonify({"status": "ok", "offline": True, "runs": ledger.count_runs(ledger_path)})

    @app.errorhandler(400)
    @app.errorhandler(404)
    def not_found(error: Any) -> tuple[str, int]:
        code = getattr(error, "code", 404)
        return render_template("error.html", code=code,
                               message=getattr(error, "description", "请求无法处理")), code

    return app
