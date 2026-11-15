from __future__ import annotations

import csv
from dataclasses import replace
from pathlib import Path

import pytest

from app.audit import run_audit
from app.config import ConfigError, load_config
from app.data import (
    DataValidationError,
    load_assignments,
    load_baseline,
    load_dataset,
    load_observations,
    sha256_bytes,
    sha256_files,
)
from app.metrics import compute_all_metrics
from app.models import BaselineObservation, BaselineSpec
from app.stats import cuped_estimate, cuped_theta, welch_t_test

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"
CUPED_CONFIG = FIXTURES / "experiment_cuped.yaml"
ASSIGNMENTS = FIXTURES / "assignments.csv"
OBSERVATIONS = FIXTURES / "observations.csv"
BASELINE = FIXTURES / "baseline.csv"


def _config():
    return load_config(CUPED_CONFIG)


def _dataset(config=None):
    config = config or _config()
    return load_dataset(config, ASSIGNMENTS, OBSERVATIONS, BASELINE)


def _audit(config, dataset, baseline=None):
    return run_audit(
        config,
        dataset.assignments,
        dataset.observations,
        run_id="run-cuped-0000001",
        generated_at="2026-03-16T00:00:00+00:00",
        input_digest=sha256_files([CUPED_CONFIG, ASSIGNMENTS, OBSERVATIONS, BASELINE]),
        config_digest=sha256_bytes(CUPED_CONFIG.read_bytes()),
        baseline=dataset.baseline if baseline is None else baseline,
    )


# ---------------------------------------------------------------- config


def test_baseline_config_contract():
    config = _config()
    baseline = config.primary.baseline
    assert baseline is not None
    assert baseline.file == "baseline.csv"
    assert baseline.column == "baseline_session_depth"
    assert baseline.min_coverage == 0.9


def test_baseline_only_allowed_on_mean_primary(tmp_path):
    text = CUPED_CONFIG.read_text(encoding="utf-8")
    path = tmp_path / "experiment.yaml"
    path.write_text(text.replace("kind: mean", "kind: proportion", 1), encoding="utf-8")
    with pytest.raises(ConfigError, match="mean"):
        load_config(path)


def test_baseline_rejected_on_secondary(tmp_path):
    text = CUPED_CONFIG.read_text(encoding="utf-8")
    marker = "      label: 激活率\n      kind: proportion\n      orientation: higher_is_better\n"
    assert marker in text
    replacement = marker + "      baseline:\n        file: baseline.csv\n        column: x\n"
    path = tmp_path / "experiment.yaml"
    path.write_text(text.replace(marker, replacement, 1), encoding="utf-8")
    with pytest.raises(ConfigError, match="metrics.primary"):
        load_config(path)


def test_baseline_rejects_path_escape(tmp_path):
    text = CUPED_CONFIG.read_text(encoding="utf-8")
    path = tmp_path / "experiment.yaml"
    path.write_text(
        text.replace("file: baseline.csv", "file: ../baseline.csv", 1),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="相对路径"):
        load_config(path)


def test_baseline_rejects_bad_min_coverage(tmp_path):
    text = CUPED_CONFIG.read_text(encoding="utf-8")
    path = tmp_path / "experiment.yaml"
    path.write_text(
        text.replace("min_coverage: 0.9", "min_coverage: 0", 1), encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="min_coverage"):
        load_config(path)


def test_old_config_has_no_baseline(config):
    assert config.primary.baseline is None


# ---------------------------------------------------------------- data


def _write_rows(path: Path, rows: list[dict[str, str]]) -> Path:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return path


def _baseline_rows() -> list[dict[str, str]]:
    with BASELINE.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def test_baseline_loader_rejects_duplicate_keys(tmp_path, config):
    rows = _baseline_rows()
    rows.append(dict(rows[0]))
    path = _write_rows(tmp_path / "baseline.csv", rows)
    cuped = _config()
    with pytest.raises(DataValidationError, match="重复键"):
        load_baseline(path, cuped, assignments=load_assignments(ASSIGNMENTS, cuped))


def test_baseline_loader_rejects_unassigned_unit(tmp_path):
    rows = _baseline_rows()
    rows[0]["unit_id"] = "unit-9999"
    path = _write_rows(tmp_path / "baseline.csv", rows)
    cuped = _config()
    with pytest.raises(DataValidationError, match="未分配"):
        load_baseline(path, cuped, assignments=load_assignments(ASSIGNMENTS, cuped))


