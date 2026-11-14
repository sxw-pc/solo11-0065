"""仅使用标准库的统计推断：比例 z 检验、Welch t 检验与卡方拟合优度。"""

from __future__ import annotations

import math
from typing import Iterable

EPS = 3.0e-14
FPMIN = 1.0e-300
MAX_ITER = 240


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def normal_sf(value: float) -> float:
    return 0.5 * math.erfc(value / math.sqrt(2.0))


def normal_ppf(probability: float) -> float:
    if not 0.0 < probability < 1.0:
        raise ValueError("probability 必须在 (0, 1) 内")
    low, high = -12.0, 12.0
    for _ in range(90):
        middle = (low + high) / 2.0
        if normal_cdf(middle) < probability:
            low = middle
        else:
            high = middle
    return (low + high) / 2.0


def _betacf(a: float, b: float, x: float) -> float:
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < FPMIN:
        d = FPMIN
    d = 1.0 / d
    h = d
    for m in range(1, MAX_ITER + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < EPS:
            break
    return h


def regularized_beta(a: float, b: float, x: float) -> float:
    if not (a > 0.0 and b > 0.0):
        raise ValueError("a,b 必须为正")
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    log_front = (
        math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
        + a * math.log(x) + b * math.log1p(-x)
    )
    front = math.exp(log_front)
    if x < (a + 1.0) / (a + b + 2.0):
        return clamp(front * _betacf(a, b, x) / a)
    return clamp(1.0 - front * _betacf(b, a, 1.0 - x) / b)


def gamma_q(a: float, x: float) -> float:
    """正则化上不完全伽马函数 Q(a,x)。"""
    if a <= 0.0 or x < 0.0:
        raise ValueError("a 必须为正且 x 非负")
    if x == 0.0:
        return 1.0
    log_front = a * math.log(x) - x - math.lgamma(a)
    if x < a + 1.0:
        term = 1.0 / a
        total = term
        ap = a
        for _ in range(MAX_ITER):
            ap += 1.0
            term *= x / ap
            total += term
            if abs(term) < abs(total) * EPS:
                break
        return clamp(1.0 - total * math.exp(log_front))
    b = x + 1.0 - a
    c = 1.0 / FPMIN
    d = 1.0 / b
    h = d
    for i in range(1, MAX_ITER + 1):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        if abs(d) < FPMIN:
            d = FPMIN
        c = b + an / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < EPS:
            break
    return clamp(math.exp(log_front) * h)


def student_t_two_sided_p(t_value: float, degrees: float) -> float:
    if degrees <= 0.0:
        raise ValueError("degrees 必须为正")
    x = degrees / (degrees + t_value * t_value)
    return clamp(regularized_beta(degrees / 2.0, 0.5, x))


def t_ppf(probability: float, degrees: float) -> float:
    if not 0.0 < probability < 1.0:
        raise ValueError("probability 必须在 (0, 1) 内")
    low, high = 0.0, 200.0
    for _ in range(90):
        middle = (low + high) / 2.0
        cdf = 1.0 - student_t_two_sided_p(middle, degrees) / 2.0
        if cdf < probability:
            low = middle
        else:
            high = middle
    return (low + high) / 2.0


def two_proportion_test(
    control_successes: int,
    control_n: int,
    treatment_successes: int,
    treatment_n: int,
    alpha: float,
) -> dict[str, float | bool]:
    if control_n <= 0 or treatment_n <= 0:
        raise ValueError("样本量必须为正")
    control = control_successes / control_n
    treatment = treatment_successes / treatment_n
    difference = treatment - control
    pooled = (control_successes + treatment_successes) / (control_n + treatment_n)
    standard_error = math.sqrt(pooled * (1.0 - pooled) * (1.0 / control_n + 1.0 / treatment_n))
    if standard_error == 0.0:
        p_value = 1.0 if difference == 0.0 else 0.0
    else:
        p_value = 2.0 * normal_sf(abs(difference / standard_error))
    ci_se = math.sqrt(
        control * (1.0 - control) / control_n
        + treatment * (1.0 - treatment) / treatment_n
    )
    z_critical = normal_ppf(1.0 - alpha / 2.0)
    return {
        "control_value": control,
        "treatment_value": treatment,
        "absolute_effect": difference,
        "ci_low": difference - z_critical * ci_se,
        "ci_high": difference + z_critical * ci_se,
        "p_value": clamp(p_value),
        "significant": bool(clamp(p_value) < alpha),
    }


def _mean(values: Iterable[float]) -> float:
    items = list(values)
    if not items:
        raise ValueError("mean 需要非空样本")
    return sum(items) / len(items)


def _sample_variance(values: Iterable[float], mean: float) -> float:
    items = list(values)
    if len(items) < 2:
        return 0.0
    return sum((item - mean) ** 2 for item in items) / (len(items) - 1)


def welch_t_test(
    control_values: Iterable[float],
    treatment_values: Iterable[float],
    alpha: float,
) -> dict[str, float | bool]:
    control_items = list(control_values)
    treatment_items = list(treatment_values)
    if len(control_items) < 2 or len(treatment_items) < 2:
        raise ValueError("Welch 检验每组至少需要 2 个观测")
    control_mean = _mean(control_items)
    treatment_mean = _mean(treatment_items)
    difference = treatment_mean - control_mean
    control_var = _sample_variance(control_items, control_mean)
    treatment_var = _sample_variance(treatment_items, treatment_mean)
    var_c = control_var / len(control_items)
    var_t = treatment_var / len(treatment_items)
    standard_error = math.sqrt(var_c + var_t)
    if standard_error == 0.0:
        return {
            "control_value": control_mean,
            "treatment_value": treatment_mean,
            "absolute_effect": difference,
            "ci_low": difference,
            "ci_high": difference,
            "p_value": 1.0 if difference == 0.0 else 0.0,
            "significant": difference != 0.0,
        }
    df_denominator = var_c * var_c / (len(control_items) - 1) + (
        var_t * var_t / (len(treatment_items) - 1)
    )
    degrees = (var_c + var_t) ** 2 / df_denominator if df_denominator else 1.0
    t_value = difference / standard_error
    p_value = student_t_two_sided_p(t_value, degrees)
    critical = t_ppf(1.0 - alpha / 2.0, degrees)
    return {
        "control_value": control_mean,
        "treatment_value": treatment_mean,
        "absolute_effect": difference,
        "ci_low": difference - critical * standard_error,
        "ci_high": difference + critical * standard_error,
        "p_value": p_value,
        "significant": p_value < alpha,
    }


def chi_square_gof(
    observed: dict[str, int], expected_proportions: dict[str, float]
) -> dict[str, object]:
    total = sum(observed.values())
    if total <= 0:
        raise ValueError("卡方检验需要非空样本")
    chi_square = 0.0
    residuals: dict[str, float] = {}
    for key, probability in expected_proportions.items():
        expected = total * probability
        if expected <= 0.0:
            raise ValueError("期望频数必须为正")
        count = observed.get(key, 0)
        chi_square += (count - expected) ** 2 / expected
        residuals[key] = (count - expected) / math.sqrt(expected)
    degrees = max(1, len(expected_proportions) - 1)
    p_value = gamma_q(degrees / 2.0, chi_square / 2.0)
    return {
        "chi_square": chi_square,
        "degrees_of_freedom": degrees,
        "p_value": clamp(p_value),
        "residuals": residuals,
        "total": total,
    }
