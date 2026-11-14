from __future__ import annotations

from pathlib import Path

import pytest

from app.audit import run_audit
from app.config import load_config
from app.data import load_dataset, sha256_bytes, sha256_files

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"
CONFIG_PATH = FIXTURES / "experiment.yaml"
ASSIGNMENTS_PATH = FIXTURES / "assignments.csv"
OBSERVATIONS_PATH = FIXTURES / "observations.csv"


@pytest.fixture(scope="session")
def config():
    return load_config(CONFIG_PATH)


@pytest.fixture(scope="session")
def dataset(config):
    return load_dataset(config, ASSIGNMENTS_PATH, OBSERVATIONS_PATH)


@pytest.fixture(scope="session")
def audit_result(config, dataset):
    digest = sha256_files([CONFIG_PATH, ASSIGNMENTS_PATH, OBSERVATIONS_PATH])
    return run_audit(
        config,
        dataset.assignments,
        dataset.observations,
        run_id="run-test-00000001",
        generated_at="2026-03-16T00:00:00+00:00",
        input_digest=digest,
        config_digest=sha256_bytes(CONFIG_PATH.read_bytes()),
    )


@pytest.fixture
def make_result(config, dataset):
    def build():
        digest = sha256_files([CONFIG_PATH, ASSIGNMENTS_PATH, OBSERVATIONS_PATH])
        return run_audit(
            config,
            dataset.assignments,
            dataset.observations,
            run_id="run-test-00000002",
            generated_at="2026-03-16T00:00:00+00:00",
            input_digest=digest,
            config_digest=sha256_bytes(CONFIG_PATH.read_bytes()),
        )

    return build