def test_baseline_loader_rejects_late_measurement(tmp_path):
    rows = _baseline_rows()
    rows[0]["measured_at"] = "2026-03-02T00:00:00+00:00"
    path = _write_rows(tmp_path / "baseline.csv", rows)
    cuped = _config()
    with pytest.raises(DataValidationError, match="早于"):
        load_baseline(path, cuped, assignments=load_assignments(ASSIGNMENTS, cuped))


def test_baseline_loader_rejects_non_finite(tmp_path):
    rows = _baseline_rows()
    rows[0]["baseline_session_depth"] = "nan"
    path = _write_rows(tmp_path / "baseline.csv", rows)
    cuped = _config()
    with pytest.raises(DataValidationError, match="有限数字"):
        load_baseline(path, cuped, assignments=load_assignments(ASSIGNMENTS, cuped))


def test_baseline_loader_rejects_treatment_label(tmp_path):
    rows = _baseline_rows()
    for row in rows:
        row["variant"] = "control"
    path = _write_rows(tmp_path / "baseline.csv", rows)
    cuped = _config()
    with pytest.raises(DataValidationError, match="不得包含 variant"):
        load_baseline(path, cuped, assignments=load_assignments(ASSIGNMENTS, cuped))


def test_baseline_loader_rejects_other_experiment(tmp_path):
    rows = _baseline_rows()
    rows[0]["experiment_id"] = "exp-other-2026"
    path = _write_rows(tmp_path / "baseline.csv", rows)
    cuped = _config()
    with pytest.raises(DataValidationError, match="experiment_id"):
        load_baseline(path, cuped, assignments=load_assignments(ASSIGNMENTS, cuped))


def test_baseline_loader_rejects_other_metric_key(tmp_path):
    rows = _baseline_rows()
    rows[0]["metric_key"] = "activation_rate"
    path = _write_rows(tmp_path / "baseline.csv", rows)
    cuped = _config()
    with pytest.raises(DataValidationError, match="metric_key"):
        load_baseline(path, cuped, assignments=load_assignments(ASSIGNMENTS, cuped))


def test_baseline_fixture_covers_all_units():
    cuped = _config()
    baseline = load_baseline(
        BASELINE, cuped, assignments=load_assignments(ASSIGNMENTS, cuped)
    )
    assert len(baseline) == 1600
    assert {row.metric_key for row in baseline} == {"session_depth"}


# ---------------------------------------------------------------- stats


def test_cuped_theta_uses_control_only():
    # 处理臂 Y 整体抬高不影响 theta：斜率只由对照组决定。
    control_x = [1.0, 2.0, 3.0, 4.0]
    control_y = [2.0, 4.0, 6.0, 8.0]
    theta, reference, variance = cuped_theta(control_x, control_y)
    assert theta == pytest.approx(2.0)
    assert reference == pytest.approx(2.5)
    assert variance > 0.0
    # 相同 X 的处理臂无论 Y 如何变化，估计值保持一致。
    first = cuped_estimate(control_x, control_y, [1.0, 2.0], [3.0, 4.0], 0.05)
    second = cuped_estimate(control_x, control_y, [1.0, 2.0], [300.0, 400.0], 0.05)
    assert first["theta"] == pytest.approx(second["theta"]) == pytest.approx(2.0)


def test_cuped_adjusted_effect_subtracts_centered_covariate():
    control_x = [1.0, 2.0, 3.0, 4.0]
    control_y = [2.0, 4.0, 6.0, 8.0]
    theta, reference, _ = cuped_theta(control_x, control_y)
    adjusted_control = [y - theta * (x - reference) for x, y in zip(control_x, control_y)]
    # theta*(x-ref) 恰好消掉对照组对 X 的依赖，调整后常数。
    assert max(adjusted_control) - min(adjusted_control) == pytest.approx(0.0, abs=1e-12)
    estimate = cuped_estimate(control_x, control_y, [1.0, 2.0], [3.0, 4.0], 0.05)
    assert estimate["result"]["absolute_effect"] == pytest.approx(
        (3.5 - theta * (1.5 - reference)) - adjusted_control[0]
    )


def test_cuped_zero_variance_control_is_not_estimable():
    theta, reference, variance = cuped_theta([2.0, 2.0, 2.0], [1.0, 2.0, 3.0])
    assert theta is None
    assert variance == 0.0
    estimate = cuped_estimate([2.0, 2.0], [1.0, 2.0], [2.0, 2.0], [3.0, 4.0], 0.05)
    assert estimate["estimable"] is False


