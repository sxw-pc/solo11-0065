import json
from pathlib import Path

from app import ledger
from app.decision import decide, evaluate_condition, referenced_flags
from app.models import DecisionCard, DecisionRule, DecisionSpec
from app.reporting import ARTIFACTS, render_html, render_markdown, write_artifacts


def test_boolean_rule_conditions():
    flags = {"a": True, "b": False, "c": True}
    assert evaluate_condition({"flag": "a"}, flags) is True
    assert evaluate_condition({"all": [{"flag": "a"}, {"flag": "c"}]}, flags) is True
    assert evaluate_condition({"any": [{"flag": "b"}, {"flag": "c"}]}, flags) is True
    assert evaluate_condition({"none": [{"flag": "b"}]}, flags) is True
    assert referenced_flags(
        {"all": [{"flag": "a"}, {"any": [{"flag": "b"}]}]}
    ) == {"a", "b"}


def test_rule_priority_and_fallback():
    cards = {
        key: DecisionCard(key, key.upper(), f"action-{key}")
        for key in ("ship", "iterate", "stop", "review")
    }
    spec = DecisionSpec(
        cards=cards,
        fallback="review",
        rules=(
            DecisionRule("low", "iterate", 10, "low", {"flag": "evidence"}),
            DecisionRule("high", "stop", 20, "high", {"flag": "risk"}),
        ),
    )
    assert decide(spec, {"risk": True, "evidence": True})["card"] == "stop"
    assert decide(spec, {"evidence": True})["card"] == "iterate"
    fallback = decide(spec, {})
    assert fallback["card"] == "review"
    assert fallback["matched_rule"] == "fallback"


def test_artifacts_are_complete_and_standalone(audit_result, tmp_path):
    written = write_artifacts(audit_result, tmp_path / "report")
    assert set(written) == set(ARTIFACTS)
    audit = json.loads((tmp_path / "report" / "audit.json").read_text(encoding="utf-8"))
    assert audit["decision"]["card"] == "iterate"
    card = json.loads(
        (tmp_path / "report" / "decision_card.json").read_text(encoding="utf-8")
    )
    assert card["decision"]["matched_rule"] == "iterate-evidence"
    markdown = (tmp_path / "report" / "report.md").read_text(encoding="utf-8")
    assert "## 决策卡" in markdown
    assert "stratification.reversal" in markdown
    html = (tmp_path / "report" / "report.html").read_text(encoding="utf-8")
    assert "继续迭代" in html
    assert "http://" not in html and "https://" not in html
    assert "<script" not in html.lower() and "<link" not in html.lower()
    assert len(html.splitlines()) <= 300
    assert len(render_html(audit_result)) > 1000
    assert render_markdown(audit_result).startswith("# 分流验台审计报告")


def test_markdown_contains_all_metric_roles(audit_result):
    markdown = render_markdown(audit_result)
    assert "activation_rate" not in markdown
    assert "激活率" in markdown
    assert "崩溃率" in markdown
    assert "会话深度" in markdown
    assert "规则评估" not in markdown


def test_ledger_round_trip(audit_result, tmp_path):
    db = tmp_path / "data" / "ledger.sqlite3"
    ledger.save_run(db, audit_result, "out/run-test-00000001")
    ledger.save_run(db, audit_result, "out/run-test-00000001")
    assert ledger.count_runs(db) == 1
    runs = ledger.list_runs(db)
    assert len(runs) == 1
    assert runs[0]["decision"] == "iterate"
    detail = ledger.get_run(db, runs[0]["run_id"])
    assert detail["primary_effect"] == 0.05
    assert len(detail["findings"]) == 3
    assert ledger.list_runs(db, decision="ship") == []
    assert ledger.get_run(db, "run-does-not-exist") is None
    assert ledger.get_run(db, "../escape") is None
