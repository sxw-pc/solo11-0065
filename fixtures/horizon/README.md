# 结果成熟期（outcome_horizon）fixture

本目录演示 `MetricSpec.outcome_horizon`：每个 unit 自 `assigned_at` 起使用固定追踪窗，只有成熟观测进入效应与置信区间。

## 文件

| 文件 | 内容 |
| --- | --- |
| `experiment.yaml` | 主指标 horizon 7 天、会话深度 21 天、崩溃率 3 天 |
| `assignments.csv` | 600 条分配：400 条 3 月 1–5 日入组，200 条 3 月 19–20 日入组 |
| `observations.csv` | 520 条观测：老 cohort 的第 8 天成熟报告 + 新 cohort 的第 1 天早期快照 |

生成方式：固定种子 `20260301` 的本地脚本离线生成，运行期只读取 CSV/YAML。

## 预设信号

1. 老 cohort（400 unit，两臂各 200）已走完 7 日窗口：
   - 320 个 unit 有成熟观测（两臂各 160）；
   - 80 个 unit 成熟后仍无观测，暴露 attrition。
2. 新 cohort（200 unit，两臂各 100）只有第 1 天快照：
   - 在 `--as-of 2026-03-22` 下是 `pending`，不计为零、不写成永久缺数；
   - 在更晚的 run 时刻，这些快照会变成 `observed_before_horizon`。
3. 主指标成熟子样本效应为正：对照 27.5% → 实验 36.25%，绝对提升约 +8.75pp。
4. 三个指标 horizon 不同，同一 run 下的有效样本数不同。

## 运行

```bash
python -m app audit \
  --experiment fixtures/horizon/experiment.yaml \
  --assignments fixtures/horizon/assignments.csv \
  --observations fixtures/horizon/observations.csv \
  --as-of 2026-03-22T00:00:00+00:00
```

## 期望结果（`--as-of 2026-03-22`）

- 决策：`人工复核 / review`，命中 `review-measurement-pending`，`provisional=true`。
- 主指标：`mature=320`、`pending=200`、`mature_but_missing=80`，成熟覆盖 53.3%。
- 会话深度：horizon 21 天，全部 600 个 unit 仍在等待。
- 崩溃率：horizon 3 天，`mature=320`、`pending=200`、`mature_but_missing=80`，状态 `pending`（按临界处理）。
- finding：`measurement_pending`、`measurement_incomplete`、`metric.provisional`、`guardrail.pending.crash_rate`、`data.low_coverage`、`stratification.mixed_direction`。

## 为什么决策不是 ship

成熟子样本的效应为正，但在 `--as-of 2026-03-22` 时主指标仍有 200 个 pending unit；固定追踪窗要求每个 unit 都拥有相同的观察时长，才能把两臂的转化率解释为「分配后 7 天内是否转化」。因此当前效应只是 provisional，工具强制降级为人工复核，而不是用早成熟样本直接放量。