def test_cuped_single_pair_is_not_estimable():
    estimate = cuped_estimate([1.0], [2.0], [1.0, 2.0], [3.0, 4.0], 0.05)
    assert estimate["estimable"] is False


def test_welch_from_moments_matches_sample_path():
    values_c = [1.0, 2.0, 3.0, 4.0, 5.0]
    values_t = [2.0, 3.0, 4.0, 5.0, 6.0]
    sample = welch_t_test(values_c, values_t, 0.05)
    from app.stats import welch_from_moments
    from statistics import mean, variance

    moments = welch_from_moments(
        mean(values_c), variance(values_c), len(values_c),
        mean(values_t), variance(values_t), len(values_t), 0.05,
    )
    assert moments["absolute_effect"] == pytest.approx(sample["absolute_effect"])
    assert moments["p_value"] == pytest.approx(sample["p_value"])
    assert moments["ci_low"] == pytest.approx(sample["ci_low"])


# ---------------------------------------------------------------- audit


def test_full_coverage_adjusts_and_keeps_raw_separate():
    config = _config()
    dataset = _dataset(config)
    result = _audit(config, dataset)
    primary = result["metrics"]["primary"]
    adjustment = primary["adjustment"]
    assert adjustment["status"] == "adjusted"
    assert adjustment["decision_statistic"] == "adjusted"
    assert adjustment["theta_source"] == "control_only"
    assert adjustment["coverage"] == pytest.approx(1.0)
    assert adjustment["paired_units"] == 1600
    assert adjustment["missing_units"] == 0
    raw, adjusted = adjustment["raw"], adjustment["adjusted"]
    # 决策统计只取调整后口径，原始与调整后各自成对，不交叉。
    assert primary["absolute_effect"] == pytest.approx(adjusted["absolute_effect"])
    assert primary["p_value"] == pytest.approx(adjusted["p_value"])
    assert primary["ci_low"] == pytest.approx(adjusted["ci_low"])
    assert primary["ci_high"] == pytest.approx(adjusted["ci_high"])
    assert adjusted["ci_low"] != pytest.approx(raw["ci_low"])
    # 强相关协变量会明显收窄区间。
    raw_width = raw["ci_high"] - raw["ci_low"]
    adjusted_width = adjusted["ci_high"] - adjusted["ci_low"]
    assert adjusted_width < raw_width
    assert result["flags"]["cuped_adjusted"] is True
    assert result["flags"]["cuped_fallback_raw"] is False
    check = next(item for item in result["checks"] if item["code"] == "baseline.adjustment")
    assert check["status"] == "pass"
    assert check["details"]["theta_source"] == "control_only"


def test_low_coverage_falls_back_to_raw_full_sample():
    config = _config()
    dataset = _dataset(config)
    sparse = [row for index, row in enumerate(dataset.baseline) if index % 10 == 0]
    result = _audit(config, dataset, sparse)
    primary = result["metrics"]["primary"]
    adjustment = primary["adjustment"]
    assert adjustment["status"] == "not_estimable"
    assert adjustment["decision_statistic"] == "raw"
    assert adjustment["reason"] == "coverage_below_threshold"
    assert adjustment["coverage"] < adjustment["min_coverage"]
    assert adjustment["adjusted"] is None
    # 决策统计是全量未调整 Welch：样本量与旧口径一致。
    assert primary["control_n"] == 800 and primary["treatment_n"] == 800
    assert primary["absolute_effect"] == pytest.approx(adjustment["raw"]["absolute_effect"])
    assert primary["p_value"] == pytest.approx(adjustment["raw"]["p_value"])
    assert result["flags"]["cuped_fallback_raw"] is True
    assert "baseline.not_estimable" in {item["code"] for item in result["findings"]}
    check = next(item for item in result["checks"] if item["code"] == "baseline.adjustment")
    assert check["status"] == "warn"


def test_zero_variance_control_baseline_is_not_estimable():
    config = _config()
    dataset = _dataset(config)
    flat = [replace(row, value=3.0) for row in dataset.baseline]
    result = _audit(config, dataset, flat)
    adjustment = result["metrics"]["primary"]["adjustment"]
    assert adjustment["status"] == "not_estimable"
    assert adjustment["reason"] == "theta_not_estimable"
    assert adjustment["theta"] is None
    assert adjustment["decision_statistic"] == "raw"


