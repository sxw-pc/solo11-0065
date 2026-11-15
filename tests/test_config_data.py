from pathlib import Path
import pytest
from app.config import ConfigError, load_config
from app.data import (
    DataValidationError,
    load_assignments,
    load_observations,
    sha256_bytes,
    sha256_files,
)
from app.safety import PathSecurityError, ensure_output_path, resolve_under
ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"
CONFIG_PATH = FIXTURES / "experiment.yaml"
ASSIGNMENTS_PATH = FIXTURES / "assignments.csv"
OBSERVATIONS_PATH = FIXTURES / "observations.csv"
def _mutated_config(tmp_path: Path, old: str, new: str) -> Path:
    text = CONFIG_PATH.read_text(encoding="utf-8")
    assert old in text
    target = tmp_path / "experiment.yaml"
    target.write_text(text.replace(old, new, 1), encoding="utf-8")
    return target
def test_main_config_contract(config):
    assert config.experiment_id == "exp-onboarding-card-2026"
    assert config.control == "control"
    assert [variant.key for variant in config.variants] == ["control", "treatment"]
    assert config.primary.key == "activation_rate"
    assert config.guardrails[0].orientation == "lower_is_better"
    assert config.stratification.fields == ("platform", "region")
    assert config.decision.fallback == "review"
    assert config.split.expected == {"control": 0.5, "treatment": 0.5}
def test_config_rejects_wrong_schema(tmp_path):
    path = _mutated_config(tmp_path, "schema_version: 1", "schema_version: 2")
    with pytest.raises(ConfigError, match="schema_version"):
        load_config(path)
def test_config_rejects_bad_expected_sum(tmp_path):
    path = _mutated_config(tmp_path, "control: 0.5", "control: 0.4")
    with pytest.raises(ConfigError, match="之和"):
        load_config(path)
def test_config_rejects_missing_primary(tmp_path):
    path = _mutated_config(tmp_path, "  primary:\n", "  primary_disabled:\n")
    with pytest.raises(ConfigError):
        load_config(path)
def test_config_rejects_unknown_orientation(tmp_path):
    path = _mutated_config(
        tmp_path,
        "orientation: higher_is_better\n    min_effect",
        "orientation: larger_is_better\n    min_effect",
    )
    with pytest.raises(ConfigError, match="orientation"):
        load_config(path)
def test_config_rejects_bad_rule_condition(tmp_path):
    path = _mutated_config(tmp_path, "          - flag: primary_inconclusive",
                           "          - flg: primary_inconclusive")
    with pytest.raises(ConfigError, match="flag"):
        load_config(path)
def test_dataset_loads_expected_fixture(config):
    assignments = load_assignments(ASSIGNMENTS_PATH, config)
    observations = load_observations(OBSERVATIONS_PATH, config)
    assert len(assignments) == 1600
    assert len(observations) == 1600
    assert {row.unit_id for row in assignments} == {
        row.unit_id for row in observations
    }
def test_assignments_reject_missing_column(tmp_path, config):
    lines = ASSIGNMENTS_PATH.read_text(encoding="utf-8").splitlines()
    malformed = tmp_path / "assignments.csv"
    malformed.write_text("\n".join(line.replace(",region", "") for line in lines),
                         encoding="utf-8")
    with pytest.raises(DataValidationError, match="region"):
        load_assignments(malformed, config)
def test_assignments_reject_unsafe_unit_id(tmp_path, config):
    lines = ASSIGNMENTS_PATH.read_text(encoding="utf-8").splitlines()
    lines[1] = lines[1].replace("unit-0001", "user-0001", 1)
    malformed = tmp_path / "assignments.csv"
    malformed.write_text("\n".join(lines), encoding="utf-8")
    with pytest.raises(DataValidationError, match="unit_id"):
        load_assignments(malformed, config)
def test_observations_reject_non_binary_proportion(tmp_path, config):
    lines = OBSERVATIONS_PATH.read_text(encoding="utf-8").splitlines()
    fields = lines[1].split(",")
    fields[4] = "2"
    lines[1] = ",".join(fields)
    malformed = tmp_path / "observations.csv"
    malformed.write_text("\n".join(lines), encoding="utf-8")
    with pytest.raises(DataValidationError, match="0 或 1"):
        load_observations(malformed, config)
def test_sha256_files_changes_with_content(tmp_path):
    one = tmp_path / "one.txt"
    one.write_text("a", encoding="utf-8")
    first = sha256_files([one])
    one.write_text("b", encoding="utf-8")
    assert first != sha256_files([one])
    assert sha256_bytes(b"a") != sha256_bytes(b"b")

def test_path_root_constraint(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    inside = root / "fixtures" / "ok.csv"
    inside.parent.mkdir()
    inside.write_text("x", encoding="utf-8")
    outside = tmp_path / "outside.csv"
    outside.write_text("x", encoding="utf-8")
    assert resolve_under(root, "fixtures/ok.csv", must_exist=True) == inside.resolve()
    with pytest.raises(PathSecurityError):
        resolve_under(root, "../outside.csv", must_exist=True)
    with pytest.raises(PathSecurityError):
        resolve_under(root, outside)
    output = ensure_output_path(root, "out/report.txt")
    assert output.parent == (root / "out").resolve()
