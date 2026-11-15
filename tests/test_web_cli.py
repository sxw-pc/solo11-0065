import json
import shutil
from pathlib import Path
from app import ledger
from app.cli import main
from app.web import create_app
ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"


def _workspace(tmp_path: Path) -> Path:
    target = tmp_path / "project"
    (target / "fixtures").mkdir(parents=True)
    for name in ("experiment.yaml", "assignments.csv", "observations.csv"):
        shutil.copy(FIXTURES / name, target / "fixtures" / name)
    return target


def test_cli_demo_and_web_routes(tmp_path):
    root = _workspace(tmp_path)
    assert main(["--root", str(root), "demo"]) == 0
    db = root / "data" / "ledger.sqlite3"
    runs = ledger.list_runs(db)
    assert len(runs) == 1
    run_id = runs[0]["run_id"]
    app = create_app(root)
    client = app.test_client()

    index = client.get("/")
    assert index.status_code == 200
    assert "新手引导卡片实验".encode() in index.data
    health = client.get("/healthz")
    assert health.status_code == 200
    assert health.json["offline"] is True
    api = client.get("/api/runs")
    assert api.status_code == 200 and len(api.json["runs"]) == 1

    detail = client.get(f"/runs/{run_id}")
    assert detail.status_code == 200
    assert "继续迭代".encode() in detail.data
    assert "default-src 'self'" in detail.headers["Content-Security-Policy"]
    assert detail.headers["X-Frame-Options"] == "DENY"
    assert detail.headers["Cache-Control"] == "no-store"

    report = client.get(f"/runs/{run_id}/artifacts/report.html")
    assert report.status_code == 200
    assert report.mimetype == "text/html"
    assert "分流验台".encode() in report.data
    audit = client.get(f"/runs/{run_id}/artifacts/audit.json")
    assert audit.status_code == 200
    assert audit.json["decision"]["card"] == "iterate"
    assert len(report.data.decode("utf-8").splitlines()) <= 300

    assert client.get("/runs/run-missing/artifacts/report.html").status_code == 404
    assert client.get(f"/runs/{run_id}/artifacts/unknown.txt").status_code == 404
    assert client.get("/?decision=unknown").status_code == 400


def test_api_run_payload_and_filter(tmp_path):
    root = _workspace(tmp_path)
    assert main(["--root", str(root), "demo"]) == 0
    run_id = ledger.list_runs(root / "data" / "ledger.sqlite3")[0]["run_id"]
    client = create_app(root).test_client()
    response = client.get(f"/api/runs/{run_id}")
    assert response.status_code == 200
    payload = response.json
    assert payload["metrics"]["primary"]["key"] == "activation_rate"
    assert payload["run"]["run_id"] == run_id
    assert client.get("/?decision=iterate").status_code == 200
    assert client.get("/?decision=ship").status_code == 200


def test_cli_rejects_path_escape(tmp_path, capsys):
    root = _workspace(tmp_path)
    outside = tmp_path / "outside.yaml"
    outside.write_text("schema_version: 1", encoding="utf-8")
    code = main([
        "--root", str(root), "audit",
        "--experiment", "../outside.yaml",
        "--assignments", "fixtures/assignments.csv",
        "--observations", "fixtures/observations.csv",
    ])
    assert code == 2
    assert "路径逃逸" in capsys.readouterr().err


def test_generated_report_is_self_contained(tmp_path):
    root = _workspace(tmp_path)
    assert main(["--root", str(root), "demo"]) == 0
    report = next((root / "out").glob("*/report.html"))
    html = report.read_text(encoding="utf-8")
    assert "https://" not in html and "http://" not in html
    assert "<script" not in html.lower()
    assert "<style>" in html
