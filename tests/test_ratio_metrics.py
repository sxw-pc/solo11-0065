"""ratio 指标类型的端到端契约测试。

覆盖：
H1 输入边界：零分母/全臂零分母/量级悬殊/正负分子与负分母/与 proportion、mean 混用/
            两臂不同零分母比例；
H2 链路：config 双列校验 -> data 成对载入 -> metrics 配对 delta-method 方差 ->
         MetricResult/payload -> guardrail/primary check -> decision flags；
H3 口径：pooled ratio（按分母规模加权）与 unit_mean（每随机化 unit 等权）不混用。
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.audit import run_audit
from app.config import ConfigError, load_config
from app.data import DataValidationError, load_dataset, load_observations
from app.metrics import compute_metric
from app.models import MetricSpec, Observation
from app.stats import pooled_ratio_test, welch_t_test

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"
CONFIG_PATH = FIXTURES / "experiment.yaml"
STAMP = datetime(2026, 3, 5, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 构造辅助
# ---------------------------------------------------------------------------

def _ratio_spec(**overrides) -> MetricSpec:
    data = dict(
        key="revenue_per_order",
        column=None,
        label="每订单收入",
        kind="ratio",
        orientation="higher_is_better",
        alpha=0.05,
        min_effect=0.0,
        max_degradation=0.0,
        numerator_column="revenue",
        denominator_column="orders",
    )
    data.update(overrides)
    return MetricSpec(**data)


def _ratio_config(tmp_path: Path, *, primary_kind: str = "ratio",
                  guardrail_kind: str = "ratio",
                  primary_min_effect: float = 0.0,
                  guardrail_max_degradation: float = 0.05,
                  primary_alpha: float = 0.05) -> Path:
    """生成含 ratio 指标的最小实验配置（基于现有 fixture 改写）。"""
    text = CONFIG_PATH.read_text(encoding="utf-8")
    ratio_primary = (
        "  primary:\n"
        "    key: revenue_per_order\n"
        "    label: 每订单收入\n"
        "    kind: ratio\n"
        "    numerator_column: revenue\n"
        "    denominator_column: orders\n"
        "    orientation: higher_is_better\n"
        f"    min_effect: {primary_min_effect}\n"
        f"    alpha: {primary_alpha}\n"
    )
    mean_primary = (
        "  primary:\n"
        "    key: activation_rate\n"
        "    column: activated\n"
        "    label: 激活率\n"
        "    kind: proportion\n"
        "    orientation: higher_is_better\n"
        "    min_effect: 0.02\n"
        "    alpha: 0.05\n"
    )
    if primary_kind == "ratio":
        text = text.replace(
            "  primary:\n"
            "    key: activation_rate\n"
            "    column: activated\n"
            "    label: 激活率\n"
            "    kind: proportion\n"
            "    orientation: higher_is_better\n"
            "    min_effect: 0.02\n"
            "    alpha: 0.05\n",
            ratio_primary,
        )
    else:
        text = text.replace(
            "  primary:\n"
            "    key: activation_rate\n"
            "    column: activated\n"
            "    label: 激活率\n"
            "    kind: proportion\n"
            "    orientation: higher_is_better\n"
            "    min_effect: 0.02\n"
            "    alpha: 0.05\n",
            mean_primary,
        )
    ratio_guardrail = (
        "  guardrails:\n"
        "    - key: error_per_request\n"
        "      label: 每请求错误数\n"
        "      kind: ratio\n"
        "      numerator_column: errors\n"
        "      denominator_column: requests\n"
        "      orientation: lower_is_better\n"
        f"      max_degradation: {guardrail_max_degradation}\n"
        "      alpha: 0.05\n"
    )
    mean_guardrail = (
        "  guardrails:\n"
        "    - key: crash_rate\n"
        "      column: crashed\n"
        "      label: 崩溃率\n"
        "      kind: proportion\n"
        "      orientation: lower_is_better\n"
        "      max_degradation: 0.01\n"
        "      alpha: 0.05\n"
    )
    old_guardrail = mean_guardrail
    new_guardrail = ratio_guardrail if guardrail_kind == "ratio" else mean_guardrail
    assert old_guardrail in text
    text = text.replace(old_guardrail, new_guardrail)
    target = tmp_path / "experiment.yaml"
    target.write_text(text, encoding="utf-8")
    return target


def _write_observations(tmp_path: Path, rows: list[tuple]) -> Path:
    # 第 9 列同时命名为 errors/crashed 不可能；ratio 护栏读 errors，
    # proportion 护栏读 crashed，因此输出两个别名列，值相同。
    header = ("unit_id,experiment_id,variant,observed_at,"
              "activated,session_depth,revenue,orders,errors,crashed,requests\n")
    lines = [header]
    for unit_id, variant, revenue, orders, errors_or_crashed, requests_ in rows:
        lines.append(
            f"unit-{unit_id:04d},exp-onboarding-card-2026,{variant},"
            f"2026-03-05T00:00:00+00:00,0,1.0,{revenue},{orders},"
            f"{errors_or_crashed},{errors_or_crashed},{requests_}\n"
        )
    target = tmp_path / "observations.csv"
    target.write_text("".join(lines), encoding="utf-8")
    return target


def _write_assignments(tmp_path: Path, unit_count: int) -> Path:
    lines = ["unit_id,experiment_id,variant,assigned_at,platform,region\n"]
    for unit_id in range(1, unit_count + 1):
        variant = "control" if unit_id % 2 else "treatment"
        lines.append(
            f"unit-{unit_id:04d},exp-onboarding-card-2026,{variant},"
            "2026-03-02T00:00:00+00:00,desktop,north\n"
        )
    target = tmp_path / "assignments.csv"
    target.write_text("".join(lines), encoding="utf-8")
    return target


def _pair_observations(spec: MetricSpec, control: list[tuple[float, float]],
                       treatment: list[tuple[float, float]]) -> list[Observation]:
    rows = []
    for index, (num, den) in enumerate(control, start=1):
        rows.append(Observation(f"unit-{index:04d}", "exp-x", "control", STAMP,
                                {}, {spec.key: (num, den)}))
    for index, (num, den) in enumerate(treatment, start=len(control) + 1):
        rows.append(Observation(f"unit-{index:04d}", "exp-x", "treatment", STAMP,
                                {}, {spec.key: (num, den)}))
    return rows


# ---------------------------------------------------------------------------
# stats.pooled_ratio_test：点估计与配对 delta-method 方差
# ---------------------------------------------------------------------------

def test_pooled_ratio_point_estimate_matches_ratio_of_sums():
    control = [(100.0, 10.0), (300.0, 30.0)]          # 400 / 40 = 10
    treatment = [(220.0, 20.0), (360.0, 30.0)]        # 580 / 50 = 11.6
    result = pooled_ratio_test(control, treatment, 0.05)
    assert result["estimable"] is True
    assert result["control_value"] == pytest.approx(10.0)
    assert result["treatment_value"] == pytest.approx(11.6)
    assert result["absolute_effect"] == pytest.approx(1.6)
    assert result["control_numerator_sum"] == 400.0
    assert result["control_denominator_sum"] == 40.0
    assert result["treatment_numerator_sum"] == 580.0
    assert result["treatment_denominator_sum"] == 50.0


def test_pooled_ratio_variance_uses_within_unit_paired_covariance():
    # 两组残差结构：y = R*d + e，其中 e 与 d 相关；
    # 手工按影响函数 residual_i = y_i - R*d_i 复算方差。
    control = [(12.0, 10.0), (104.0, 100.0), (0.0, 0.0)]
    treatment = [(30.0, 20.0), (110.0, 100.0), (0.0, 0.0)]
    result = pooled_ratio_test(control, treatment, 0.05)

    def arm_variance(pairs):
        num = sum(y for y, _ in pairs)
        den = sum(d for _, d in pairs)
        r = num / den
        ss = sum((y - r * d) ** 2 for y, d in pairs)
        return ss / len(pairs) / den ** 2

    expected_var = arm_variance(control) + arm_variance(treatment)
    assert result["standard_error"] == pytest.approx(math.sqrt(expected_var))
    # p 与 CI 必须来自同一个推断方差
    z = 1.959963984540054
    effect = result["absolute_effect"]
    assert result["ci_low"] == pytest.approx(effect - z * result["standard_error"])
    assert result["ci_high"] == pytest.approx(effect + z * result["standard_error"])
    expected_p = math.erfc(
        abs(effect / result["standard_error"]) / math.sqrt(2.0)
    )
    assert result["p_value"] == pytest.approx(expected_p)
    assert result["control_zero_denominator_units"] == 1
    assert result["treatment_zero_denominator_units"] == 1
    assert result["control_effective_units"] == 2
    assert result["treatment_effective_units"] == 2


def test_pooled_ratio_zero_denominator_rows_count_but_weigh_nothing():
    # 零分母且零分子的 unit 计入 n，但对总和与方差贡献 0。
    pairs = [(0.0, 0.0), (10.0, 5.0), (30.0, 15.0)]
    result = pooled_ratio_test(pairs, [(20.0, 10.0), (40.0, 10.0)], 0.05)
    assert result["control_value"] == pytest.approx(2.0)  # 40/20，零行不影响
    assert result["control_zero_denominator_units"] == 1
    assert result["control_effective_units"] == 2


def test_pooled_ratio_total_zero_denominator_is_not_estimable():
    result = pooled_ratio_test([(0.0, 0.0), (0.0, 0.0)],
                               [(10.0, 5.0), (20.0, 10.0)], 0.05)
    assert result["estimable"] is False
    assert result["control_value"] is None
    assert result["treatment_value"] is None
    assert result["absolute_effect"] is None
    assert result["ci_low"] is None and result["ci_high"] is None
    assert result["p_value"] is None
    assert result["significant"] is False
    assert result["control_denominator_sum"] == 0.0
    # 另一个臂也为 0 时同样不可估计
    both = pooled_ratio_test([(0.0, 0.0)], [(0.0, 0.0)], 0.05)
    assert both["estimable"] is False


def test_pooled_ratio_zero_variance_is_degenerate_not_crash():
    result = pooled_ratio_test([(10.0, 10.0), (20.0, 20.0)],
                               [(10.0, 10.0), (20.0, 20.0)], 0.05)
    assert result["standard_error"] == 0.0
    assert result["p_value"] == 1.0
    assert result["significant"] is False
    assert result["absolute_effect"] == 0.0
    assert result["ci_low"] == 0.0 and result["ci_high"] == 0.0


def test_pooled_ratio_alpha_drives_ci_and_significance():
    # 同一数据同一 p 值：alpha 只改变显著性判定与 CI 宽度，不改变点估计。
    control = [(100.0, 10.0), (112.0, 10.0), (88.0, 10.0), (100.0, 10.0)]
    treatment = [(108.0, 10.0), (118.0, 10.0), (100.0, 10.0), (106.0, 10.0)]
    loose = pooled_ratio_test(control, treatment, 0.05)
    strict = pooled_ratio_test(control, treatment, 0.001)
    assert loose["p_value"] == pytest.approx(strict["p_value"])
    assert loose["absolute_effect"] == pytest.approx(strict["absolute_effect"])
    # p ≈ 0.0027：0.05 显著、0.001 不显著
    assert loose["significant"] is True
    assert strict["significant"] is False
    width_loose = loose["ci_high"] - loose["ci_low"]
    width_strict = strict["ci_high"] - strict["ci_low"]
    assert width_strict > width_loose


# ---------------------------------------------------------------------------
# H3：pooled ratio 与 unit_mean 不是同一估计量
# ---------------------------------------------------------------------------

def test_pooled_ratio_diverges_from_unit_mean_under_heterogeneous_scale():
    # 对照组：小 unit (y=10,d=100 -> 0.1) 与巨型 unit (y=1900,d=10000 -> 0.19)
    control = [(10.0, 100.0), (1900.0, 10000.0)]
    # 实验组：小 unit 比率 0.16，巨型 unit 比率 0.10
    treatment = [(16.0, 100.0), (1000.0, 10000.0)]
    result = pooled_ratio_test(control, treatment, 0.05)
    pooled_control = 1910.0 / 10100.0
    pooled_treatment = 1016.0 / 10100.0
    assert result["control_value"] == pytest.approx(pooled_control)
    assert result["treatment_value"] == pytest.approx(pooled_treatment)
    # pooled ratio 效应为负（大 unit 主导）
    assert result["absolute_effect"] < 0.0
    # unit_mean 口径：每 unit 等权
    unit_mean_control = (0.1 + 0.19) / 2
    unit_mean_treatment = (0.16 + 0.10) / 2
    unit_mean_effect = unit_mean_treatment - unit_mean_control
    # 两口径效应数值不同，证明不能把 per-unit ratio 当 pooled ratio
    assert result["absolute_effect"] != pytest.approx(unit_mean_effect)
    # Welch on per-unit ratios 给出的是 unit_mean 口径，数值不同
    per_unit_control = [y / d for y, d in control]
    per_unit_treatment = [y / d for y, d in treatment]
    welch = welch_t_test(per_unit_control, per_unit_treatment, 0.05)
    assert welch["absolute_effect"] == pytest.approx(unit_mean_effect)
    assert welch["absolute_effect"] != pytest.approx(result["absolute_effect"])


def test_pooled_and_unit_mean_can_have_opposite_signs():
    # 显式构造方向相反的样例（分母规模悬殊，大 unit 在 pooled 中占主导）：
    # 对照：小 unit 比率 0.20（d=10），大 unit 比率 0.10（d=1000）
    control = [(2.0, 10.0), (100.0, 1000.0)]
    # 实验：小 unit 比率 0.30（d=10），大 unit 比率 0.09（d=1000）
    treatment = [(3.0, 10.0), (90.0, 1000.0)]
    result = pooled_ratio_test(control, treatment, 0.05)
    unit_mean_effect = ((0.30 + 0.09) / 2) - ((0.20 + 0.10) / 2)
    assert unit_mean_effect > 0.0           # unit_mean：典型 unit 变好
    assert result["absolute_effect"] < 0.0  # pooled：每单位分母产出变差
    # 手算 pooled：对照 102/1010，实验 93/1010
    assert result["control_value"] == pytest.approx(102.0 / 1010.0)
    assert result["treatment_value"] == pytest.approx(93.0 / 1010.0)


# ---------------------------------------------------------------------------
# config.py：双列声明校验
# ---------------------------------------------------------------------------

def test_ratio_config_loads_with_dual_columns(tmp_path):
    path = _ratio_config(tmp_path)
    config = load_config(path)
    assert config.primary.kind == "ratio"
    assert config.primary.column is None
    assert config.primary.numerator_column == "revenue"
    assert config.primary.denominator_column == "orders"
    assert config.guardrails[0].kind == "ratio"
    assert config.guardrails[0].numerator_column == "errors"
    assert config.guardrails[0].denominator_column == "requests"


def test_ratio_config_requires_numerator_and_denominator(tmp_path):
    path = _ratio_config(tmp_path)
    text = path.read_text(encoding="utf-8").replace(
        "    numerator_column: revenue\n", "", 1
    )
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match="numerator_column"):
        load_config(path)


def test_ratio_config_rejects_single_column(tmp_path):
    path = _ratio_config(tmp_path)
    text = path.read_text(encoding="utf-8").replace(
        "    numerator_column: revenue\n", "    column: revenue\n", 1
    ).replace("    denominator_column: orders\n", "", 1)
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match="numerator_column|column"):
        load_config(path)


def test_scalar_metric_rejects_dual_columns(tmp_path):
    # 直接从原始 proportion 配置出发，给单列指标塞双列声明
    text = CONFIG_PATH.read_text(encoding="utf-8")
    text = text.replace(
        "    column: activated\n",
        "    column: activated\n    numerator_column: revenue\n"
        "    denominator_column: orders\n", 1
    )
    target = tmp_path / "experiment.yaml"
    target.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match="ratio"):
        load_config(target)


def test_ratio_config_rejects_same_numerator_denominator(tmp_path):
    path = _ratio_config(tmp_path)
    text = path.read_text(encoding="utf-8").replace(
        "    denominator_column: orders\n",
        "    denominator_column: revenue\n", 1
    )
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match="同一列"):
        load_config(path)


# ---------------------------------------------------------------------------
# data.py：成对载入与分母契约
# ---------------------------------------------------------------------------

def _load(tmp_path, rows):
    config_path = _ratio_config(tmp_path)
    config = load_config(config_path)
    obs_path = _write_observations(tmp_path, rows)
    assignments = _write_assignments(tmp_path, len(rows))
    dataset = load_dataset(config, assignments, obs_path)
    return config, dataset


def test_data_loads_numerator_denominator_as_indivisible_pair(tmp_path):
    rows = [
        (1, "control", 100.0, 10.0, 0.0, 100.0),
        (2, "treatment", 120.0, 10.0, 1.0, 100.0),
        (3, "control", 200.0, 20.0, 0.0, 100.0),
        (4, "treatment", 220.0, 20.0, 2.0, 100.0),
    ]
    config, dataset = _load(tmp_path, rows)
    obs = dataset.observations
    assert "revenue_per_order" not in obs[0].values
    assert obs[0].pairs["revenue_per_order"] == (100.0, 10.0)
    # 同一行的两个数绑定在同一个样本对里
    assert all("revenue_per_order" in row.pairs for row in obs)


def test_data_accepts_zero_denominator_with_zero_numerator(tmp_path):
    rows = [
        (1, "control", 0.0, 0.0, 0.0, 100.0),
        (2, "treatment", 0.0, 0.0, 0.0, 100.0),
        (3, "control", 100.0, 10.0, 0.0, 100.0),
        (4, "treatment", 100.0, 10.0, 0.0, 100.0),
    ]
    _, dataset = _load(tmp_path, rows)
    zero_rows = [
        row for row in dataset.observations
        if row.pairs["revenue_per_order"][1] == 0.0
    ]
    assert len(zero_rows) == 2  # 仍计入输入质量计数（行数不丢）


def test_data_rejects_negative_denominator(tmp_path):
    rows = [
        (1, "control", 100.0, 10.0, 0.0, 100.0),
        (2, "treatment", 120.0, 10.0, 0.0, 100.0),
        (3, "control", 100.0, -1.0, 0.0, 100.0),
        (4, "treatment", 100.0, 10.0, 0.0, 100.0),
    ]
    config_path = _ratio_config(tmp_path)
    config = load_config(config_path)
    obs_path = _write_observations(tmp_path, rows)
    with pytest.raises(DataValidationError, match="分母"):
        load_observations(obs_path, config)


def test_data_rejects_zero_denominator_with_nonzero_numerator(tmp_path):
    rows = [
        (1, "control", 100.0, 10.0, 0.0, 100.0),
        (2, "treatment", 120.0, 10.0, 0.0, 100.0),
        (3, "control", 5.0, 0.0, 0.0, 100.0),
        (4, "treatment", 100.0, 10.0, 0.0, 100.0),
    ]
    config_path = _ratio_config(tmp_path)
    config = load_config(config_path)
    obs_path = _write_observations(tmp_path, rows)
    with pytest.raises(DataValidationError, match="分子必须也为 0"):
        load_observations(obs_path, config)


def test_data_rejects_negative_denominator_in_guardrail_column(tmp_path):
    rows = [
        (1, "control", 100.0, 10.0, 1.0, -5.0),
        (2, "treatment", 120.0, 10.0, 1.0, 100.0),
        (3, "control", 100.0, 10.0, 0.0, 100.0),
        (4, "treatment", 100.0, 10.0, 0.0, 100.0),
    ]
    config_path = _ratio_config(tmp_path)
    config = load_config(config_path)
    obs_path = _write_observations(tmp_path, rows)
    with pytest.raises(DataValidationError, match="分母"):
        load_observations(obs_path, config)


def test_missing_ratio_column_is_rejected_at_csv_layer(tmp_path):
    config_path = _ratio_config(tmp_path)
    config = load_config(config_path)
    rows = [
        (1, "control", 100.0, 10.0, 0.0, 100.0),
        (2, "treatment", 120.0, 10.0, 0.0, 100.0),
    ]
    obs_path = _write_observations(tmp_path, rows)
    lines = obs_path.read_text(encoding="utf-8").splitlines()
    # 删掉 denominator 列 orders
    header = lines[0].split(",")
    idx = header.index("orders")
    broken = tmp_path / "broken.csv"
    broken.write_text(
        "\n".join(",".join(v for j, v in enumerate(line.split(",")) if j != idx)
                  for line in lines),
        encoding="utf-8",
    )
    with pytest.raises(DataValidationError, match="orders"):
        load_observations(broken, config)


def test_negative_numerator_is_allowed(tmp_path):
    rows = [
        (1, "control", -50.0, 10.0, 0.0, 100.0),
        (2, "treatment", -20.0, 10.0, 0.0, 100.0),
        (3, "control", 100.0, 10.0, 0.0, 100.0),
        (4, "treatment", 100.0, 10.0, 0.0, 100.0),
    ]
    _, dataset = _load(tmp_path, rows)
    assert dataset.observations[0].pairs["revenue_per_order"] == (-50.0, 10.0)


def test_mixed_ratio_and_proportion_metrics_share_one_pipeline(tmp_path):
    # 主指标 ratio + 护栏 proportion，proportion 单列解析保持不变
    path = _ratio_config(tmp_path, primary_kind="ratio", guardrail_kind="mean")
    config = load_config(path)
    assert config.primary.kind == "ratio"
    assert config.guardrails[0].kind == "proportion"
    rows = []
    uid = 1
    for i in range(20):
        crashed = 1 if i % 7 == 0 else 0
        rows.append((uid, "control", 100.0, 10.0, float(crashed), 100.0))
        uid += 1
    for i in range(20):
        crashed = 1 if i % 11 == 0 else 0
        rows.append((uid, "treatment", 120.0, 10.0, float(crashed), 100.0))
        uid += 1
    obs_path = _write_observations(tmp_path, rows)
    assignments = _write_assignments(tmp_path, len(rows))
    dataset = load_dataset(config, assignments, obs_path)
    assert dataset.observations[0].values["crash_rate"] in (0.0, 1.0)
    assert dataset.observations[0].pairs["revenue_per_order"] == (100.0, 10.0)
    result = run_audit(
        config, dataset.assignments, dataset.observations,
        run_id="run-ratio-mixed-0001",
        generated_at="2026-03-16T00:00:00+00:00",
        input_digest="a" * 64, config_digest="b" * 64,
    )
    assert result["metrics"]["primary"]["kind"] == "ratio"
    assert result["metrics"]["guardrails"][0]["kind"] == "proportion"
    # proportion 护栏走旧两比例检验，结果字段保持旧口径
    guardrail = result["metrics"]["guardrails"][0]
    assert guardrail["estimable"] is True
    assert 0.0 <= guardrail["control_value"] <= 1.0


# ---------------------------------------------------------------------------
# metrics.compute_metric：MetricResult 字段与 decision 输入
# ---------------------------------------------------------------------------

def test_compute_ratio_metric_populates_pair_fields():
    spec = _ratio_spec()
    obs = _pair_observations(
        spec,
        control=[(100.0, 10.0), (300.0, 30.0), (0.0, 0.0)],
        treatment=[(220.0, 20.0), (360.0, 30.0)],
    )
    config = _mini_config(spec)
    result = compute_metric(config, obs, spec, "primary")
    assert result.kind == "ratio"
    assert result.estimable is True
    assert result.control_n == 3 and result.treatment_n == 2  # 随机化 unit 数
    assert result.control_numerator_sum == 400.0
    assert result.control_denominator_sum == 40.0
    assert result.treatment_numerator_sum == 580.0
    assert result.treatment_denominator_sum == 50.0
    assert result.control_zero_denominator_units == 1
    assert result.treatment_zero_denominator_units == 0
    assert result.control_effective_units == 2
    assert result.control_value == pytest.approx(10.0)
    assert result.treatment_value == pytest.approx(11.6)
    assert result.absolute_effect == pytest.approx(1.6)
    assert result.p_value is not None and result.ci_low is not None
    assert result.significant == (result.p_value < spec.alpha)
    # CI 与 p 同一方差：CI 不含 0 当且仅当显著
    assert result.significant == (result.ci_low > 0 or result.ci_high < 0)


def test_compute_ratio_metric_not_estimable_never_pretends_zero():
    spec = _ratio_spec()
    obs = _pair_observations(
        spec,
        control=[(0.0, 0.0), (0.0, 0.0)],   # 整臂总分母 0
        treatment=[(220.0, 20.0), (360.0, 30.0)],
    )
    config = _mini_config(spec)
    result = compute_metric(config, obs, spec, "primary")
    assert result.estimable is False
    assert result.control_value is None
    assert result.treatment_value is None
    assert result.absolute_effect is None
    assert result.relative_effect is None
    assert result.ci_low is None and result.ci_high is None
    assert result.p_value is None
    assert result.significant is False
    assert result.favorable is False
    assert result.meets_threshold is False
    assert result.control_n == 2  # unit 数仍被记录
    assert result.control_denominator_sum == 0.0


def test_ratio_guardrail_uses_absolute_effect_for_degradation():
    # lower_is_better：treatment 变差（错误率上升）应判定 degradation
    spec = _ratio_spec(key="error_per_request", label="每请求错误数",
                       orientation="lower_is_better", max_degradation=0.01,
                       numerator_column="errors", denominator_column="requests")
    obs = _pair_observations(
        spec,
        control=[(5.0, 1000.0)] * 20,
        treatment=[(8.0, 1000.0)] * 20,   # 0.005 -> 0.008，劣化 0.003
    )
    config = _mini_config(spec)
    result = compute_metric(config, obs, spec, "guardrail")
    assert result.guardrail_status in {"watch", "breach", "ok"}
    assert result.degradation == pytest.approx(0.003)
    assert result.absolute_effect == pytest.approx(0.003)


def test_ratio_guardrail_not_estimable_status():
    spec = _ratio_spec(key="error_per_request", label="每请求错误数",
                       orientation="lower_is_better", max_degradation=0.01,
                       numerator_column="errors", denominator_column="requests")
    obs = _pair_observations(
        spec,
        control=[(0.0, 0.0)] * 5,
        treatment=[(8.0, 1000.0)] * 5,
    )
    config = _mini_config(spec)
    result = compute_metric(config, obs, spec, "guardrail")
    assert result.guardrail_status == "not_estimable"
    assert result.degradation is None
    assert result.significant is False


def test_ratio_orientation_flips_favorable_without_changing_statistics():
    spec_high = _ratio_spec(orientation="higher_is_better")
    spec_low = _ratio_spec(orientation="lower_is_better")
    obs = _pair_observations(
        spec_high,
        control=[(100.0, 10.0)] * 10,
        treatment=[(120.0, 10.0)] * 10,
    )
    config = _mini_config(spec_high)
    high = compute_metric(config, obs, spec_high, "primary")
    low = compute_metric(config, obs, spec_low, "primary")
    assert high.absolute_effect == low.absolute_effect
    assert high.p_value == low.p_value
    assert high.favorable is True and low.favorable is False


def test_ratio_min_effect_gates_meets_threshold():
    spec = _ratio_spec(min_effect=2.0)
    obs = _pair_observations(
        spec,
        control=[(100.0, 10.0)] * 20,
        treatment=[(110.0, 10.0)] * 20,   # 效应 1.0 < min_effect 2.0
    )
    config = _mini_config(spec)
    result = compute_metric(config, obs, spec, "primary")
    assert result.significant is True
    assert result.favorable is True
    assert result.meets_threshold is False


# ---------------------------------------------------------------------------
# 端到端 audit：ratio CI/p 驱动 decision flags 与报表状态
# ---------------------------------------------------------------------------

def _ratio_audit(tmp_path, rows, *, primary_min_effect=0.0,
                 guardrail_max_degradation=0.05, primary_alpha=0.05):
    config_path = _ratio_config(
        tmp_path, primary_min_effect=primary_min_effect,
        guardrail_max_degradation=guardrail_max_degradation,
        primary_alpha=primary_alpha,
    )
    config = load_config(config_path)
    assignments = _write_assignments(tmp_path, len(rows))
    obs_path = _write_observations(tmp_path, rows)
    dataset = load_dataset(config, assignments, obs_path)
    result = run_audit(
        config, dataset.assignments, dataset.observations,
        run_id="run-ratio-test-0001",
        generated_at="2026-03-16T00:00:00+00:00",
        input_digest="x" * 64, config_digest="y" * 64,
    )
    return config, result


def _strong_signal_rows(n_per_arm: int = 60):
    rows = []
    uid = 1
    for i in range(n_per_arm):
        rows.append((uid, "control", 100.0 + (i % 3), 10.0, 2.0, 100.0))
        uid += 1
    for i in range(n_per_arm):
        rows.append((uid, "treatment", 130.0 + (i % 3), 10.0, 2.0, 100.0))
        uid += 1
    return rows


def test_audit_ratio_primary_ci_p_drive_ship_flags(tmp_path):
    rows = _strong_signal_rows()
    config, result = _ratio_audit(tmp_path, rows, primary_min_effect=0.5)
    primary = result["metrics"]["primary"]
    assert primary["kind"] == "ratio"
    assert primary["estimable"] is True
    assert primary["significant"] is True
    assert primary["favorable"] is True
    assert primary["ci_low"] > 0
    flags = result["flags"]
    assert flags["primary_positive_significant"] is True
    assert flags["primary_negative_significant"] is False
    assert flags["primary_estimable"] is True
    assert flags["primary_not_estimable"] is False
    # ratio 自己的 CI/p 进入决策，而不是保留字段摆设
    assert "metric.not_estimable" not in {
        f["code"] for f in result["findings"]
    }


def test_audit_ratio_guardrail_breach_drives_stop(tmp_path):
    # 主指标保持正向显著，护栏 ratio 明显劣化触发 breach
    rows = _strong_signal_rows()
    # 把实验组护栏错误率拉高：errors 从 2 -> 12（每 100 请求）
    rows = [
        (u, v, rev, od, (12.0 if v == "treatment" else err), req)
        for (u, v, rev, od, err, req) in rows
    ]
    config, result = _ratio_audit(tmp_path, rows, guardrail_max_degradation=0.02)
    guardrail = result["metrics"]["guardrails"][0]
    assert guardrail["kind"] == "ratio"
    assert guardrail["guardrail_status"] == "breach"
    assert result["flags"]["guardrail_breach"] is True
    assert result["decision"]["card"] == "stop"
    assert result["decision"]["matched_rule"] == "stop-negative-or-guardrail"
    finding_codes = {f["code"] for f in result["findings"]}
    assert "guardrail.breach.error_per_request" in finding_codes


def test_audit_not_estimable_primary_surfaces_in_checks_and_payload(tmp_path):
    rows = []
    uid = 1
    for _ in range(30):  # 对照臂全部零分母
        rows.append((uid, "control", 0.0, 0.0, 0.0, 100.0))
        uid += 1
    for _ in range(30):
        rows.append((uid, "treatment", 130.0, 10.0, 2.0, 100.0))
        uid += 1
    config, result = _ratio_audit(tmp_path, rows)
    primary = result["metrics"]["primary"]
    assert primary["estimable"] is False
    assert primary["absolute_effect"] is None
    assert primary["p_value"] is None
    assert result["flags"]["primary_not_estimable"] is True
    assert result["flags"]["primary_positive_significant"] is False
    assert result["flags"]["primary_inconclusive"] is True
    check_codes = {c["code"]: c for c in result["checks"]}
    assert check_codes["metric.primary"]["status"] == "warn"
    finding_codes = {f["code"] for f in result["findings"]}
    assert "metric.not_estimable" in finding_codes
    # 不可估计不能触发 ship
    assert result["decision"]["card"] != "ship"
    assert "不可估计" in result["headline"]


def test_audit_not_estimable_guardrail_flag_and_report_state(tmp_path):
    rows = _strong_signal_rows()
    # 对照臂 requests 全部置 0（errors 也必须为 0 才能通过输入校验）
    rows = [
        (u, v, rev, od, err, (0.0 if v == "control" else req))
        for (u, v, rev, od, err, req) in rows
    ]
    rows = [
        (u, v, rev, od, (0.0 if v == "control" and req == 0.0 else err), req)
        for (u, v, rev, od, err, req) in rows
    ]
    config, result = _ratio_audit(tmp_path, rows)
    guardrail = result["metrics"]["guardrails"][0]
    assert guardrail["estimable"] is False
    assert guardrail["guardrail_status"] == "not_estimable"
    assert result["flags"]["guardrail_not_estimable"] is True
    assert result["flags"]["guardrail_estimable"] is False
    check = next(c for c in result["checks"] if c["code"] == "guardrail.status")
    assert check["status"] == "warn"
    finding_codes = {f["code"] for f in result["findings"]}
    assert "guardrail.not_estimable.error_per_request" in finding_codes


def test_ratio_payload_round_trips_through_json_and_ledger(tmp_path):
    from app import ledger
    from app.reporting import write_artifacts
    import json

    rows = _strong_signal_rows()
    config, result = _ratio_audit(tmp_path, rows)
    out_dir = tmp_path / "out" / result["run"]["run_id"]
    write_artifacts(result, out_dir)
    audit_json = json.loads((out_dir / "audit.json").read_text(encoding="utf-8"))
    primary = audit_json["metrics"]["primary"]
    for key in ("control_numerator_sum", "control_denominator_sum",
                "treatment_numerator_sum", "treatment_denominator_sum",
                "control_zero_denominator_units", "treatment_zero_denominator_units",
                "control_effective_units", "treatment_effective_units", "estimable"):
        assert key in primary
    markdown = (out_dir / "report.md").read_text(encoding="utf-8")
    assert "每订单收入" in markdown
    html = (out_dir / "report.html").read_text(encoding="utf-8")
    assert "每订单收入" in html
    db = tmp_path / "ledger.sqlite3"
    ledger.save_run(db, result, out_dir.relative_to(tmp_path).as_posix())
    stored = ledger.list_runs(db)
    assert stored[0]["primary_effect"] == primary["absolute_effect"]
    import sqlite3
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    metric_row = con.execute(
        "SELECT * FROM metrics WHERE metric_key = 'revenue_per_order'"
    ).fetchone()
    assert metric_row["kind"] == "ratio"
    assert metric_row["estimable"] == 1
    assert metric_row["control_denominator_sum"] == primary["control_denominator_sum"]


def test_different_zero_denominator_shares_between_arms(tmp_path):
    # 对照臂 1/3 零分母，实验臂 1/2 零分母，两臂仍可估计且计数正确
    rows = []
    uid = 1
    for i in range(30):
        if i % 3 == 0:
            rows.append((uid, "control", 0.0, 0.0, 0.0, 100.0))
        else:
            rows.append((uid, "control", 100.0, 10.0, 2.0, 100.0))
        uid += 1
    for i in range(30):
        if i % 2 == 0:
            rows.append((uid, "treatment", 0.0, 0.0, 0.0, 100.0))
        else:
            rows.append((uid, "treatment", 120.0, 10.0, 2.0, 100.0))
        uid += 1
    config, result = _ratio_audit(tmp_path, rows)
    primary = result["metrics"]["primary"]
    assert primary["estimable"] is True
    assert primary["control_zero_denominator_units"] == 10
    assert primary["treatment_zero_denominator_units"] == 15
    assert primary["control_effective_units"] == 20
    assert primary["treatment_effective_units"] == 15
    assert primary["control_n"] == 30 and primary["treatment_n"] == 30


def test_ledger_migrates_legacy_not_null_schema(tmp_path):
    from app import ledger
    import sqlite3

    db = tmp_path / "legacy.sqlite3"
    # 手工建出旧版 metrics 表（关键列 NOT NULL、无 ratio 字段）
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE runs (run_id TEXT PRIMARY KEY, generated_at TEXT NOT NULL, "
        "experiment_id TEXT NOT NULL, experiment_name TEXT NOT NULL, decision TEXT NOT NULL, "
        "decision_title TEXT NOT NULL, matched_rule TEXT NOT NULL, decision_reason TEXT NOT NULL, "
        "headline TEXT NOT NULL, primary_metric_key TEXT NOT NULL, primary_effect REAL NOT NULL, "
        "primary_p_value REAL NOT NULL, finding_count INTEGER NOT NULL, input_digest TEXT NOT NULL, "
        "config_digest TEXT NOT NULL, artifact_dir TEXT NOT NULL, decision_json TEXT NOT NULL)"
    )
    con.execute(
        "CREATE TABLE metrics (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, "
        "role TEXT NOT NULL, metric_key TEXT NOT NULL, label TEXT NOT NULL, "
        "control_n INTEGER NOT NULL, treatment_n INTEGER NOT NULL, control_value REAL NOT NULL, "
        "treatment_value REAL NOT NULL, absolute_effect REAL NOT NULL, p_value REAL NOT NULL, "
        "significant INTEGER NOT NULL, favorable INTEGER NOT NULL, guardrail_status TEXT NOT NULL, "
        "details_json TEXT NOT NULL)"
    )
    con.execute(
        "CREATE TABLE checks (id INTEGER PRIMARY KEY, run_id TEXT, code TEXT, "
        "status TEXT, label TEXT, summary TEXT, details_json TEXT)"
    )
    con.execute(
        "CREATE TABLE findings (id INTEGER PRIMARY KEY, run_id TEXT, code TEXT, "
        "severity TEXT, category TEXT, title TEXT, detail TEXT, evidence_json TEXT)"
    )
    con.execute(
        "CREATE TABLE strata (id INTEGER PRIMARY KEY, run_id TEXT, field TEXT, "
        "value TEXT, control_n INTEGER, treatment_n INTEGER, absolute_effect REAL, "
        "p_value REAL, eligible INTEGER, reversal INTEGER, details_json TEXT)"
    )
    con.execute("CREATE INDEX idx_findings_run ON findings(run_id, severity)")
    con.execute("CREATE INDEX idx_strata_run ON strata(run_id)")
    con.execute("CREATE INDEX idx_runs_created ON runs(generated_at DESC)")
    con.execute("CREATE INDEX idx_metrics_run ON metrics(run_id)")
    con.execute(
        "INSERT INTO runs VALUES ('run-legacy-0001','2026-03-01T00:00:00+00:00','exp-x',"
        "'旧实验','iterate','继续迭代','r','理由','标题','m',0.05,0.5,0,'a','b','o','{}')"
    )
    con.execute(
        "INSERT INTO metrics (run_id, role, metric_key, label, control_n, treatment_n, "
        "control_value, treatment_value, absolute_effect, p_value, significant, favorable, "
        "guardrail_status, details_json) VALUES "
        "('run-legacy-0001','primary','m','旧指标',10,10,1.0,1.2,0.2,0.01,1,1,'n/a','{}')"
    )
    con.commit()
    con.close()

    ledger.init_db(db)  # 触发迁移
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    info = {r[1]: r for r in con.execute("PRAGMA table_info(metrics)")}
    assert "estimable" in info
    assert info["absolute_effect"][3] == 0  # 新表允许 NULL
    row = con.execute("SELECT * FROM metrics WHERE metric_key='m'").fetchone()
    assert row["kind"] == "mean" and row["estimable"] == 1
    assert row["absolute_effect"] == 0.2  # 旧数据保留
    # 迁移可重复执行不报错
    ledger.init_db(db)
    assert con.execute("SELECT COUNT(*) FROM metrics").fetchone()[0] == 1


def test_legacy_proportion_mean_results_remain_value_identical(audit_result):
    # 验收①：旧 proportion/mean 配置的结果字段逐值保持
    primary = audit_result["metrics"]["primary"]
    assert primary["kind"] == "proportion"
    assert primary["estimable"] is True
    assert primary["absolute_effect"] == 0.05
    guardrail = audit_result["metrics"]["guardrails"][0]
    assert guardrail["degradation"] == 0.0075
    assert guardrail["max_degradation"] == 0.01
    # 旧结果中新字段取默认值，不改变既有字段
    assert primary["control_numerator_sum"] == 0.0
    assert primary["control_denominator_sum"] == 0.0


# ---------------------------------------------------------------------------
# 小型 ExperimentConfig 构造（compute_metric 直测用）
# ---------------------------------------------------------------------------

def _mini_config(spec: MetricSpec):
    from app.models import (ExperimentConfig, SplitSpec, ExposureSpec,
                            StratificationSpec, DecisionSpec, Variant)
    return ExperimentConfig(
        schema_version=1,
        experiment_id="exp-x",
        name="测试实验",
        owner="qa",
        hypothesis="h",
        description="d",
        start=STAMP,
        end=STAMP,
        control="control",
        variants=(Variant("control", "对照"), Variant("treatment", "实验")),
        split=SplitSpec({"control": 0.5, "treatment": 0.5}, 0.1, 2),
        exposure=ExposureSpec(1, 0),
        mutexes=(),
        primary=spec,
        secondary=(),
        guardrails=(spec,),
        stratification=StratificationSpec(("platform",), 2, 12, 0.05, 0.75),
        decision=DecisionSpec((), {}, "review"),
    )
