"""JSON、Markdown 与自包含 HTML 报告渲染。HTML 不引用任何外部资源。"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .models import CHECK_LABELS, DECISION_LABELS, SEVERITY_LABELS

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
    })
    return env


def decision_card_payload(result: dict[str, Any]) -> dict[str, Any]:
    primary = result["metrics"]["primary"]
    payload = {
        "run_id": result["run"]["run_id"],
        "generated_at": result["run"]["generated_at"],
        "experiment": result["experiment"]["experiment"],
        "decision": result["decision"],
        "headline": result["headline"],
        "primary_metric": primary,
        "guardrails": result["metrics"]["guardrails"],
        "checks": [
            {"code": check["code"], "status": check["status"], "summary": check["summary"]}
            for check in result["checks"]
        ],
        "findings": result["findings"],
        "input_digest": result["run"]["input_digest"],
    }
    if primary.get("adjustment") is not None:
        payload["primary_statistic"] = primary["adjustment"]["decision_statistic"]
    return payload


def _adjustment_lines(primary: dict[str, Any]) -> list[str]:
    adjustment = primary.get("adjustment")
    if adjustment is None:
        return []
    raw = adjustment["raw"]
    adjusted = adjustment.get("adjusted")
    status_label = "已调整" if adjustment["status"] == "adjusted" else "未估计"
    statistic_label = "调整后" if adjustment["decision_statistic"] == "adjusted" else "原始（全量）"
    lines = [
        "",
        "## 实验前协变量调整（CUPED）",
        "",
        f"- 基线文件：`{adjustment['baseline_file']}`（列 `{adjustment['baseline_column']}`）",
        f"- 调整状态：**{status_label}**（原因：`{adjustment['reason']}`）",
        f"- 决策统计：**{statistic_label}**；theta 来源：`{adjustment['theta_source']}`",
        f"- 覆盖：配对 {adjustment['paired_units']}/{adjustment['observed_units']} 个观测单元 "
        f"（{fmt_pct(adjustment['coverage'])}，门槛 {fmt_pct(adjustment['min_coverage'], 0)}），"
        f"缺失 {adjustment['missing_units']} 个",
        f"- 覆盖分臂：{'，'.join(f'{key} {fmt_pct(value, 1)}' for key, value in adjustment['coverage_by_variant'].items())}",
        f"- theta：{fmt_num(adjustment['theta'], 6)}；参照均值：{fmt_num(adjustment['reference_mean'])}",
        "",
        "| 口径 | 对照 | 实验 | 绝对效应 | 95% 区间 | p 值 | 显著 |",
        "| --- | ---: | ---: | ---: | --- | ---: | --- |",
    ]
    for label, payload in (("原始 raw", raw), ("调整后 adjusted", adjusted)):
        if payload is None:
            lines.append(f"| {label} | — | — | — | 不可估计 | — | — |")
            continue
        lines.append(
            f"| {label} | {fmt_num(payload['control_value'])} "
            f"| {fmt_num(payload['treatment_value'])} "
            f"| {fmt_num(payload['absolute_effect'])} "
            f"| [{fmt_num(payload['ci_low'])}, {fmt_num(payload['ci_high'])}] "
            f"| {fmt_p(payload['p_value'])} "
            f"| {'是' if payload['significant'] else '否'} |"
        )
    lines.extend([
        "",
        "> 原始与调整后的区间、p 值互为独立口径，不得交叉引用。"
        "主指标 finding 与决策仅使用上表中的“决策统计”一行。",
    ])
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
        state = "显著" if item["significant"] else "不显著"
        if item["role"] == "guardrail":
            state = {"breach": "越界", "watch": "临界", "ok": "正常", "n/a": "—"}[
                item["guardrail_status"]
            ]
        lines.append(
            f"| {item['label']} | {item['role']} | {fmt_num(item['control_value'])} "
            f"| {fmt_num(item['treatment_value'])} | {fmt_num(item['absolute_effect'])} "
            f"| [{fmt_num(item['ci_low'])}, {fmt_num(item['ci_high'])}] "
            f"| {fmt_p(item['p_value'])} | {state} |"
        )
    lines.extend(["", "## 自动检查", "", "| 检查 | 状态 | 摘要 |", "| --- | --- | --- |"])
    for check in result["checks"]:
        lines.append(
            f"| {check['label']} | {CHECK_LABELS.get(check['status'], check['status'])} "
            f"| {check['summary']} |"
        )
    lines.extend(_adjustment_lines(primary))
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
