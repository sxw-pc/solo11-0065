import math
import pytest
from app.stats import (
    chi_square_gof,
    normal_cdf,
    normal_ppf,
    student_t_two_sided_p,
    t_ppf,
    two_proportion_test,
    welch_t_test,
)
def test_normal_distribution_round_trip():
    assert normal_cdf(0.0) == pytest.approx(0.5)
    assert normal_ppf(0.975) == pytest.approx(1.959964, abs=1e-6)
    assert normal_cdf(normal_ppf(0.9)) == pytest.approx(0.9, abs=1e-9)
def test_two_proportion_test_known_result():
    result = two_proportion_test(200, 800, 240, 800, 0.05)
    assert result["absolute_effect"] == pytest.approx(0.05)
    assert result["p_value"] == pytest.approx(0.02511885, abs=1e-8)
    assert result["significant"] is True
    assert result["ci_low"] < 0.05 < result["ci_high"]
    assert result["ci_low"] == pytest.approx(0.00631104, abs=1e-8)
def test_two_proportion_zero_variance():
    result = two_proportion_test(0, 10, 0, 10, 0.05)
    assert result["p_value"] == 1.0
    assert result["significant"] is False
    assert result["absolute_effect"] == 0.0
def test_chi_square_gof_reference():
    result = chi_square_gof({"a": 100, "b": 200}, {"a": 0.5, "b": 0.5})
    assert result["chi_square"] == pytest.approx(33.3333333)
    assert result["degrees_of_freedom"] == 1
    assert result["p_value"] == pytest.approx(7.764e-09, rel=1e-3)
    assert result["residuals"]["a"] == pytest.approx(-math.sqrt(100 / 6))
def test_chi_square_rejects_empty_sample():
    with pytest.raises(ValueError):
        chi_square_gof({"a": 0}, {"a": 1.0})
def test_student_t_reference_values():
    assert student_t_two_sided_p(2.0, 10) == pytest.approx(0.073388, abs=1e-5)
    assert student_t_two_sided_p(0.0, 10) == 1.0
    assert t_ppf(0.975, 10) == pytest.approx(2.228139, abs=1e-5)
def test_welch_t_test_known_result():
    result = welch_t_test([1, 2, 3, 4, 5], [2, 3, 4, 5, 6], 0.05)
    assert result["absolute_effect"] == 1.0
    assert result["p_value"] == pytest.approx(0.3465935, abs=1e-6)
    assert result["ci_low"] < 1.0 < result["ci_high"]
    assert result["significant"] is False
def test_welch_requires_two_observations():
    with pytest.raises(ValueError):
        welch_t_test([1.0], [2.0, 3.0], 0.05)
def test_welch_identical_samples_is_not_significant():
    result = welch_t_test([2, 2, 2], [2, 2, 2], 0.05)
    assert result["p_value"] == 1.0
    assert result["absolute_effect"] == 0.0
def test_normal_ppf_rejects_out_of_range():
    with pytest.raises(ValueError):
        normal_ppf(1.0)
