"""结果成熟期在配置、报告、台账与 Web 层的展示契约。"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from app import ledger
from app.audit import run_audit
from app.config import ConfigError, load_config
from app.models import Assignment, Observation
from app.reporting import (
    decision_card_payload,
    render_html,
    render_markdown,
    write_artifacts,
)

T0 = datetime(2026, 3, 1, tzinfo=timezone.utc)
CONFIG = """
schema_version: 1
experiment:
  id: exp-report-maturity
  name: 报告成熟期实验
  owner: qa
  hypothesis: 报告展示
  start: '2026-03-01T00:00:00+00:00'
  end: '2026-03-31T00:00:00+00:00'
control: control
variants:
  control: {label: 对照组}
  treatment: {label: 实验组}
split:
  expected: {control: 0.5, treatment: 0.5}
  tolerance: 0.2
  min_assignments: 4
exposure: {max_per_unit: 1, tolerance: 0}
metrics:
  primary:
    key: activation_rate
    column: activated
    label: 7 日激活率
    kind: proportion
    orientation: higher_is_better
    min_effect: 0.0
    outcome_horizon: 7
  guardrails:
    - key: crash_rate
      column: crashed
      label: 崩溃率
      kind: proportion
      orientation: lower_is_better
      max_degradation: 0.5
      outcome_horizon: 3
stratification:
  fields: [platform]
  min_group_size: 2
  max_groups: 4
  alpha: 0.05
  min_favorable_share: 0.5
decision:
  fallback: review
  cards:
    ship: {title: 可上线, action: go}
    iterate: {title: 继续迭代, action: wait}
    stop: {title: 停止, action: halt}
    review: {title: 人工复核, action: check}
  rules:
    - id: review-pending
      card: review
      priority: 10
      reason: pending cohort
      when:
        any:
          - flag: measurement_provisional
