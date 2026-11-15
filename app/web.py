"""本地只读 Web 展示。仅监听回环地址，只读取台账、out/ 与内置模板。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from flask import Flask, abort, jsonify, render_template, request, send_file

from . import ledger
from .lifecycle import LIFECYCLE_LABELS
from .models import CHECK_LABELS, DECISION_LABELS, SEVERITY_LABELS
from .reporting import ARTIFACTS, fmt_num, fmt_p, fmt_pct
from .safety import PathSecurityError, resolve_under

ALLOWED_DECISIONS = set(DECISION_LABELS)


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
            "LIFECYCLE_LABELS": LIFECYCLE_LABELS,
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
        return render_template(
            "index.html",
            runs=runs,
            total=len(all_runs),
            counts=counts,
            selected=decision,
            latest=all_runs[0] if all_runs else None,
            db_exists=ledger_path.exists(),
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
        result = _load_audit(artifact_path)
        # 台账中的生命周期关系是权威来源（旧产物可能缺少该段）。
        result["lifecycle"] = row.get("lifecycle")
        return render_template("run.html", row=row, result=result)

    @app.get("/runs/<run_id>/findings/<finding_key>")
    def finding_history(run_id: str, finding_key: str) -> str:
        row = _run_row(run_id)
        history = ledger.get_finding_history(ledger_path, run_id, finding_key)
        if not history:
            abort(404, description="台账中没有该 finding 的跨运行记录")
        return render_template(
            "finding_history.html", row=row, finding_key=finding_key, history=history
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
                    "baseline_run_id": row["baseline_run_id"],
                    "comparison_status": row["comparison_status"],
                }
                for row in runs
            ]
        })

    @app.get("/api/runs/<run_id>")
    def api_run(run_id: str) -> Any:
        row = _run_row(run_id)
        try:
            artifact_path = resolve_under(
                out_path, Path(run_id) / "audit.json", must_exist=True
            )
        except (PathSecurityError, OSError) as exc:
            abort(404, description=str(exc))
        payload = _load_audit(artifact_path)
        payload["lifecycle"] = row.get("lifecycle")
        return jsonify(payload)

    @app.get("/api/runs/<run_id>/findings/<finding_key>")
    def api_finding_history(run_id: str, finding_key: str) -> Any:
        _run_row(run_id)
        history = ledger.get_finding_history(ledger_path, run_id, finding_key)
        if not history:
            abort(404, description="没有该 finding 的跨运行记录")
        return jsonify({"finding_key": finding_key, "history": history})

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