def test_single_arm_pairs_are_not_estimable():
    config = _config()
    dataset = _dataset(config)
    config = replace(
        config,
        primary=replace(
            config.primary,
            baseline=BaselineSpec(
                file=config.primary.baseline.file,
                column=config.primary.baseline.column,
                min_coverage=0.001,
            ),
        ),
    )
    control_units = [row.unit_id for row in dataset.observations if row.variant == "control"][:5]
    treatment_units = [row.unit_id for row in dataset.observations if row.variant == "treatment"][:1]
    selected = set(control_units) | set(treatment_units)
    partial = [row for row in dataset.baseline if row.unit_id in selected]
    result = _audit(config, dataset, partial)
    adjustment = result["metrics"]["primary"]["adjustment"]
    assert adjustment["reason"] == "insufficient_paired_units"
    assert adjustment["decision_statistic"] == "raw"


def test_baseline_only_on_one_arm_falls_back():
    config = _config()
    dataset = _dataset(config)
    control_units = {row.unit_id for row in dataset.observations if row.variant == "control"}
    one_arm = [row for row in dataset.baseline if row.unit_id in control_units]
    result = _audit(config, dataset, one_arm)
    adjustment = result["metrics"]["primary"]["adjustment"]
    assert adjustment["status"] == "not_estimable"
    assert adjustment["coverage"] == pytest.approx(0.5)


def test_unconfigured_baseline_keeps_legacy_primary(config, dataset, audit_result):
    # 旧 fixture 的 primary 是比例指标且未配置 baseline，结果字段保持原样。
    primary = audit_result["metrics"]["primary"]
    assert "adjustment" not in primary
    assert primary["kind"] == "proportion"
    assert primary["absolute_effect"] == 0.05
    assert "baseline.adjustment" not in {item["code"] for item in audit_result["checks"]}
    assert "cuped_adjusted" not in audit_result["flags"]


def test_proportion_secondary_and_guardrail_unchanged():
    legacy = load_config(FIXTURES / "experiment.yaml")
    cuped = _config()
    old = load_dataset(legacy, ASSIGNMENTS, OBSERVATIONS)
    legacy_primary, _, legacy_guardrails, _ = compute_all_metrics(
        legacy, old.observations, assignments=old.assignments, baseline=[]
    )
    dataset = _dataset(cuped)
    adjusted_primary, secondary, guardrails, _ = compute_all_metrics(
        cuped, dataset.observations,
        assignments=dataset.assignments, baseline=dataset.baseline,
    )
    # 同一条比例指标：在 CUPED 配置里降为 secondary，仍走未调整路径且逐值一致。
    activation = next(item for item in secondary if item.key == "activation_rate")
    assert activation.adjustment is None
    assert activation.kind == "proportion"
    assert activation.absolute_effect == pytest.approx(legacy_primary.absolute_effect)
    assert activation.p_value == pytest.approx(legacy_primary.p_value)
    # 护栏指标完全不变。
    assert guardrails[0].adjustment is None
    assert guardrails[0].degradation == pytest.approx(legacy_guardrails[0].degradation)
    assert guardrails[0].p_value == pytest.approx(legacy_guardrails[0].p_value)


def test_stratification_uses_adjusted_analysis_sample():
    config = _config()
    dataset = _dataset(config)
    result = _audit(config, dataset)
    platforms = next(
        field for field in result["stratification"]["fields"] if field["field"] == "platform"
    )
    # 调整只改变主指标的 Y，分层组数与样本量保留全量配对样本。
    assert sum(group["control_n"] for group in platforms["groups"]) == 800
    assert sum(group["treatment_n"] for group in platforms["groups"]) == 800


def test_missing_baseline_data_raises_when_declared():
    config = _config()
    dataset = _dataset(config)
    with pytest.raises(ValueError, match="缺少分配或基线数据"):
        compute_all_metrics(
            config, dataset.observations,
            assignments=dataset.assignments, baseline=None,
        )


# ---------------------------------------------------------------- reporting


def test_markdown_reports_both_statistics_without_mixing(tmp_path):
    from app.reporting import render_markdown, write_artifacts

    config = _config()
    dataset = _dataset(config)
    result = _audit(config, dataset)
    markdown = render_markdown(result)
    assert "## 实验前协变量调整（CUPED）" in markdown
    assert "原始 raw" in markdown and "调整后 adjusted" in markdown
    assert "不得交叉引用" in markdown
    assert "theta 来源：`control_only`" in markdown
    written = write_artifacts(result, tmp_path / "report")
    assert written["report.html"].exists()
    html = written["report.html"].read_text(encoding="utf-8")
    assert "CUPED" in html
    assert "http://" not in html and "https://" not in html


