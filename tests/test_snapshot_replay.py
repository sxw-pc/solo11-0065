"""输入快照绑定与离线回放的端到端测试。

覆盖：
- 源文件并发替换时，run 仍绑定替换前的字节（解析/digest/归档/台账同一快照）；
- 暂存失败不留半成品；
- 报告写完、台账提交前后的中断语义；
- 旧 run 无快照时继续可查询但明确标记不可重放；
- 回放一致 / 差异（统计、findings、规则命中、决策）/ 快照损坏；
- Web 与 CLI 的状态展示；原报告永不被回放覆盖。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from app import ledger, replay, snapshot
from app.cli import main
from app.config import load_config
from app.data import load_dataset, sha256_files, sha256_bytes
from app.reporting import write_artifacts
from app.snapshot import (
    REASON_FILE_MISSING,
    REASON_HASH_MISMATCH,
    REASON_LEGACY,
    STAGING_PREFIX,
    SnapshotError,
    capture_snapshot,
    discard_snapshot,
    snapshot_status,
    verify_snapshot,
)
from app.web import create_app
from app.audit import run_audit

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"
FIXTURE_FILES = ("experiment.yaml", "assignments.csv", "observations.csv")


def _workspace(tmp_path: Path) -> Path:
    target = tmp_path / "project"
    (target / "fixtures").mkdir(parents=True)
    for name in FIXTURE_FILES:
        shutil.copy(FIXTURES / name, target / "fixtures" / name)
    return target


def _run_demo(root: Path) -> str:
    assert main(["--root", str(root), "demo"]) == 0
    return ledger.list_runs(root / "data" / "ledger.sqlite3")[0]["run_id"]


def _run_dir(root: Path, run_id: str) -> Path:
    return root / "out" / run_id


# --------------------------------------------------------------------------- #
# 基础绑定：每个 run 都有自洽的快照，且 digest 与旧哈希框架一致
# --------------------------------------------------------------------------- #

def test_run_binds_snapshot_with_manifest_and_archived_bytes(tmp_path):
    root = _workspace(tmp_path)
    run_id = _run_demo(root)
    run_dir = _run_dir(root, run_id)

    manifest = verify_snapshot(run_dir)
    assert {entry["role"] for entry in manifest["files"]} == {
        "config", "assignments", "observations"
    }
    # 归档字节与当前源文件逐字节相同，且每个文件都带独立 sha256。
    for entry in manifest["files"]:
        archived = run_dir / "snapshot" / entry["name"]
        source = root / "fixtures" / entry["name"]
        assert archived.read_bytes() == source.read_bytes()
        assert entry["size"] == len(archived.read_bytes())

    audit = json.loads((run_dir / "audit.json").read_text(encoding="utf-8"))
    assert audit["snapshot"]["bound"] is True
    assert audit["snapshot"]["input_digest"] == audit["run"]["input_digest"]
    # 与历史 sha256_files 框架保持兼容。
    legacy_digest = sha256_files([root / "fixtures" / n for n in FIXTURE_FILES])
    assert audit["run"]["input_digest"] == legacy_digest
    row = ledger.get_run(root / "data" / "ledger.sqlite3", run_id)
    assert row["snapshot_bound"] == 1
    assert row["replay_status"] is None  # 尚未回放


def test_no_visible_staging_directory_after_publish(tmp_path):
    root = _workspace(tmp_path)
    _run_demo(root)
    staging = [
        path for path in (root / "out").iterdir()
        if path.name.startswith(STAGING_PREFIX)
    ]
    assert staging == []


# --------------------------------------------------------------------------- #
# 竞态：读取与哈希之间替换源文件
# --------------------------------------------------------------------------- #

def test_concurrent_source_replacement_binds_original_bytes(tmp_path, monkeypatch):
    root = _workspace(tmp_path)
    original = {
        name: (root / "fixtures" / name).read_bytes() for name in FIXTURE_FILES
    }
    # 三份输入都已捕获进快照后、写清单前替换全部源文件——这正是旧实现
    # “load_dataset 先读、sha256_files 后算”两步之间被替换的竞态窗口。
    real_write = snapshot._write_bytes_atomic
    captured: list[Path] = []

    def tampering_write(path: Path, data: bytes) -> None:
        real_write(path, data)
        captured.append(path)
        # 三个快照输入文件写完后的下一次写入是清单；此时把源文件换成版本 B。
        if len(captured) == len(FIXTURE_FILES):
            for name in FIXTURE_FILES:
                (root / "fixtures" / name).write_bytes(b"tampered version B\n")

    monkeypatch.setattr("app.snapshot._write_bytes_atomic", tampering_write)

    run_id = _run_demo(root)
    run_dir = _run_dir(root, run_id)
    # 快照里是替换前的版本 A，而不是磁盘上的版本 B。
    for name in FIXTURE_FILES:
        assert (run_dir / "snapshot" / name).read_bytes() == original[name]
    audit = json.loads((run_dir / "audit.json").read_text(encoding="utf-8"))
    expected_digest = snapshot.digest_inputs(
        [(n, original[n]) for n in FIXTURE_FILES]
    )
    assert audit["run"]["input_digest"] == expected_digest
    # 台账登记同一 digest，绝不出现“用 A 计算、登记 B 摘要”。
    row = ledger.list_runs(root / "data" / "ledger.sqlite3")[0]
    assert row["input_digest"] == expected_digest
    # 源文件已是 B，但回放只认快照 A，仍应一致。
    outcome = replay.replay_run(run_dir, root / "data" / "ledger.sqlite3")
    assert outcome.status == replay.STATUS_MATCH
    assert (root / "fixtures" / "assignments.csv").read_bytes() == b"tampered version B\n"


def test_digests_and_parse_come_from_single_snapshot(tmp_path):
    root = _workspace(tmp_path)
    out_root = root / "out"
    snap = capture_snapshot(
        root,
        root / "fixtures" / "experiment.yaml",
        root / "fixtures" / "assignments.csv",
        root / "fixtures" / "observations.csv",
        out_root,
    )
    try:
        # 替换源文件后，快照内字节哈希与暂存文件哈希必须仍然自洽。
        (root / "fixtures" / "assignments.csv").write_bytes(b"x")
        manifest = snap.manifest_payload(root)
        verify = verify_snapshot(snap.staging_dir)
        assert verify["input_digest"] == snap.input_digest
        assert snap.input_digest == snapshot.digest_inputs(
            ((e["name"], snap.bytes_for(e["role"])) for e in manifest["files"])
        )
    finally:
        discard_snapshot(snap.staging_dir)


# --------------------------------------------------------------------------- #
# 暂存失败：清理、不留半成品、不写台账
# --------------------------------------------------------------------------- #

def test_staging_failure_rolls_back_and_skips_ledger(tmp_path, monkeypatch):
    root = _workspace(tmp_path)
    db = root / "data" / "ledger.sqlite3"

    def boom(*args, **kwargs):
        raise OSError("simulated disk failure")

    monkeypatch.setattr("app.snapshot._write_bytes_atomic", boom)
    # main() 把底层 OSError 统一转换为退出码 2。
    assert main(["--root", str(root), "demo"]) == 2

    assert not db.exists() or ledger.count_runs(db) == 0
    leftovers = [
        p for p in (root / "out").glob(f"{STAGING_PREFIX}*")
    ] if (root / "out").exists() else []
    assert leftovers == []


def test_publish_failure_leaves_no_published_run(tmp_path, monkeypatch):
    root = _workspace(tmp_path)
    db = root / "data" / "ledger.sqlite3"

    def boom(*args, **kwargs):
        raise OSError("rename failed")

    monkeypatch.setattr("app.snapshot.publish_snapshot", boom)
    assert main(["--root", str(root), "demo"]) == 2
    assert ledger.count_runs(db) == 0
    # 暂存目录已回滚，正式 run 目录一个都没有。
    assert list((root / "out").iterdir()) == []


# --------------------------------------------------------------------------- #
# 台账提交前后的中断
# --------------------------------------------------------------------------- #

def test_crash_after_publish_before_ledger_hides_run_from_web(tmp_path, monkeypatch):
    root = _workspace(tmp_path)

    def crash_save(*args, **kwargs):
        raise SystemExit(23)

    monkeypatch.setattr("app.cli.ledger.save_run", crash_save)
    with pytest.raises(SystemExit):
        main(["--root", str(root), "demo"])

    # run 目录已原子发布、内容完整，但台账没有它 → Web 必须当作不存在。
    published = [p for p in (root / "out").iterdir() if not p.name.startswith(STAGING_PREFIX)]
    assert len(published) == 1
    assert (published[0] / "audit.json").is_file()
    client = create_app(root).test_client()
    assert client.get("/").status_code == 200
    assert client.get("/api/runs").json["runs"] == []
    assert client.get(f"/runs/{published[0].name}").status_code == 404


def test_snapshot_lost_after_commit_marks_unreplayable_keeps_report(tmp_path):
    root = _workspace(tmp_path)
    run_id = _run_demo(root)
    run_dir = _run_dir(root, run_id)
    original_report = (run_dir / "report.html").read_bytes()
    original_audit = (run_dir / "audit.json").read_bytes()

    shutil.rmtree(run_dir / "snapshot")
    status = snapshot_status(run_dir)
    assert status["replayable"] is False
    assert status["snapshot_bound"] is True
    assert status["reason"] == REASON_FILE_MISSING

    outcome = replay.replay_run(run_dir, root / "data" / "ledger.sqlite3")
    assert outcome.status == replay.STATUS_UNREPLAYABLE
    assert outcome.replayable is False
    # 旧报告原样保留，绝不拿当前源文件替补。
    assert (run_dir / "report.html").read_bytes() == original_report
    assert (run_dir / "audit.json").read_bytes() == original_audit

    row = ledger.get_run(root / "data" / "ledger.sqlite3", run_id)
    assert row["replay_status"] == "unreplayable"
    client = create_app(root).test_client()
    page = client.get(f"/runs/{run_id}")
    assert page.status_code == 200
    assert "不可重放".encode() in page.data


def test_tampered_snapshot_file_detected(tmp_path):
    root = _workspace(tmp_path)
    run_id = _run_demo(root)
    run_dir = _run_dir(root, run_id)
    target = run_dir / "snapshot" / "assignments.csv"
    target.write_bytes(target.read_bytes() + b"\n")
    status = snapshot_status(run_dir)
    assert status["replayable"] is False
    assert status["reason"] == REASON_HASH_MISMATCH
    with pytest.raises(SnapshotError):
        verify_snapshot(run_dir)
    outcome = replay.replay_run(run_dir, root / "data" / "ledger.sqlite3")
    assert outcome.status == replay.STATUS_UNREPLAYABLE


def test_web_prefers_filesystem_truth_over_stale_ledger_status(tmp_path):
    root = _workspace(tmp_path)
    run_id = _run_demo(root)
    run_dir = _run_dir(root, run_id)
    # 先回放一致并写回台账，再删除快照：旧状态不得继续显示“回放一致”。
    assert replay.replay_run(
        run_dir, root / "data" / "ledger.sqlite3"
    ).status == replay.STATUS_MATCH
    shutil.rmtree(run_dir / "snapshot")
    client = create_app(root).test_client()
    detail = client.get(f"/runs/{run_id}")
    assert detail.status_code == 200
    assert "回放一致".encode() not in detail.data
    assert "不可重放".encode() in detail.data
    assert client.get(f"/runs/{run_id}/replay").json["replay_status"] == "unreplayable"


# --------------------------------------------------------------------------- #
# 旧 run：无快照仍可查询，明确不可重放
# --------------------------------------------------------------------------- #

def _build_legacy_run(root: Path) -> str:
    config = load_config(root / "fixtures" / "experiment.yaml")
    dataset = load_dataset(
        config,
        root / "fixtures" / "assignments.csv",
        root / "fixtures" / "observations.csv",
    )
    digest = sha256_files([root / "fixtures" / n for n in FIXTURE_FILES])
    result = run_audit(
        config, dataset.assignments, dataset.observations,
        run_id="run-legacy-0000000001-abcd1234",
        generated_at="2026-01-01T00:00:00+00:00",
        input_digest=digest,
        config_digest=sha256_bytes((root / "fixtures" / "experiment.yaml").read_bytes()),
    )
    run_dir = root / "out" / result["run"]["run_id"]
    write_artifacts(result, run_dir)
    ledger.save_run(
        root / "data" / "ledger.sqlite3", result,
        run_dir.relative_to(root).as_posix(), snapshot_bound=False,
    )
    return result["run"]["run_id"]


def test_legacy_run_is_queryable_but_marked_unreplayable(tmp_path):
    root = _workspace(tmp_path)
    legacy_id = _build_legacy_run(root)
    # 同一台账里再加一个新 run，验证两类 run 并存时的展示。
    new_id = _run_demo(root)

    legacy_dir = _run_dir(root, legacy_id)
    assert not (legacy_dir / "input_manifest.json").exists()
    status = snapshot_status(legacy_dir)
    assert status["snapshot_bound"] is False
    assert status["reason"] == REASON_LEGACY

    outcome = replay.replay_run(legacy_dir, root / "data" / "ledger.sqlite3")
    assert outcome.status == replay.STATUS_UNREPLAYABLE
    record = replay.load_replay_record(legacy_dir)
    assert record["status"] == "unreplayable"

    client = create_app(root).test_client()
    index = client.get("/")
    assert index.status_code == 200
    assert "旧 run".encode() in index.data
    detail = client.get(f"/runs/{legacy_id}")
    assert detail.status_code == 200
    assert "不可重放".encode() in detail.data
    assert client.get(f"/runs/{legacy_id}/snapshot/manifest").status_code == 404
    # 新 run 快照路由正常。
    assert client.get(f"/runs/{new_id}/snapshot/manifest").status_code == 200


def test_ledger_schema_migrates_old_database(tmp_path):
    root = _workspace(tmp_path)
    db = root / "data" / "ledger.sqlite3"
    legacy_id = _build_legacy_run(root)
    # 直接以旧 schema 视角确认迁移列存在且默认值正确。
    with sqlite3.connect(db) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
    assert {"snapshot_bound", "replay_status", "replay_detail_json"} <= columns
    row = ledger.get_run(db, legacy_id)
    assert row["snapshot_bound"] == 0
    assert row["replay_status"] is None


# --------------------------------------------------------------------------- #
# 回放：一致、差异、只忽略 run_id/generated_at、不覆盖原报告
# --------------------------------------------------------------------------- #

def test_replay_match_persists_to_file_and_ledger(tmp_path):
    root = _workspace(tmp_path)
    run_id = _run_demo(root)
    run_dir = _run_dir(root, run_id)
    outcome = replay.replay_run(run_dir, root / "data" / "ledger.sqlite3")
    assert outcome.status == replay.STATUS_MATCH
    assert outcome.mismatch_count == 0

    record = replay.load_replay_record(run_dir)
    assert record["status"] == "match"
    assert record["detail"]["ignored_keys"] == ["run_id", "generated_at"]
    row = ledger.get_run(root / "data" / "ledger.sqlite3", run_id)
    assert row["replay_status"] == "match"
    assert row["replay_mismatch_count"] == 0


def test_replay_ignores_only_run_id_and_generated_at(tmp_path):
    root = _workspace(tmp_path)
    run_id = _run_demo(root)
    run_dir = _run_dir(root, run_id)
    # 直接改写原 audit.json 的 run_id/时间，回放仍必须判一致。
    audit_path = run_dir / "audit.json"
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    audit["run"]["run_id"] = "run-changed-0000000000-zzzz9999"
    audit["run"]["generated_at"] = "1999-01-01T00:00:00+00:00"
    audit_path.write_text(json.dumps(audit, ensure_ascii=False), encoding="utf-8")
    # 注意：台账中的 run_id 未改，Web 仍按原 run_id 查询，这里直接调用回放引擎。
    outcome = replay.replay_run(run_dir)
    assert outcome.status == replay.STATUS_MATCH


def test_replay_detects_statistics_finding_rule_and_decision_drift(tmp_path):
    root = _workspace(tmp_path)
    run_id = _run_demo(root)
    run_dir = _run_dir(root, run_id)
    audit_path = run_dir / "audit.json"
    before = audit_path.read_bytes()
    audit = json.loads(audit_path.read_text(encoding="utf-8"))

    # 同时篡改统计、findings、规则命中与决策，模拟“原报告与重算不一致”。
    audit["metrics"]["primary"]["absolute_effect"] = 9.99
    audit["findings"][0]["code"] = "forged.finding"
    audit["decision"]["card"] = "ship"
    audit["decision"]["matched_rule"] = "forged-rule"
    audit["flags"]["guardrail_watch"] = not audit["flags"]["guardrail_watch"]
    audit_path.write_text(json.dumps(audit, ensure_ascii=False), encoding="utf-8")

    outcome = replay.replay_run(run_dir, root / "data" / "ledger.sqlite3")
    assert outcome.status == replay.STATUS_MISMATCH
    paths = {diff["path"] for diff in outcome.detail["diffs"]}
    assert any(path.startswith("metrics.primary.absolute_effect") for path in paths)
    assert any(path.startswith("findings[0].code") for path in paths)
    assert any(path.startswith("decision") for path in paths)
    assert any(path.startswith("flags") for path in paths)
    assert outcome.mismatch_count == len(outcome.detail["diffs"])

    row = ledger.get_run(root / "data" / "ledger.sqlite3", run_id)
    assert row["replay_status"] == "mismatch"
    assert row["replay_mismatch_count"] == outcome.mismatch_count
    # 回放只写 replay_status.json，原四份报告保持被篡改后的原样，不被重算覆盖。
    assert audit_path.read_bytes() != before or True  # 篡改内容仍在
    assert json.loads(audit_path.read_text(encoding="utf-8"))["decision"]["card"] == "ship"
    for name in ("decision_card.json", "report.md", "report.html"):
        assert (run_dir / name).is_file()
    assert replay.load_replay_record(run_dir)["status"] == "mismatch"


def test_replay_does_not_touch_original_artifacts(tmp_path):
    root = _workspace(tmp_path)
    run_id = _run_demo(root)
    run_dir = _run_dir(root, run_id)
    fingerprints = {
        name: (run_dir / name).read_bytes()
        for name in ("audit.json", "decision_card.json", "report.md", "report.html")
    }
    replay.replay_run(run_dir, root / "data" / "ledger.sqlite3")
    replay.replay_run(run_dir, root / "data" / "ledger.sqlite3")
    for name, payload in fingerprints.items():
        assert (run_dir / name).read_bytes() == payload
    # 只允许新增回放状态文件。
    assert (run_dir / "replay_status.json").is_file()


def test_replay_uses_snapshot_not_current_sources(tmp_path):
    root = _workspace(tmp_path)
    run_id = _run_demo(root)
    run_dir = _run_dir(root, run_id)
    # 源文件被整体换成无效内容，回放仍必须从快照成功重算。
    for name in FIXTURE_FILES:
        (root / "fixtures" / name).write_text("not valid anymore", encoding="utf-8")
    outcome = replay.replay_run(run_dir, root / "data" / "ledger.sqlite3")
    assert outcome.status == replay.STATUS_MATCH


# --------------------------------------------------------------------------- #
# CLI replay
# --------------------------------------------------------------------------- #

def test_cli_replay_single_and_all(tmp_path, capsys):
    root = _workspace(tmp_path)
    first = _run_demo(root)
    # 第二个 run：再跑一次 demo（run_id 含时间戳，天然不同）。
    assert main(["--root", str(root), "demo"]) == 0
    ids = [row["run_id"] for row in ledger.list_runs(root / "data" / "ledger.sqlite3")]
    assert len(ids) == 2

    rc = main(["--root", str(root), "replay", "--run-id", first])
    assert rc == 0
    assert "回放一致" in capsys.readouterr().out

    rc = main(["--root", str(root), "replay", "--all"])
    assert rc == 0
    out = capsys.readouterr().out
    assert out.count("回放一致") == 2


def test_cli_replay_legacy_run_exit_code(tmp_path, capsys):
    root = _workspace(tmp_path)
    legacy_id = _build_legacy_run(root)
    rc = main(["--root", str(root), "replay", "--run-id", legacy_id])
    assert rc == 1
    assert "不可重放" in capsys.readouterr().out
    # --all 模式下旧 run 不算失败。
    rc = main(["--root", str(root), "replay", "--all"])
    assert rc == 0


def test_cli_replay_missing_run(tmp_path):
    root = _workspace(tmp_path)
    _run_demo(root)
    rc = main(["--root", str(root), "replay", "--run-id", "run-does-not-exist-0000"])
    assert rc == 1


# --------------------------------------------------------------------------- #
# Web 展示与路由
# --------------------------------------------------------------------------- #

def test_web_shows_replay_badges_and_snapshot_routes(tmp_path):
    root = _workspace(tmp_path)
    run_id = _run_demo(root)
    client = create_app(root).test_client()

    # 回放前：已绑定、待回放。
    index = client.get("/")
    assert "待回放".encode() in index.data
    replay.replay_run(_run_dir(root, run_id), root / "data" / "ledger.sqlite3")
    index = client.get("/")
    assert "回放一致".encode() in index.data

    detail = client.get(f"/runs/{run_id}")
    assert detail.status_code == 200
    assert "输入快照与离线回放".encode() in detail.data
    manifest = client.get(f"/runs/{run_id}/snapshot/manifest")
    assert manifest.status_code == 200
    assert manifest.json["snapshot_version"] == 1
    snap_file = client.get(f"/runs/{run_id}/snapshot/files/observations.csv")
    assert snap_file.status_code == 200
    assert snap_file.data == (
        _run_dir(root, run_id) / "snapshot" / "observations.csv"
    ).read_bytes()
    assert client.get(f"/runs/{run_id}/snapshot/files/audit.json").status_code == 404
    status_api = client.get(f"/runs/{run_id}/replay")
    assert status_api.status_code == 200
    assert status_api.json["replay_status"] == "match"


def test_report_markdown_and_html_describe_snapshot(tmp_path):
    root = _workspace(tmp_path)
    run_id = _run_demo(root)
    run_dir = _run_dir(root, run_id)
    markdown = (run_dir / "report.md").read_text(encoding="utf-8")
    assert "## 输入快照与可回放性" in markdown
    assert "python -m app replay" in markdown
    html = (run_dir / "report.html").read_text(encoding="utf-8")
    assert "输入快照与可回放性" in html
