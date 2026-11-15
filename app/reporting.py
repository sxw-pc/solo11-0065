"""JSON、Markdown 与自包含 HTML 报告渲染。HTML 不引用任何外部资源。"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .models import (
    CHECK_LABELS,
    DECISION_LABELS,
    MEASUREMENT_STATUS_LABELS,
    SEVERITY_LABELS,
)

TEMPLATE_DIR = Path(__file__).parent / "templates"
ARTIFACTS = ("audit.json", "decision_card.json", "report.md", "report.html")


def fmt_pct(value: float | None, digits: int = 2) -> str:
    return "—" if value is None else f"{value:.{digits}%}"


def fmt_num(value: float | None, digits: int = 4) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def fmt_p(value: float | None) -> str:
    if value is None:
        return "—"
    if value < 0.0001:
        return "<0.0001"
    return f"{value:.4f}"


def _environment() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters.update({
        "pct": fmt_pct,
        "num": fmt_num,
        "pval": fmt_p,
        "severity": lambda key: SEVERITY_LABELS.get(key, key),
        "check_status": lambda key: CHECK_LABELS.get(key, key),
        "decision_label": lambda key: DECISION_LABELS.get(key, key),
        "measurement_status": lambda key: MEASUREMENT_STATUS_LABELS.get(key, key),
    })
    return env


def decision_card_payload(result: dict[str, Any]) -> dict[str, Any]:
    primary = result["metrics"]["primary"]
    measurement = result.get("measurement") or {}
    return {
        "run_id": result["run"]["run_id"],
        "generated_at": result["run"]["generated_at"],
        "provisional": result["run"].get("provisional", False),
        "experiment": result["experiment"]["experiment"],
        "decision": result["decision"],
        "headline": result["headline"],
        "primary_metric": primary,
        "guardrails": result["metrics"]["guardrails"],
        "measurement": {
            "tracked": measurement.get("tracked", False),
            "primary": measurement.get("primary"),
            "check_status": measurement.get("check_status"),
        },
        "checks": [
            {"code": check["code"], "status": check["status"], "summary": check["summary"]}
            for check in result["checks"]
        ],
        "findings": result["findings"],
        "input_digest": result["run"]["input_digest"],
    }


def _cohort_status(raw: Any) -> str:
    label = MEASUREMENT_STATUS_LABELS.get(str(raw))
    return f"{label}（{raw}）" if label else str(raw)


def _measurement_lines(result: dict[str, Any]) -> list[str]:
    measurement = result.get("measurement") or {}
    metrics = measurement.get("metrics") or []
    tracked = [item for item in metrics if item.get("outcome_horizon") is not None]
    lines = ["## 结果成熟期", ""]
    if not tracked:
        lines.append("未配置 `outcome_horizon`，沿用旧行为：全部观测参与指标计算。")
        lines.append("")
        return lines
    lines.extend(
        [
            f"- 判定基准：run.generated_at = `{result['run'].get('measurement_as_of', '')}`",
            "- 口径：每个 unit 自 `assigned_at` 起的固定追踪窗，成熟时点含边界。",
            "",
            "| 指标 | 角色 | horizon | 分母 | 成熟 | 等待(pending) | 成熟缺数 | 早于窗口 | 成熟覆盖 | 状态 |",
            "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for item in tracked:
        lines.append(
            f"| {item.get('label')} | {item.get('role')} | "
            f"{item.get('outcome_horizon')}d | {item.get('denominator_units', 0)} | "
            f"{item.get('eligible_units', 0)} | {item.get('pending_units', 0)} | "
            f"{item.get('mature_but_missing_units', 0)} | "
            f"{item.get('observed_before_horizon_units', 0)} | "
            f"{fmt_pct(float(item.get('measurement_coverage', 0.0) or 0.0), 1)} | "
            f"{_cohort_status(item.get('status'))} |"
        )
    primary = measurement.get("primary") or {}
    cohorts = primary.get("pending_cohorts") or []
    if cohorts:
        lines.extend(["", "### 等待中的 cohort（主指标）", ""])
        lines.append("| 入组日 | 成熟时点 | units | 分臂 |")
        lines.append("| --- | --- | ---: | --- |")
        for cohort in cohorts:
            by_variant = ", ".join(
                f"{key}={value}" for key, value in sorted(
                    (cohort.get("by_variant") or {}).items()
                )
            )
            mature_at = cohort.get("mature_at")
            if cohort.get("mature_at_latest"):
                mature_at = f"{mature_at} → {cohort['mature_at_latest']}"
            lines.append(
                f"| {cohort.get('assigned_date')} | {mature_at} | "
                f"{cohort.get('units')} | {by_variant} |"
            )
    if result["run"].get("provisional"):
        lines.extend(
            [
                "",
                "> **provisional**：主指标仍有 pending cohort，"
                "当前效应只代表成熟子样本，不能作为最终上线结论。",
            ]
        )
    missing = [
        (item.get("label"), item.get("mature_but_missing_units"))
        for item in tracked if item.get("mature_but_missing_units")
    ]
    if missing:
        lines.extend(["", "### 成熟缺数（attrition）", ""])
        for label, count in missing:
            lines.append(f"- {label}：{count} 个 unit 已过成熟时点但仍无可用观测。")
    lines.append("")
    return lines


def render_markdown(result: dict[str, Any]) -> str:
    decision = result["decision"]
    experiment = result["experiment"]["experiment"]
    primary = result["metrics"]["primary"]
    lines = [
        f"# 分流验台审计报告：{experiment['name']}",
        "",
        f"- 实验 ID：`{experiment['id']}`",
        f"- run_id：`{result['run']['run_id']}`",
        f"- 生成时间：{result['run']['generated_at']}",
        f"- 输入摘要：`{result['run']['input_digest']}`",
        "",
        "## 决策卡",
        "",
        f"**{decision['title']}（{decision['card']}）**",
        "",
        decision["action"],
        "",
        f"- 命中规则：`{decision['matched_rule']}`",
        f"- 规则理由：{decision['reason']}",
        "",
        "## 核心指标",
        "",
        "| 指标 | 角色 | 对照 | 实验 | 绝对效应 | 95% 区间 | p 值 | 状态 |",
        "| --- | --- | ---: | ---: | ---: | --- | ---: | --- |",
    ]
    metric_rows = [primary, *result["metrics"]["secondary"], *result["metrics"]["guardrails"]]
    for item in metric_rows:
        computed = item.get("computed", True)
        state = "显著" if item["significant"] else "不显著"
        if not computed:
            state = "成熟观察不足"
        if item["role"] == "guardrail":
            state = {"breach": "越界", "watch": "临界", "ok": "正常",
                     "pending": "成熟度不足", "n/a": "—"}[
                item["guardrail_status"]
            ]
        control_value = fmt_num(item["control_value"]) if computed else "—"
        treatment_value = fmt_num(item["treatment_value"]) if computed else "—"
        effect = fmt_num(item["absolute_effect"]) if computed else "—"
        interval = (
            f"[{fmt_num(item['ci_low'])}, {fmt_num(item['ci_high'])}]"
            if computed else "—"
        )
        p_value = fmt_p(item["p_value"]) if computed else "—"
        lines.append(
            f"| {item['label']} | {item['role']} | {control_value} "
            f"| {treatment_value} | {effect} "
            f"| {interval} "
            f"| {p_value} | {state} |"
        )
    lines.extend(["", "## 自动检查", "", "| 检查 | 状态 | 摘要 |", "| --- | --- | --- |"])
    for check in result["checks"]:
        lines.append(
            f"| {check['label']} | {CHECK_LABELS.get(check['status'], check['status'])} "
            f"| {check['summary']} |"
        )
    lines.extend([""])
    lines.extend(_measurement_lines(result))
    lines.extend(["", "## Findings", ""])
    if not result["findings"]:
        lines.append("未发现需要关注的问题。")
    for finding in result["findings"]:
        lines.append(
            f"- **[{SEVERITY_LABELS.get(finding['severity'], finding['severity'])}] "
            f"{finding['title']}**（`{finding['code']}`）：{finding['detail']}"
        )
    lines.extend(["", "## 分层复核", ""])
    for field in result["stratification"]["fields"]:
        lines.append(f"### {field['field']}")
        lines.append("")
        lines.append("| 分层 | 对照 n | 实验 n | 效应 | p 值 | 可评估 | 反转 |")
        lines.append("| --- | ---: | ---: | ---: | ---: | --- | --- |")
        for group in field["groups"]:
            lines.append(
                f"| {group['value']} | {group['control_n']} | {group['treatment_n']} "
                f"| {fmt_num(group['absolute_effect'])} | {fmt_p(group['p_value'])} "
                f"| {'是' if group['eligible'] else '否'} "
                f"| {'是' if group['reversal'] else '否'} |"
            )
        lines.append("")
    lines.extend(
        [
            "## 可复现性",
            "",
            f"- 输入 SHA-256：`{result['run']['input_digest']}`",
            f"- 配置 SHA-256：`{result['run']['config_digest']}`",
            "- 统计计算仅使用 Python 标准库；随机过程已固定或不存在。",
            "- 除 run_id 与生成时间外，相同输入与配置产生相同审计内容。",
            "",
        ]
    )
    return "\n".join(lines)


def render_html(result: dict[str, Any]) -> str:
    template = _environment().get_template("report.html")
    rendered = template.render(
        result=result,
        experiment=result["experiment"]["experiment"],
        decision=result["decision"],
        primary=result["metrics"]["primary"],
        generated_label=datetime.fromisoformat(result["run"]["generated_at"]).strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
        CHECK_LABELS=CHECK_LABELS,
        SEVERITY_LABELS=SEVERITY_LABELS,
        MEASUREMENT_STATUS_LABELS=MEASUREMENT_STATUS_LABELS,
    )
    return _compact_html(rendered)


def _compact_html(html: str) -> str:
    """压缩标签间空白，使生成的单文件报告保持紧凑且仍为合法 HTML。"""
    compact = re.sub(r">\s+<", "><", html).strip()
    return compact + "\n"


def _write_text(path: Path, text: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    temporary.replace(path)


def write_artifacts(result: dict[str, Any], output_dir: Path) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    payloads = {
        "audit.json": json.dumps(
            result, ensure_ascii=False, indent=2, sort_keys=True
        ) + "\n",
        "decision_card.json": json.dumps(
            decision_card_payload(result), ensure_ascii=False, indent=2, sort_keys=True
        ) + "\n",
        "report.md": render_markdown(result),
        "report.html": render_html(result),
    }
    written: dict[str, Path] = {}
    for name in ARTIFACTS:
        target = output_dir / name
        _write_text(target, payloads[name])
        written[name] = target
    return written