def test_decision_card_exposes_primary_statistic():
    from app.reporting import decision_card_payload

    config = _config()
    dataset = _dataset(config)
    result = _audit(config, dataset)
    card = decision_card_payload(result)
    assert card["primary_statistic"] == "adjusted"
    assert card["primary_metric"]["adjustment"]["decision_statistic"] == "adjusted"


def test_ledger_persists_adjustment_state(tmp_path):
    from app import ledger

    config = _config()
    dataset = _dataset(config)
    result = _audit(config, dataset)
    db = tmp_path / "ledger.sqlite3"
    ledger.save_run(db, result, "out/run-cuped-0000001")
    row = ledger.list_runs(db)[0]
    assert row["primary_statistic"] == "adjusted"
    assert row["primary_adjustment_status"] == "adjusted"
    assert row["primary_theta"] == pytest.approx(0.826906, abs=1e-5)
    assert row["primary_baseline_coverage"] == pytest.approx(1.0)


def test_ledger_migrates_legacy_schema(tmp_path, audit_result):
    import sqlite3

    from app import ledger

    db = tmp_path / "legacy.sqlite3"
    # 旧库只按最初的表结构建表，不含后加的调整列。
    with sqlite3.connect(db) as connection:
        connection.executescript(ledger.SCHEMA)
        connection.execute(
            """INSERT INTO runs (
                run_id, generated_at, experiment_id, experiment_name, decision,
                decision_title, matched_rule, decision_reason, headline,
                primary_metric_key, primary_effect, primary_p_value, finding_count,
                input_digest, config_digest, artifact_dir, decision_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "run-legacy-0000001", "2026-03-01T00:00:00+00:00",
                "exp-legacy", "旧实验", "ship", "可上线", "legacy-rule",
                "旧规则", "旧摘要", "activation_rate", 0.1, 0.01, 0,
                "digest", "config", "out/run-legacy-0000001", "{}",
            ),
        )
    ledger.init_db(db)
    with sqlite3.connect(db) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(runs)").fetchall()
        }
    assert {"primary_statistic", "primary_adjustment_status",
            "primary_theta", "primary_baseline_coverage"} <= columns
    legacy_row = next(row for row in ledger.list_runs(db) if row["run_id"] == "run-legacy-0000001")
    assert legacy_row["primary_statistic"] == "raw"
    assert legacy_row["primary_adjustment_status"] is None
    ledger.save_run(db, audit_result, "out/run-test-00000001")
    row = next(item for item in ledger.list_runs(db) if item["run_id"] == "run-test-00000001")
    assert row["primary_statistic"] == "raw"
    assert row["primary_adjustment_status"] is None


def test_cli_demo_with_baseline(tmp_path):
    import shutil

    from app import ledger
    from app.cli import main

    root = tmp_path / "project"
    (root / "fixtures").mkdir(parents=True)
    for name in ("experiment_cuped.yaml", "assignments.csv", "observations.csv", "baseline.csv"):
        shutil.copy(FIXTURES / name, root / "fixtures" / name)
    code = main([
        "--root", str(root), "demo",
        "--config", "fixtures/experiment_cuped.yaml",
    ])
    assert code == 0
    row = ledger.list_runs(root / "data" / "ledger.sqlite3")[0]
    assert row["primary_statistic"] == "adjusted"


def test_cli_rejects_missing_baseline(tmp_path, capsys):
    import shutil

    from app.cli import main

    root = tmp_path / "project"
    (root / "fixtures").mkdir(parents=True)
    for name in ("experiment_cuped.yaml", "assignments.csv", "observations.csv"):
        shutil.copy(FIXTURES / name, root / "fixtures" / name)
    code = main([
        "--root", str(root), "demo",
        "--config", "fixtures/experiment_cuped.yaml",
    ])
    assert code == 2
    assert "baseline.csv" in capsys.readouterr().err


# ---------------------------------------------------------------- identity


def test_theta_does_not_use_treatment_outcomes():
    """只改变处理组 Y，不应改变 theta / reference_mean。"""
    config = _config()
    dataset = _dataset(config)
    first = _audit(config, dataset)
    shifted = [
        replace(row, values={**row.values, "session_depth": row.values["session_depth"] + 5.0})
        if row.variant == "treatment" else row
        for row in dataset.observations
    ]
    second = run_audit(
        config, dataset.assignments, shifted,
        run_id="run-cuped-0000002", generated_at="2026-03-16T00:00:00+00:00",
        input_digest="x", config_digest="y", baseline=dataset.baseline,
    )
    a1 = first["metrics"]["primary"]["adjustment"]
    a2 = second["metrics"]["primary"]["adjustment"]
    assert a1["theta"] == pytest.approx(a2["theta"])
    assert a1["reference_mean"] == pytest.approx(a2["reference_mean"])
    # 处理组整体抬升会改变效应，但不会改变斜率。
    assert a2["adjusted"]["absolute_effect"] == pytest.approx(
        a1["adjusted"]["absolute_effect"] + 5.0
    )


def test_baseline_file_must_match_experiment_and_metric(tmp_path):
    cuped = _config()
    assignments = load_assignments(ASSIGNMENTS, cuped)
    rows = _baseline_rows()
    rows[0]["experiment_id"] = "exp-not-this-one"
    path = _write_rows(tmp_path / "baseline.csv", rows)
    with pytest.raises(DataValidationError, match="experiment_id"):
        load_baseline(path, cuped, assignments=assignments)


def test_cli_baseline_override_path(tmp_path):
    import shutil

    from app import ledger
    from app.cli import main

    root = tmp_path / "project"
    (root / "fixtures").mkdir(parents=True)
    for name in ("experiment_cuped.yaml", "assignments.csv", "observations.csv"):
        shutil.copy(FIXTURES / name, root / "fixtures" / name)
    shutil.copy(BASELINE, root / "fixtures" / "alt_baseline.csv")
    code = main([
        "--root", str(root), "demo",
        "--config", "fixtures/experiment_cuped.yaml",
        "--baseline", "fixtures/alt_baseline.csv",
    ])
    assert code == 0
    row = ledger.list_runs(root / "data" / "ledger.sqlite3")[0]
    assert row["primary_adjustment_status"] == "adjusted"


def test_primary_finding_evidence_uses_decision_statistic_only():
    config = _config()
    dataset = _dataset(config)
    result = _audit(config, dataset)
    primary = result["metrics"]["primary"]
    adjustment = primary["adjustment"]
    # 主指标 finding 的 evidence 是选中的决策统计，而不是 raw。
    primary_checks = [
        item for item in result["checks"] if item["code"] == "metric.primary"
    ]
    assert primary_checks[0]["details"]["statistic"] == "adjusted"
    assert primary_checks[0]["details"]["ci"] == [
        adjustment["adjusted"]["ci_low"], adjustment["adjusted"]["ci_high"]
    ]
    assert primary_checks[0]["details"]["p_value"] == pytest.approx(
        adjustment["adjusted"]["p_value"]
    )
    assert primary["ci_low"] != pytest.approx(adjustment["raw"]["ci_low"])


def test_fallback_primary_finding_uses_raw_only():
    config = _config()
    dataset = _dataset(config)
    sparse = [row for index, row in enumerate(dataset.baseline) if index % 10 == 0]
    result = _audit(config, dataset, sparse)
    primary = result["metrics"]["primary"]
    adjustment = primary["adjustment"]
    checks = [item for item in result["checks"] if item["code"] == "metric.primary"]
    assert checks[0]["details"]["statistic"] == "raw"
    assert checks[0]["details"]["ci"] == [
        adjustment["raw"]["ci_low"], adjustment["raw"]["ci_high"]
    ]
    assert primary["ci_low"] == pytest.approx(adjustment["raw"]["ci_low"])
    assert primary["ci_high"] == pytest.approx(adjustment["raw"]["ci_high"])


def test_partial_coverage_above_threshold_adjusts_on_paired_sample():
    config = _config()
    dataset = _dataset(config)
    # 保留 95% 的基线，覆盖仍高于 90% 门槛：调整在配对子样本上进行，并报告缺失。
    partial = [
        row for index, row in enumerate(dataset.baseline) if index % 20 != 0
    ]
    result = _audit(config, dataset, partial)
    adjustment = result["metrics"]["primary"]["adjustment"]
    assert adjustment["status"] == "adjusted"
    assert adjustment["decision_statistic"] == "adjusted"
    assert adjustment["paired_units"] == 1520
    assert adjustment["missing_units"] == 80
    assert adjustment["coverage"] == pytest.approx(0.95)
    assert adjustment["coverage_ok"] is True
    assert result["metrics"]["primary"]["control_n"] + result["metrics"]["primary"]["treatment_n"] == 1520
