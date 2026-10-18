# 宽基研究台 · cn-etf-monitor

五个A股宽基指数的全历史因子研究网页。顶部是有证据引用的综合观察，下方是各因子的当前读数、历史分位、全样本/样本外IC、滚动IC与指数走势图。

## 运行

需要 Python 3.12+、Node.js 22+、curl。

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-lock.txt
npm ci
cp .env.example .env
.venv/bin/python -m backend.app.cli backfill
npm run build
./start.sh
```

网页 http://localhost:3000，API文档 http://127.0.0.1:8000/docs。默认本地 SQLite 存储于 `data/monitor.db`；PostgreSQL 使用相同 SQLAlchemy 模型，通过 `DATABASE_URL` 切换。密钥只放在 `.env`，不会发送到浏览器。

```bash
# 每日刷新：全历史拉取、检测修订，未变化则跳过计算
.venv/bin/python -m backend.app.cli update
# 仅用已归档原始响应重放，可离线重算
.venv/bin/python -m backend.app.cli recompute
# 仅更新顶部综合规则视图（不重复存储历史因子）
.venv/bin/python -m backend.app.cli assess
# 为已有快照补充增量预测诊断，不复制行情/因子表
.venv/bin/python -m backend.app.cli diagnostics
# 单指数、强制重算
.venv/bin/python -m backend.app.cli recompute --index 000300.SH --force
# 覆盖报告与逐日审计
.venv/bin/python -m backend.app.cli coverage
.venv/bin/python scripts/audit-data.py
```

## 指数与历史范围

| 指数 | 代码 | 已获取的最早日期 |
|---|---|---|
| 沪深300 | 000300.SH | 2002-01-04 |
| 中证500 | 000905.SH | 2005-01-04 |
| 上证50 | 000016.SH | 2004-01-02 |
| 创业板50 | 399673.SZ | 2010-05-31 |
| 科创50 | 000688.SH | 2020-01-02 |

历史不会按最近几年截断。保存源返回的全部历史，并将发布前回溯区间与正式发布后区间区分。首次交付数据截至2026-10-16，实际覆盖应以网页覆盖报告为准。

来源：新浪完整日线；东方财富日线与成交额；国证指数官方创业板50回溯历史；中证官方接口补充其余四个指数的成交额与可取得的滚动市盈率。中证接口未被主源确认的边界日期不导入。重叠价格差超过0.2%停止发布新快照。行情源可能不可用，原始响应按SHA256归档；补充源不可用时可重放同日期历史缓存，最新缺失仍保留缺失状态。

“全历史覆盖”指可取得的历史观测，**不表示每个因子在每一天都有有效数值**。窗口预热、源缺失、常量分母等分别记录为 `warmup`、`missing_source`、`undefined`。官方基日只有收盘值时，开高低保留NULL，不制造OHLC。独立交易所日历认证尚未接入；审计脚本可逐日比较五个指数的日期集合。

## 指标与扩展

首批19个定义：5/20/60日动量、MA20/60/120/250乖离、MA20斜率、RSI14、标准化MACD柱、20/60日年化波动率、ATR14、距一年高点回撤、252日区间位置、成交量/成交额放大倍数、PE-TTM与PB。

价格与量价类按全部历史计算。五个指数成交额已接通；创业板50基日仅收盘值，成交额从下一交易日起。沪深300早期成交量源为零时保留缺失，不用成交额反推成交量。沪深300、中证500、上证50及科创50的PE使用中证实际提供区间，按次日可用假设对齐；早期空白不补造。PB及创业板50的日频PE仍需额外数据源。匿名公开估值样本经核验仅有稀疏月度历史，不据此伪造完整日线。净值、份额、ETF折溢价不属于本版指数因子，尚未实现ETF产品层。

新增因子在 `backend/app/factors.py` 使用 `register(Factor(...))` 注册：稳定ID、版本、分类、单位、预热长度、输入列、公式、说明与向量计算函数。要求计算只使用当前及过去数据。修改公式需增加版本，再运行历史重算。新数值因子自动拥有卡片、走势、IC及统计诊断，不需改前端或新增结果列。

估值导入格式，路径 `data/imports/000300.SH-valuations.csv`，其余指数同理：

```csv
date,available_date,pe_ttm,pb
2026-10-15,2026-10-16,13.2,1.4
```

`date`为原始数据日期，`available_date`为当时可用日期。按首次可用交易日对齐，不前向填充。示例数值仅用于说明格式，不随项目导入。设置 `TUSHARE_TOKEN` 可按年请求 `index_dailybasic`，权限和指数覆盖以账户实际返回为准。该接口缺少真实发布时间，采用次日可用假设，不宣称严格点时数据。

## IC与统计口径

- 单指数时间序列Spearman Rank IC：因子值与未来1/5/10/20/60交易日价格指数收盘收益的相关性，保留原始正负号。
- 因子与图表包含发布前回溯；默认IC只用指数正式发布后的观测。
- 全样本IC、按年度扩展的样本外IC、逐年IC、日频252日滚动IC。滚动至少200有效样本，横轴为最后一个收益标签的成熟日期。
- 样本外至少504个历史训练样本，训练/测试边界剔除跨期收益标签。分组阈值只来自当年之前的训练集。
- 95%区间采用199次移动分块Bootstrap，块长至少为预测期限，以处理重叠收益。区间未经多重检验校正，不用于宣称显著性。
- 分组收益、散点与领先滞后图均可展开。负滞后使用较晚因子，含前视，仅作诊断；不进入预测结论。另提供固定5阶滞后、HAC稳健Wald双向预测检验，以及控制历史收益和波动率后的年度样本外误差比较。因子变化预先固定为一阶差分，ADF未通过则不报告预测关系；双向p值在本指数全部因子内作BH-FDR校正。
- 当前分位用最近1260个交易日的有效观测；标准化叠加按所选图表范围计算，仅用于显示。
- 顶部状态按成熟历史方向、样本外方向与年度一致性过滤；同类先平均，再按类别等权。合格因子还要求 |样本外IC|≥0.03 且95%单项分块区间不跨零；至少3个合格因子且覆盖2类才给综合预测方向，规则版本随结果记录。这个组合尚无独立的样本外策略验证。
- 当前第三方历史可能已经修订，系统只能从首次采集起保留自己的原始版本，不能重建此前的真实点时版本。

增量预测模块固定研究次日收益，与页面IC期限独立，不参与顶部综合方向。增量样本外R²=1−增广模型误差平方和/基线模型误差平方和，负值表示变差。只有过去变量进入预测，模型与标准化参数按训练年度拟合。

IC研究不等于交易收益回测。本版没有交易执行、手续费/滑点、仓位策略、收益概率或经济因果识别。

## 数据库与发布

主表：`market_indices`、`data_snapshots`、`daily_bars`、`factor_definitions`、`factor_values`、`forward_returns`、`factor_evaluations`、`rolling_evaluations`、`index_assessments`、`job_runs`。结果按行存储，包含指标版本和输入数据哈希。

同一指数的整批数据在单事务内发布，更新失败时保留上个已发布快照。源数据哈希相同时跳过重算，历史长度或已覆盖字段倒退时拒绝替换。CLI用文件锁防止本机重复运行；多机部署需使用共享数据卷或数据库级分布式锁。

当前每次有修订都会保留完整快照，日频滚动IC占据主要存储。长期运行应配置备份与快照归档/保留策略；本版不会自动删除历史版本。原始响应也不进Git。数据库初始结构由SQLAlchemy创建，内置v2的可空OHLC迁移；后续复杂结构变更建议迁移到Alembic。

## 每日更新

独立调度器在北京时间工作日08:30、16:30、20:30尝试更新；休市日源未变化时幂等跳过，源数据异常会记录在任务日志，真实交易日历接入后可进一步减少空跑。收盘前不采纳当日未完成的日线。

```bash
.venv/bin/python -m backend.app.scheduler
```

macOS 可预览并安装当前项目的单独定时任务：

```bash
.venv/bin/python ops/install-schedule.py --preview
.venv/bin/python ops/install-schedule.py
```

任务名 `com.cn-etf-monitor.daily-update`，日志位于 `data/logs/scheduled-update*.log`。需电脑开机、当前用户登录；机器休眠或离线时不保证准点更新。网页与API通过 `./start.sh` 启动。

## PostgreSQL与Docker部署

在 `.env` 设置非空 `POSTGRES_PASSWORD`，建议使用十六进制字符串。Docker中前端、API、PostgreSQL和调度器分别运行，本机只暴露loopback端口。

```bash
docker compose up -d --build
docker compose exec api python -m backend.app.cli backfill
```

Docker/PostgreSQL需要目标机器已安装Docker。切换数据库后需要重新回填；本版不自动迁移已有SQLite内容。上线公网前再按实际部署环境加入认证和HTTPS。远程写操作默认未启用；可选管理配置查询使用Bearer Token且不返回密钥。

## 验证

```bash
.venv/bin/python -m pytest -q
npm run typecheck
npm run build
.venv/bin/python scripts/audit-data.py
```

测试覆盖因子前缀不变性（防前视）、预热/缺失、RSI边界、收益标签成熟、IC与SciPy对照、滚动日期对齐、样本外边界剔除与双源异常阻断。

可选的真实PostgreSQL集成验证，会复制当前五个指数的全部历史快照到临时隔离实例，逐表核对计数并实际运行HTTP API，退出后清理临时实例，不改动源数据库：

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python scripts/verify-postgres.py
```

新浪解码器来自MIT许可的AKShare，署名与许可见 `NOTICE`。任何远端JavaScript不被执行，仅把编码数据传入本地固定解码器。