"""


def _write_config(tmp_path, text: str = CONFIG):
    path = tmp_path / "experiment.yaml"
    path.write_text(text, encoding="utf-8")
    return load_config(path)


def _fixture(config):
    old = T0
    new = T0 + timedelta(days=25)
    assignments = []
    observations = []
    for index in range(1, 41):
        variant = "control" if index <= 20 else "treatment"
        unit = f"unit-{index:04d}"
        assignments.append(
            Assignment(unit, config.experiment_id, variant, old,
                       {"platform": "mobile"})
        )
        observations.append(
            Observation(unit, config.experiment_id, variant,
                        old + timedelta(days=8),
                        {"activation_rate": 1.0 if variant == "treatment" else 0.0,
                         "crash_rate": 0.0})
        )
    for index in range(41, 61):
        variant = "control" if index <= 50 else "treatment"
        unit = f"unit-{index:04d}"
        assignments.append(
            Assignment(unit, config.experiment_id, variant, new,
                       {"platform": "mobile"})
        )
        observations.append(
            Observation(unit, config.experiment_id, variant,
                        new + timedelta(days=1),
                        {"activation_rate": 0.0, "crash_rate": 1.0})
        )
    return assignments, observations


def _run(config, assignments, observations, generated_at):
    return run_audit(
        config, assignments, observations,
        run_id="run-report-maturity",
        generated_at=generated_at.isoformat(),
        input_digest="a" * 64,
        config_digest="b" * 64,
    )


def test_config_accepts_and_validates_outcome_horizon(tmp_path):
    config = _write_config(tmp_path)
    assert config.primary.outcome_horizon == 7.0
    assert config.guardrails[0].outcome_horizon == 3.0
    config = replace(config, primary=replace(config.primary, outcome_horizon=None))
    assert config.primary.has_horizon is False

    bad = CONFIG.replace("outcome_horizon: 7", "outcome_horizon: 0")
    with pytest.raises(ConfigError, match="outcome_horizon"):
        _write_config(tmp_path, bad)
    bad = CONFIG.replace("outcome_horizon: 7", "outcome_horizon: -1")
    with pytest.raises(ConfigError, match="outcome_horizon"):
        _write_config(tmp_path, bad)
    bad = CONFIG.replace("outcome_horizon: 7", "outcome_horizon: soon")
    with pytest.raises(ConfigError, match="outcome_horizon"):
        _write_config(tmp_path, bad)


def test_config_without_horizon_loads_as_none(tmp_path):
    text = CONFIG.replace("    outcome_horizon: 7\n", "")
    text = text.replace("      outcome_horizon: 3\n", "")
    config = _write_config(tmp_path, text)
    assert config.primary.outcome_horizon is None
    assert config.guardrails[0].outcome_horizon is None


def test_decision_card_exposes_provisional_and_measurement(tmp_path):
    config = _write_config(tmp_path)
    assignments, observations = _fixture(config)
    result = _run(config, assignments, observations, T0 + timedelta(days=26))
    card = decision_card_payload(result)
    assert card["provisional"] is True
    assert card["decision"]["card"] == "review"
    assert card["measurement"]["tracked"] is True
    assert card["measurement"]["primary"]["pending_units"] == 20
    assert card["measurement"]["primary"]["status"] == "provisional"


def test_markdown_and_html_show_maturity_split(tmp_path):
    config = _write_config(tmp_path)
    assignments, observations = _fixture(config)
    result = _run(config, assignments, observations, T0 + timedelta(days=26))
    markdown = render_markdown(result)
    assert "## 结果成熟期" in markdown
    assert "等待中的 cohort（主指标）" in markdown
    assert "2026-03-26" in markdown
    assert "provisional" in markdown
    assert "成熟缺数" in markdown
    html = render_html(result)
    assert "结果成熟期" in html
    assert "provisional" in html
    assert "等待中" in html or "pending" in html
    assert "http://" not in html and "https://" not in html
    assert "<script" not in html.lower() and "<link" not in html.lower()


def test_artifacts_and_ledger_persist_measurement(tmp_path):
    config = _write_config(tmp_path)
    assignments, observations = _fixture(config)
    result = _run(config, assignments, observations, T0 + timedelta(days=26))
    written = write_artifacts(result, tmp_path / "out")
    audit = json.loads(written["audit.json"].read_text(encoding="utf-8"))
    assert audit["measurement"]["primary"]["metric_key"] == "activation_rate"
    assert audit["measurement"]["primary"]["pending_units"] == 20
    assert audit["run"]["provisional"] is True

    db = tmp_path / "ledger.sqlite3"
    ledger.save_run(db, result, "out/run-report-maturity")
    row = ledger.list_runs(db)[0]
    assert row["decision"] == "review"
    assert row["provisional"] == 1
    assert row["measurement_status"] == "provisional"
    assert row["pending_units"] == 20
    assert row["mature_but_missing_units"] == 0
    assert row["maturity_coverage"] is not None


def test_ledger_migrates_old_schema(tmp_path):
    import sqlite3

    db = tmp_path / "old.sqlite3"
    connection = sqlite3.connect(db)
    connection.executescript(
        """
        CREATE TABLE runs (
            run_id TEXT PRIMARY KEY,
            generated_at TEXT NOT NULL,
            experiment_id TEXT NOT NULL,
            experiment_name TEXT NOT NULL,
            decision TEXT NOT NULL,
            decision_title TEXT NOT NULL,
            matched_rule TEXT NOT NULL,
            decision_reason TEXT NOT NULL,
            headline TEXT NOT NULL,
            primary_metric_key TEXT NOT NULL,
            primary_effect REAL NOT NULL,
            primary_p_value REAL NOT NULL,
            finding_count INTEGER NOT NULL,
            input_digest TEXT NOT NULL,
            config_digest TEXT NOT NULL,
            artifact_dir TEXT NOT NULL,
            decision_json TEXT NOT NULL
        );
        """
    )
    connection.execute(
        "INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("run-old-00000001", "2026-03-01T00:00:00+00:00", "exp", "old",
         "ship", "可上线", "rule", "reason", "headline", "activation_rate",
         0.1, 0.01, 0, "d", "e", "out/old", "{}"),
    )
    connection.commit()
    connection.close()
    ledger.init_db(db)
    rows = ledger.list_runs(db)
    assert len(rows) == 1
    assert rows[0]["provisional"] == 0
    assert rows[0]["measurement_status"] == "legacy"
    assert rows[0]["pending_units"] == 0
