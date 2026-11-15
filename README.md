# 分流验台（Split Audit Bench）

面向小型产品与数据团队的**本地、离线、配置驱动** A/B 实验分流健康审计工具。

它回答一个问题：

> 这次实验的分流关系与指标证据，是否足以支持一个可复核的 `ship / iterate / stop / review` 决策？

分流验台读取仓库内的实验定义 YAML、分配 CSV 和观测 CSV，完成分流比例、重复曝光、互斥冲突、输入一致性、主指标效应、护栏劣化、分层方向反转等检查，再按声明式规则生成决策卡、JSON / Markdown / 自包含 HTML 报告，并写入可查询的 SQLite 台账。

---

## 目录

- [边界与定位](#边界与定位)
- [快速开始](#快速开始)
- [一条命令做什么](#一条命令做什么)
- [项目结构](#项目结构)
- [输入契约](#输入契约)
- [审计项与方法](#审计项与方法)
- [声明式决策规则](#声明式决策规则)
- [输出与台账](#输出与台账)
- [本地 Web](#本地-web)
- [示例 fixture](#示例-fixture)
- [确定性与复现](#确定性与复现)
- [安全与离线边界](#安全与离线边界)
- [测试](#测试)
- [已知边界](#已知边界)

---

## 边界与定位

### 它做什么

- 对一个已定义的双变体 A/B 实验做离线分流健康审计。
- 检查分流比例是否偏离声明期望，重复分配 / 重复曝光是否超限。
- 检查同一 unit 是否同时进入声明互斥的实验。
- 校验观测与分配是否一致、是否落在实验窗口内。
- 计算比例类与均值类指标，输出绝对效应、相对效应、置信区间与 p 值。
- 计算护栏指标劣化幅度，输出 `ok / watch / breach`。
- 按配置字段做分层复核，特别检查总体为正但某分层显著为负的方向反转。
- 根据优先级和条件 flag 执行声明式规则，产生可解释的决策卡与规则评估轨迹。
- 生成机器可读 JSON、可审阅 Markdown、自包含 HTML，并写入 SQLite。
- 通过只监听 `127.0.0.1` 的本地 Web 展示台账与产物。

### 它不做什么

- 不是通用 BI、看板、SQL 查询器或数据湖。
- 不校准指标口径，不处理指标定义漂移或报表对账。
- 不做数据采集、埋点 SDK、爬虫、RPA、脱敏、抽样执行、闸门准入或表关联发现。
- 不是真实实验平台，不接收真实流量，不自动上线或回滚。
- 不做用户级隐私分析，不连接任何外部 API、CDN、数据库、消息队列或实验平台。
- 不提供任意文件上传，不接受运行期外部数据源。
- 不引入 numpy、pandas、scipy、matplotlib 或任何需要编译的统计依赖。

本项目是 Coding Agent 评测用的可运行素材源项目，所有输入都是合成 fixture。

---

## 快速开始

要求 Python 3.10+。项目在 Python 3.13 上验证，代码保持 3.10 兼容写法。

### 安装

在线环境下：

```bash
pip install -r requirements.txt
```

完全离线环境（已准备本地 wheel 目录）时：

```bash
pip install --no-index --find-links ./wheelhouse -r requirements.txt
```

依赖只使用常见纯 Python 包，版本范围：

| 依赖 | 范围 | 用途 |
| --- | --- | --- |
| Flask | `>=3.0,<4` | 本地 Web |
| PyYAML | `>=6.0,<7` | 实验定义 |
| pytest | `>=8.0,<9` | 测试 |

统计计算只用 Python 标准库（`math`、`statistics` 思路的自实现函数、`csv`、`sqlite3`、`hashlib` 等）。

### 三条核心命令

```bash
# 1. 完整演示：审计内置 fixture，写 out/ 与 SQLite 台账
python -m app demo --out out --db data/ledger.sqlite3

# 2. 运行测试
pytest -q

# 3. 启动本地 Web（仅 127.0.0.1:5000）
python -m app serve --port 5000
```

浏览器打开 `http://127.0.0.1:5000`。

### 审计其他本地输入

```bash
python -m app audit \
  --experiment fixtures/experiment.yaml \
  --assignments fixtures/assignments.csv \
  --observations fixtures/observations.csv \
  --out out \
  --db data/ledger.sqlite3
```

所有输入、输出和数据库路径都被约束在项目根目录内；符号链接逃逸也会被拒绝。默认根目录是当前工作目录，可用全局参数覆盖：

```bash
python -m app --root /path/to/project demo
```

`--root` 是全局参数，应放在子命令之前。

### 查询台账

```bash
python -m app ledger --db data/ledger.sqlite3
python -m app ledger --db data/ledger.sqlite3 --decision iterate --limit 10
```

---

## 一条命令做什么

`python -m app demo --out out --db data/ledger.sqlite3` 会：

1. 从 `fixtures/` 读取实验定义、分配与观测，并校验契约。
2. 计算输入 SHA-256 摘要与配置摘要。
3. 生成运行 ID（含 UTC 时间戳与输入摘要前 8 位）和生成时间。
4. 执行全部检查、指标统计、分层复核与规则决策。
5. 写出：
   - `out/<run_id>/audit.json`
   - `out/<run_id>/decision_card.json`
   - `out/<run_id>/report.md`
   - `out/<run_id>/report.html`
6. 将运行、检查、finding、指标与分层写入 SQLite。
7. 在终端打印决策、命中规则、run_id 和产物路径。

---

## 项目结构

```text
.
├── app/
│   ├── __init__.py
│   ├── __main__.py          # python -m app
│   ├── cli.py               # demo / audit / serve / ledger
│   ├── config.py            # YAML 契约校验
│   ├── data.py              # CSV 读取与输入校验
│   ├── safety.py            # 根目录约束、路径逃逸检查、回环地址
│   ├── models.py            # 数据类
│   ├── stats.py             # 标准库统计实现
│   ├── checks.py            # 比例、曝光、互斥、数据一致性
│   ├── metrics.py           # 指标效应、区间、护栏
│   ├── stratify.py          # 分层与方向反转
│   ├── evaluate.py          # 单项检查、flags、序列化辅助
│   ├── decision.py          # 声明式规则引擎
│   ├── audit.py             # 审计编排
│   ├── reporting.py         # JSON / Markdown / 自包含 HTML
│   ├── ledger.py            # SQLite 台账
│   ├── web.py               # 只读本地 Web
│   ├── templates/           # 台账、详情、独立报告
│   └── static/style.css     # Web 样式
├── fixtures/
│   ├── experiment.yaml
│   ├── assignments.csv
│   ├── observations.csv
│   └── README.md
├── tests/
│   ├── conftest.py
│   ├── test_stats.py
│   ├── test_config_data.py
│   ├── test_audit_checks.py
│   ├── test_decision_report_ledger.py
│   └── test_web_cli.py
├── data/                    # 默认 SQLite 台账目录
├── out/                     # 默认报告产物目录
├── requirements.txt
├── pyproject.toml
└── README.md
```

---

## 输入契约

### 实验定义 YAML

完整例子见 `fixtures/experiment.yaml`。顶层结构：

```yaml
schema_version: 1
experiment:
  id: exp-example-2026        # 小写 slug
  name: 实验名称
  owner: owning-team
  hypothesis: 可复核的假设
  description: 数据来源与边界
  start: "2026-03-01T00:00:00+00:00"
  end: "2026-03-15T00:00:00+00:00"
control: control
variants:
  control: { label: 对照组 }
  treatment: { label: 实验组 }
split:
  expected: { control: 0.5, treatment: 0.5 }
  tolerance: 0.03
  min_assignments: 1200
# 可选：按时间分阶段的 rollout 计划
# phases:
#   - key: ramp-90-10
#     start: "2026-03-01T00:00:00+00:00"
#     end: "2026-03-08T00:00:00+00:00"
#     expected: { control: 0.9, treatment: 0.1 }
#     min_assignments: 500
#   - key: even-50-50
#     start: "2026-03-08T00:00:00+00:00"
#     end: "2026-03-15T00:00:00+00:00"
#     expected: { control: 0.5, treatment: 0.5 }
#     min_assignments: 500
exposure:
  max_per_unit: 1
  tolerance: 0
mutual_exclusions:
  - experiment_id: exp-other-2026
    label: 其他实验
metrics:
  primary:
    key: activation_rate
    column: activated
    label: 激活率
    kind: proportion
    orientation: higher_is_better
    min_effect: 0.02
    alpha: 0.05
  secondary: []
  guardrails:
    - key: crash_rate
      column: crashed
      label: 崩溃率
      kind: proportion
      orientation: lower_is_better
      max_degradation: 0.01
      alpha: 0.05
stratification:
  fields: [platform, region]
  min_group_size: 80
  max_groups: 12
  alpha: 0.05
  min_favorable_share: 0.75
decision:
  # 见“声明式决策规则”
  ...
```

校验规则包括：

- `schema_version` 必须为 `1`。
- 只支持一个对照 + 一个实验变体，变体键必须是 slug。
- `split.expected` 必须覆盖全部变体、每项在 `[0, 1]`、总和为 1。
- `split.tolerance` 在 `[0, 0.5]`，`min_assignments >= 2`。
- `split.phases` 可选；声明后按时间逐段核验，具体契约见“分阶段流量计划”。
- 指标 `kind` 为 `proportion` 或 `mean`；`orientation` 为 `higher_is_better` 或 `lower_is_better`。
- 指标 key 唯一；至少声明一个护栏。
- 护栏的 `max_degradation` 对比例指标不能大于 1。
- 分层字段非空且不重复；`max_groups >= 1`。
- 决策卡必须包含 `ship / iterate / stop / review`；规则 id 唯一，只能使用 `flag / all / any / none` 条件。
- 条件嵌套深度有限制；拒绝任意表达式或代码执行。

### 分配 CSV

必需列：

| 列 | 说明 |
| --- | --- |
| `unit_id` | 合成标识，必须形如 `unit-0001` |
| `experiment_id` | 实验 slug |
| `variant` | 变体键；本实验的未知变体直接报错 |
| `assigned_at` | ISO-8601 时间；无时区按 UTC |
| `platform` | 分层字段示例，按配置字段要求 |
| `region` | 分层字段示例，按配置字段要求 |

允许同一 `unit_id` 出现在其他实验的分配行中，用于互斥冲突检查。

### 观测 CSV

必需列：

| 列 | 说明 |
| --- | --- |
| `unit_id` | 同分配文件，必须能关联 |
| `experiment_id` | 实验 slug |
| `variant` | 变体键 |
| `observed_at` | ISO-8601 时间 |
| 指标列 | 每个指标配置中的 `column`，例如 `activated`、`session_depth`、`crashed` |

数值规则：

- 比例指标只接受 `0` 或 `1`。
- 均值指标只接受有限浮点数。
- 缺失列、空文件、非法时间、非法 unit 标识、未知变体都会中断读取，并给出文件名与行号。
- 观测缺少对应分配、变体不一致、时间越界会进入审计 finding，而不是被静默忽略。
- 重复分配与重复曝光由配置 `max_per_unit` / `tolerance` 判定。

---

## 审计项与方法

### 检查矩阵

| 检查 | 问题 | 触发方式 |
| --- | --- | --- |
| `split.balance` | 分流比例失衡 | 比例偏差超过 `tolerance`，或卡方 p 值异常；分阶段配置下逐段判定 |
| `exposure.duplicate` | 重复分配 / 重复曝光 | 单 unit 次数超过 `max_per_unit` 且额外次数超过 `tolerance` |
| `mutex.conflict` | 互斥实验冲突 | 本实验 unit 也出现在声明的互斥实验分配中 |
| `data.quality` | 数据一致性 | 未分配观测、变体不一致、时间越界；覆盖率低为告警 |
| `sample.size` | 每组样本不足 | 每组唯一观测单元少于 `ceil(min_assignments / 2)` |
| `metric.primary` | 主指标证据 | 负向显著为 fail；稳健通过为 pass；其他为 warn |
| `guardrail.status` | 护栏指标 | 任一越界为 fail；临界为 warn |
| `stratification.reversal` | 分层稳健性 | 存在显著方向反转或同向分层占比不足 |

检查状态只有 `pass / warn / fail`，按 `fail → warn → pass` 排序展示。

### 分流比例

- 用唯一分配单元计数，避免重复分配重复计权。
- 计算每个变体的观测占比与期望占比的绝对偏差。
- 用卡方拟合优度检验（`k-1` 自由度）给出总体 p 值。
- 任一变体偏差超过配置容差即失败；未超过容差但卡方 p 值低于 `0.05` 时告警。

### 分阶段流量计划

A/B 实验常见的 ramp（例如前半程 control/treatment `90/10`、后半程 `50/50`）
无法用单一全程比例表达。配置 `split.phases` 后，审计改为逐段核验：

- 每段声明 `key`、`start`、`end`、`expected` 与 `min_assignments`。
- 边界是 UTC 半开区间 `[start, end)`；相邻阶段必须首尾相接，不得重叠或留空档，且整体覆盖实验窗口。
- 每个阶段的 `expected` 必须覆盖全部变体且和为 1；允许 `0` 比例，用于 `0%` 起量的阶段。
- 用 `Assignment.assigned_at` 把唯一 unit 归入确切阶段，`experiment.end` 的等点归最后一段。
- 逐段独立计算 counts、实际比例、偏差、卡方与状态；整体 `split.balance` 由最差阶段决定：
  任一阶段 `fail` → 整体 `fail`；否则任一阶段样本不足或 `warn` → 整体 `warn`；全部 `pass` 才 `pass`。
- 某段唯一 unit 少于该段 `min_assignments` 时，该段为 `unverified`，不据此判定真实失衡，
  但会以 `warn` 与 `split.phase_insufficient` finding 进入决策。
- 若某段实际比例在容差内但卡方 p 值低于 `0.05`，该段为 `warn`（`split.phase_imbalance`，medium）。
- 同一 unit 在一个阶段内出现多条分配只计一次；同 variant 的重复由 `exposure.duplicate` 处理。
- 同一 unit 的分配跨阶段重复时，单独产生 `split.phase_reassignment`（high）finding，
  该 unit 从所有阶段计数中剔除，整体分流结论直接 `fail`，避免阶段检查因重复计数伪装成通过。
- 不在任何阶段内的本实验 unit 不计入阶段比例，数量与样本记录在 `outside_schedule_*`。

选择逐段判定而非“按各阶段样本量合并 likelihood”，是因为审计对象是每段已声明的
traffic allocation：合并检验会在两段偏差方向相反时相互抵消，把失配阶段报告为通过。
逐段判定的代价是某段样本不足时只能给出 `unverified/warn`，不会被大阶段稀释成 `pass`。

未配置 `split.phases` 的旧配置保持原有全程汇总逻辑与输出格式。阶段级证据（计划窗口、
实际 counts、比例、偏差、卡方、样本不足与跨段重复原因）会写入 `audit.json`
的 `split.balance.details.phases`、决策卡 `split_phases`、Markdown / HTML 报告与本地 Web。

### 统计推断

全部计算只用标准库：

- **比例指标**：两个独立比例检验。p 值用合并方差的标准误；效应置信区间用非合并方差 Wald 区间。
- **均值指标**：Welch t 检验，置信区间使用 Welch–Satterthwaite 自由度和 t 分位数。
- **卡方**：拟合优度统计量与上尾概率。
- 数值函数包含误差函数、Lanczos `lgamma`、正则化不完全 Beta / Gamma 的连分式实现。
- 所有 p 值夹取到 `[0, 1]`；零方差样本有显式退化处理。

### 主指标稳健性

主指标只有在以下条件同时满足时标记为稳健：

1. 效应方向符合业务方向；
2. p 值小于配置 alpha；
3. 绝对效应达到 `min_effect`；
4. 置信区间整体位于 0 的业务有利一侧；
5. 分层中没有显著方向反转；
6. 可评估分层的同向占比达到 `min_favorable_share`；
7. 至少一个分层可评估。

### 护栏

对每个护栏指标：

```text
higher_is_better: degradation = control - treatment
lower_is_better:  degradation = treatment - control
```

- `degradation > max_degradation` → `breach`
- `degradation > 0.5 * max_degradation` 且未越界 → `watch`
- 其他 → `ok`

`breach` 是阻断项；`watch` 进入 finding 并影响保守决策。

### 分层方向反转

- 对每个配置字段，按取值分组；组内对照 / 实验各至少 `min_group_size` 才可评估。
- 单组比较使用与主指标相同的方法，alpha 取 `stratification.alpha`。
- 若总体效应与某可评估分层效应方向相反且该分层显著，标记为 `reversal`。
- 同时计算可评估分层的同向占比、反向组数与截断情况。
- 分层组数超过 `max_groups` 时按样本量取前 N 组并标记截断，不静默丢弃。

---

## 声明式决策规则

决策完全由 YAML 规则决定，不执行用户代码。

### 规则结构

```yaml
decision:
  fallback: review
  cards:
    ship: { title: 可上线, action: 放量并继续观测 }
    iterate: { title: 继续迭代, action: 补齐证据 }
    stop: { title: 停止, action: 停止实验 }
    review: { title: 人工复核, action: 先修复数据或分流关系 }
  rules:
    - id: stop-guardrail
      card: stop
      priority: 130
      reason: 护栏越界
      when:
        any:
          - flag: guardrail_breach
          - flag: primary_negative_significant
```

条件节点只有三种组合子和一种叶子：

```yaml
when:
  all:
    - flag: split_balanced
    - any:
        - flag: primary_effect_robust
        - flag: primary_inconclusive
```

- `flag`：布尔 flag 为真
- `all`：所有子条件为真
- `any`：任一子条件为真
- `none`：所有子条件为假

### 评估方式

1. 规则按 `priority` 从高到低排序；同优先级按规则 id 稳定排序。
2. 逐条评估条件，记录 `matched`，形成可审计的 trace。
3. 第一条命中的规则决定决策卡；都不命中时使用 `fallback`。
4. 决策卡输出标题、建议动作、命中规则、优先级、理由和完整 trace。
5. 规则引用了代码未提供的 flag 时，审计直接报错，防止静默误判。

### 内置 flags

| flag | 含义 |
| --- | --- |
| `split_balanced` / `split_imbalanced` | 分流检查通过 / 未通过（分阶段配置下由最差阶段决定） |
| `split_phase_failed` | 分阶段配置下至少一个阶段真实失衡（仅分阶段配置提供） |
| `split_phase_unverified` | 分阶段配置下至少一个阶段样本不足（仅分阶段配置提供） |
| `split_phase_reassigned` | 分阶段配置下存在跨阶段重复分配的 unit（仅分阶段配置提供） |
| `no_duplicate_exposure` / `duplicate_exposure` | 无重复曝光 / 有重复曝光 |
| `no_mutex_conflict` / `mutex_conflict` | 无互斥冲突 / 有互斥冲突 |
| `data_quality_ok` | 数据一致性检查为 pass |
| `data_quality_warning` / `data_quality_failed` | 数据质量为 warn / fail |
| `coverage_ok` | 观测覆盖率至少 90% 且无结构性异常 |
| `sample_sufficient` / `sample_insufficient` | 每组样本达标 / 不达标 |
| `primary_positive_significant` | 主指标显著且方向有利 |
| `primary_negative_significant` | 主指标显著且方向不利 |
| `primary_inconclusive` | 主指标不显著 |
| `effect_meets_threshold` | 绝对效应达到 `min_effect` |
| `primary_effect_robust` | 上述全部稳健条件通过 |
| `effect_fragile` | 显著正向但不够稳健 |
| `stratified_reversal` / `no_stratified_reversal` | 有 / 无显著方向反转 |
| `guardrail_breach` / `no_guardrail_breach` | 有 / 无护栏越界 |
| `guardrail_watch` / `no_guardrail_watch` | 有 / 无护栏临界 |

---

## 输出与台账

### 产物

每次运行写入 `out/<run_id>/`：

| 文件 | 内容 |
| --- | --- |
| `audit.json` | 完整结构化审计结果：run、实验、counts、flags、checks、findings、metrics、stratification、decision、reproducibility |
| `decision_card.json` | 精炼决策卡：决策、命中规则、主指标、护栏、检查摘要、输入摘要 |
| `report.md` | 中文可审阅报告，含决策卡、指标、检查、findings、分层、复现信息 |
| `report.html` | 自包含 HTML；内联 CSS，不使用外部脚本、字体、图片或 CDN |

`report.html` 生成时会压缩标签间空白，保持单文件紧凑；源模板仍保留可读格式。

### SQLite 表

默认数据库：`data/ledger.sqlite3`。

| 表 | 作用 |
| --- | --- |
| `runs` | 一次运行：实验、决策、命中规则、主指标摘要、摘要哈希、产物目录 |
| `checks` | 检查项、状态、摘要与细节 JSON |
| `findings` | 结构化问题、严重度、类别、证据 JSON |
| `metrics` | 主指标、次级指标与护栏的统计结果 |
| `strata` | 每个分层组的样本量、效应、p 值、可评估与反转标记 |

台账使用参数化 SQL、外键和事务；同一个 run_id 重写会先清理旧子记录，保证幂等。

---

## 本地 Web

```bash
python -m app serve --port 5000
```

强制约束：

- 只绑定 `127.0.0.1`，不提供改 host 的参数。
- 启动前检查端口占用；端口已被占用时直接退出，不静默启动第二个监听者。
- 只读展示：不接收上传、不触发审计、不连接外部服务。
- 只读取 SQLite 台账、`out/` 产物和内置模板 / 静态样式。
- `out/` 文件下载使用 run_id 校验、产物白名单和真实路径约束。
- 响应带 CSP、`X-Frame-Options: DENY`、`X-Content-Type-Options: nosniff`、`Referrer-Policy: no-referrer` 和 `Cache-Control: no-store`。

路由：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 台账与决策筛选 |
| GET | `/runs/<run_id>` | 决策卡、指标、检查、finding、分层与 trace |
| GET | `/runs/<run_id>/artifacts/<name>` | 允许的四种产物 |
| GET | `/api/runs` | 台账 JSON 列表 |
| GET | `/api/runs/<run_id>` | 完整 `audit.json` |
| GET | `/healthz` | 本地健康检查 |

---

## 示例 fixture

`fixtures/` 是固定随机种子生成的 100% 合成数据，不含真实用户、真实组织或真实域名。

- 1600 个分配单元，1600 条观测；对照 / 实验各 800。
- 平台分层：`desktop=960`、`mobile=400`、`legacy=240`。
- 区域分层：`north=700`、`south=600`、`west=300`。
- 主指标：激活率；对照 `200/800=25.0%`，实验 `240/800=30.0%`，绝对效应 `+5.0pp`，p≈`0.0251`。
- 护栏：崩溃率；对照 `8/800=1.0%`，实验 `14/800=1.75%`，劣化 `0.75pp`，未超过 `1.0pp` 阈值，但进入 `watch`。
- 分层：`legacy` 平台对照 `40/120`、实验 `12/120`，方向显著为负，形成总体正、局部分层负的方向反转。
- 互斥实验：配置了 `exp-checkout-copy-2026`，样例中没有冲突。
- 重复曝光：样例中没有重复。

因此演示决策为 **继续迭代（iterate）**，命中 `iterate-evidence`，同时产生：

- `stratification.reversal`（high）
- `guardrail.watch.crash_rate`（medium）
- `metric.fragile`（medium）

这组 fixture 特意同时覆盖“比例健康、主指标正向显著、护栏临界、分层反转”这一类真实审计中常见的冲突证据，避免演示退化为单一阈值判断。

---

## 确定性与复现

- 不使用运行期随机数；样例 CSV 由固定种子离线生成后提交。
- 统计计算是纯函数，结果只依赖输入与配置。
- 输出 JSON 使用稳定键排序；检查、finding、分组和规则 trace 都有确定排序。
- 输入摘要与配置摘要写入 JSON、决策卡、HTML 和台账。
- **例外**：`run_id` 与 `generated_at` 必然随运行变化。
- 相同输入、配置和代码下，将两份 `audit.json` 的 `run` 字段排除后应逐字节等价。

复现检查建议：

```bash
python -m app demo --out out --db data/ledger.sqlite3
python -m app demo --out out --db data/ledger.sqlite3
# 比较两个 run 的 JSON 时排除 run.run_id / run.generated_at
```

---

## 安全与离线边界

- 无任何 HTTP 客户端、爬虫、外部 API 调用或遥测。
- 模板无 CDN、远程字体、远程图片、外部脚本。
- 所有示例地址只使用 `127.0.0.1` / `localhost` / `::1`。
- 所有输入、输出、数据库路径经过 `Path.resolve()` 与根目录 commonpath 检查，阻止 `../` 和软链接逃逸。
- Web 产物只允许 `audit.json`、`decision_card.json`、`report.md`、`report.html`，run_id 与台账校验后才读取。
- 不保存任何密钥、令牌、口令、私钥、真实用户标识或真实组织信息。
- Web 仅作为本地审阅界面；Flask 开发服务器不应暴露于公网。

---

## 测试

```bash
pytest -q
```

测试覆盖：

- 统计函数与已知参考值：正态分位数、比例检验、Welch t、卡方、t 分位数。
- YAML/CSV 契约失败路径、非法 unit_id、非法比例值、路径逃逸。
- 完整 fixture 审计、分流失衡、重复曝光、互斥冲突、未分配观测、负向主指标。
- 分阶段 rollout：schedule 分区契约、边界归段、正反偏差不可抵消、小样本
  `unverified/warn`、跨阶段重复 unit、CSV 行序无关、静态配置输出不变。
- 规则优先级、fallback、`all/any/none`、引用 flag 检查。
- 四种产物的完整性、HTML 自包含、行数约束、Markdown 内容。
- SQLite 写入、幂等、列表/筛选/读取。
- CLI demo、Web 路由、CSP 响应头、API、非法筛选与产物路径。

---

## 已知边界

- 只支持双变体 A/B；多臂实验需要扩展指标模型与分层策略。
- 比例指标使用二项近似 z 检验；小样本极端比例应改用精确检验或分层样本说明。
- 均值指标要求每组至少 2 个观测。
- 分层是描述性复核，不是因果异质性检验；因果推断不在范围内。
- SQLite 与 Flask 均为本地单机使用；不设计多实例写入或生产部署。
- 决策卡是审计建议，不是自动上线执行器。
