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


def _seed_two_runs(root: Path):
    from app import ledger
    from app.audit import run_audit
    from app.config import load_config
    from app.data import load_dataset, sha256_bytes, sha256_files
    from app.reporting import write_artifacts

    fixtures = root / "fixtures"
    config = load_config(fixtures / "experiment.yaml")
    dataset = load_dataset(
        config, fixtures / "assignments.csv", fixtures / "observations.csv"
    )
    digest = sha256_files([
        fixtures / "experiment.yaml",
        fixtures / "assignments.csv",
        fixtures / "observations.csv",
    ])
    config_digest = sha256_bytes((fixtures / "experiment.yaml").read_bytes())
    db = root / "data" / "ledger.sqlite3"
    run_ids = []
    for index, stamp in enumerate(
        ("2026-03-01T00:00:00+00:00", "2026-03-02T00:00:00+00:00"), start=1
    ):
        run_id = f"run-webcycle-00000{index}"
        result = run_audit(
            config, dataset.assignments, dataset.observations,
            run_id=run_id, generated_at=stamp,
            input_digest=digest, config_digest=config_digest,
        )
        run_dir = root / "out" / run_id
        lifecycle = ledger.save_run(db, result, f"out/{run_id}")
        result["lifecycle"] = lifecycle
        write_artifacts(result, run_dir)
        run_ids.append(run_id)
    return run_ids


def test_web_lifecycle_detail_and_finding_history(tmp_path):
    root = _workspace(tmp_path)
    first_id, second_id = _seed_two_runs(root)
    client = create_app(root).test_client()

    detail = client.get(f"/runs/{second_id}")
    assert detail.status_code == 200
    body = detail.data.decode("utf-8")
    assert "持续存在" in body
    assert "比较基准 run" in body
    assert first_id in body
    assert "完整轨迹" in body

    # 首个 run 的页面展示无基准而非报错
    first_page = client.get(f"/runs/{first_id}")
    assert first_page.status_code == 200
    assert "无比较基准" in first_page.data.decode("utf-8")
    assert "后继 run" in first_page.data.decode("utf-8")

    # finding 轨迹 API 与页面
    api_run = client.get(f"/api/runs/{second_id}")
    assert api_run.status_code == 200
    entries = api_run.json["lifecycle"]["entries"]
    assert entries and all(e["finding_key"] for e in entries)
    finding_key = entries[0]["finding_key"]
    history = client.get(f"/runs/{second_id}/findings/{finding_key}")
    assert history.status_code == 200
    assert first_id in history.data.decode("utf-8")
    api_history = client.get(f"/api/runs/{second_id}/findings/{finding_key}")
    assert api_history.status_code == 200
    assert len(api_history.json["history"]) == 2
    assert client.get(f"/runs/{second_id}/findings/missing-key").status_code == 404

    # 列表页包含比较基准列
    index = client.get("/")
    assert index.status_code == 200
    assert "比较基准" in index.data.decode("utf-8")


def test_web_config_change_shows_incomparable(tmp_path):
    from app import ledger
    from app.reporting import write_artifacts

    root = _workspace(tmp_path)
    first_id, second_id = _seed_two_runs(root)
    db = root / "data" / "ledger.sqlite3"
    # 第三个 run 使用不同 config_digest：应标记不可比较而非把 run-2 当基准
    third = json.loads(
        (root / "out" / second_id / "audit.json").read_text(encoding="utf-8")
    )
    third["run"]["run_id"] = "run-webcycle-000003"
    third["run"]["generated_at"] = "2026-03-03T00:00:00+00:00"
    third["run"]["config_digest"] = "rotated-config-digest"
    lifecycle = ledger.save_run(db, third, "out/run-webcycle-000003")
    assert lifecycle["comparison_status"] == "incomparable"
    third["lifecycle"] = lifecycle
    write_artifacts(third, root / "out" / "run-webcycle-000003")

    client = create_app(root).test_client()
    page = client.get("/runs/run-webcycle-000003")
    assert page.status_code == 200
    body = page.data.decode("utf-8")
    assert "无同配置基准" in body
    assert "新出现" in body
    index = client.get("/")
    assert "配置变化·不可比" in index.data.decode("utf-8")
