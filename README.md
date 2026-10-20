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

本机网页的“刷新数据”会启动五个指数的后台采集与全历史因子计算，显示任务进度和失败原因，完成后自动读取新快照。设置`CN_MONITOR_PUBLISH=1`后，还会自动导出并同步到Netlify；本机页面分别显示计算、网页同步及发布结果。`POST /api/v1/updates` 返回202；`GET /api/v1/updates/latest` 查询进度。网页更新与CLI/调度器共用文件锁，上传结束后才释放，重复启动返回409。更新期间继续提供已发布快照，收盘前不纳入当日未完成日线。网页同步失败时保留本机新数据和线上旧结果，状态显示`publish_failed`；上传中进程退出则显示`publish_interrupted`。实际最新日期以各指数覆盖报告为准。网页任务日志在 `data/logs/web-update.log`。

无管理密钥时，仅允许本机同源浏览器发起写操作；远程API调用须提供 `Authorization: Bearer <ADMIN_TOKEN>`。密钥不会传给前端。Docker网页经容器网络访问时请用CLI更新，或通过经认证的API调用；本机访问校验不会把容器网络自动视作可信。

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

现有20个定义：5/20/60日动量、MA20/60/120/250乖离、MA20斜率、RSI14、标准化MACD柱、20/60日年化波动率、ATR14、距一年高点回撤、252日区间位置、成交量/成交额放大倍数、PE-TTM、PB与股债收益差。

价格与量价类按全部历史计算。五个指数成交额已接通；创业板50基日仅收盘值，成交额从下一交易日起。沪深300早期成交量源为零时保留缺失，不用成交额反推成交量。沪深300、中证500、上证50及科创50的PE使用中证实际提供区间，按次日可用假设对齐；早期空白不补造。PB及创业板50的日频PE仍需额外数据源。匿名公开估值样本经核验仅有稀疏月度历史，不据此伪造完整日线。净值、份额、ETF折溢价不属于本版指数因子，尚未实现ETF产品层。

新增因子在 `backend/app/factors.py` 使用 `register(Factor(...))` 注册：稳定ID、版本、分类、单位、预热长度、输入列、公式、说明与向量计算函数。要求计算只使用当前及过去数据。修改公式需增加版本，再运行历史重算。新数值因子自动拥有卡片、走势、IC及统计诊断，不需改前端或新增结果列。

每张因子卡片展开后，在走势图上方提供“因子解读与历史相近位置”，包括关联逻辑、当天读数/滚动分位、所选期限的样本外IC解释，以及历史相近位置的未来表现。当前期限跟随页面研究期限，股债收益差跟随该因子的独立期限选择；可展开其他期限对照、最近12个历史观察期和计算口径。输入缺失时保留解释及缺失提示，不沿用旧读数。

本机API从同一快照的价格、因子值和评估生成解读；每次导出时用相同函数生成紧凑分析结果，随因子对象上传Netlify。因此本机网页刷新、CLI或调度器更新成功并同步后，线上解读、图表与统计会一起切换到同一发布版本；无需另运行报告脚本。旧发布暂不含解读时，页面提示刷新同步，继续保留原有图表。分析版本为`position-match-v1`。

另可运行 `.venv/bin/python scripts/analyze-factors.py` 生成独立的 `artifacts/factor-analysis.html` 和同名JSON。独立报告只读当前五个指数的已发布数据库快照，不采集、写库或上传；需要最新独立文件时重新运行该命令。

相近位置固定使用当日近1260交易日分位的±5个百分点，历史各日分位仅由当时及以前的数据计算；只统计发布后、标签已成熟的信号。除全部匹配日外，另按最早优先的固定规则选择收益区间不重叠的观察期，并与相同有效日期范围的指数基准比较。该分析是回顾性描述，未进行前瞻策略验证，不把样本上涨比例视为未来上涨概率；少于30个不重叠观察期标记为参考有限。HTML提供最近12个可核对样本，JSON保留全部不重叠样本与快照指纹。

估值导入格式，路径 `data/imports/000300.SH-valuations.csv`，其余指数同理：

```csv
date,available_date,pe_ttm,pb
2026-10-15,2026-10-16,13.2,1.4
```

`date`为原始数据日期，`available_date`为当时可用日期。按首次可用交易日对齐，不前向填充。示例数值仅用于说明格式，不随项目导入。设置 `TUSHARE_TOKEN` 可按年请求 `index_dailybasic`，权限和指数覆盖以账户实际返回为准。该接口缺少真实发布时间，采用次日可用假设，不宣称严格点时数据。

## 股债收益差（差值版）

因子ID为 `equity_bond_spread_10y`，公式为 `100 / PE-TTM - 10年期国债到期收益率(%)`，输出单位是百分点，1个百分点=100基点。只使用正PE；PE缺失、国债源缺失和PE非正分别保留缺源或不可定义状态。它是相对估值观察项，首版不参与顶部综合评分。

债券数据直接来自中债官网 `cbweb-pbc-web/pbc/historyQuery`，按年请求2002年至今的全部可返回历史，筛选“中债国债收益率曲线”的10年列，保留原始HTML与哈希。空年份也保留真实归档，不推算早期数据。已结束年份复用归档，当年每日重取；`--force`同时重新请求历史年份。一次更新任务只采集一份共享国债历史。离线 `recompute` 重放归档。源端额外的日期只保存为源观测，不会成为指数交易日。

先以原始观测日配对PE与国债利率，再映射到下一A股交易日；两端任何一个尚未可用均不提前使用。未提供真实披露时点的来源，采用次日可用假设，不宣称严格点时数据。日频观测不向后填补，延迟超过下一股票交易日的旧观测不重新激活。收盘后的最新原始数据会在后续可用交易日进入研究信号，卡片会同时列出原始观测日和信号日。

新增 `macro_series`、`macro_observations`、`valuation_observations`、`factor_inputs` 表，记录原始输入版本和每个快照日期的引用。原始响应、输入引用和引擎版本参与研究数据版本识别；旧快照的输入保持不变。无需改动已有数据库列，新表由初始化自动创建。

卡片提供当前收益差、最近1260个交易日内的分位（至少756个有效样本）、输入对照图、指数与收益差走势图、IC及样本外检验。此因子可独立选择1/5/10/20/60/120/252日；其余因子的期限保持原配置。回测目标仍是股票价格指数未来收益，尚未接入股债全收益差回测。

本次官方接口实际返回的国债历史为2006-03-01至2026-10-19，共5,159条源观测；空白的更早请求年份保留空归档。指数因子的有效日期仍取同日PE和国债输入交集。

当前PE源的沪深300、中证500、上证50历史从2011年开始，科创50从2020年开始；创业板50缺少PE，因子按全部指数日期保留缺失，需补充同口径PE数据后重算。通过已有估值CSV入口补充时，必须提供原始观测日和可用日期。

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

主表：`market_indices`、`data_snapshots`、`daily_bars`、`factor_definitions`、`factor_values`、`forward_returns`、`factor_evaluations`、`rolling_evaluations`、`index_assessments`、`job_runs`，以及股债输入与宏观观测表。结果按行存储，包含指标版本和输入数据哈希。

同一指数的整批数据在单事务内发布，更新失败时保留上个已发布快照。源数据哈希相同时跳过重算，历史长度或已覆盖字段倒退时拒绝替换。CLI用文件锁防止本机重复运行；多机部署需使用共享数据卷或数据库级分布式锁。

当前每次有修订都会保留完整快照，日频滚动IC占据主要存储。长期运行应配置备份与快照归档/保留策略；本版不会自动删除历史版本。原始响应也不进Git。数据库初始结构由SQLAlchemy创建，内置v2的可空OHLC迁移；后续复杂结构变更建议迁移到Alembic。

## 每日更新

独立调度器在北京时间工作日08:30、16:30、20:30尝试更新；休市日源未变化时幂等跳过，源数据异常会记录在任务日志，真实交易日历接入后可进一步减少空跑。收盘前不采纳当日未完成的日线。

`./start.sh` 会随网页一起启动此调度器，日志在 `data/logs/scheduler.log`，关闭启动脚本会一起停止服务与调度器。它在启动终端的权限环境中执行，可以避开下述独立launchd任务的目录授权问题；仍需电脑开机且进程持续运行。

```bash
.venv/bin/python -m backend.app.scheduler
```

macOS 可预览并安装当前项目的单独定时任务：

```bash
.venv/bin/python ops/install-schedule.py --preview
.venv/bin/python ops/install-schedule.py
```

任务名 `com.cn-etf-monitor.daily-update`，日志位于 `data/logs/scheduled-update*.log`。需电脑开机、当前用户登录；机器休眠或离线时不保证准点更新。网页与API通过 `./start.sh` 启动。

如果该目录位于 macOS 受保护的 Documents 下，launchd 日志可能出现 `getcwd: Operation not permitted` / `run-update.sh: Operation not permitted`，此时任务根本没有启动。需在系统隐私设置中授权实际执行程序访问该目录，或将项目部署到不受该限制的目录并重新安装任务。也可在已获目录权限的终端运行上面的 Python 调度器；安装定时任务本身不等于验证了任务执行成功。

## Netlify：本机计算，网页展示

该模式不需要云数据库或云端Python。SQLite、原始响应与计算留在本机；Netlify托管静态网页、只读结果函数和Blobs结果存储。公开站点不提供更新计算接口。线上网页在前台每60秒检查一次发布版本，切回页面时立即检查，发现新版后自动读取；隐藏页面不定时请求，版本未变不重载图表，网络失败保留已加载结果。“刷新结果”可手动立即读取。

首次配置（Node.js 22.13+；本项目实际验证使用Node.js 26）：

```bash
npm ci
node_modules/.bin/netlify login
node_modules/.bin/netlify sites:create --name 你的唯一站点名 --account-slug 你的团队标识
npm run deploy:netlify
.venv/bin/python -m backend.app.cli publish --skip-update
```

登录需在浏览器授权。站点关联保存在被Git忽略的`.netlify/state.json`，上传程序复用官方CLI保存的本机凭证。也可通过本机`.env`设置`NETLIFY_SITE_ID`与`NETLIFY_AUTH_TOKEN`，不要将凭证设置为`NEXT_PUBLIC_*`或提交Git。不需要把本机凭证添加到Netlify网页环境变量。

日常更新并发布：

```bash
.venv/bin/python -m backend.app.cli publish
```

仅发布现有计算结果可加`--skip-update`。设置本机`.env`的`CN_MONITOR_PUBLISH=1`后，本机网页“刷新数据”、调度器和`ops/run-update.sh`原有的CLI `update`命令都会在更新成功后自动上传；修改该配置需重启服务。任一指数更新失败时，本次不切换线上结果。上传失败可再次点击本机“刷新数据”重试，未变化的计算和已上传对象会复用；也可直接执行`publish --skip-update`重试上传。

电脑关机、休眠或离线期间，网站仍可展示最后成功发布的结果，但不会产生新数据。顶部同时显示发布时间和各指数行情日期，避免将重新读取误认为完成采集。

导出与预览（无需云端凭证）：

```bash
.venv/bin/python -m backend.app.cli export
npm run build:published
npm run preview:published
```

访问`http://127.0.0.1:3001`。Next.js静态导出位于`.next-published/`，由`netlify.toml`指定发布；本机常规模式使用`.next-local/`和FastAPI，避免静态导出的内部`.next/`构建覆盖正在运行的本机网页。Docker standalone仍使用`.next/`。不要上传项目根目录或`data/`。

已关联本项目站点：[宽基研究台](https://cn-etf-monitor.netlify.app/)。Netlify新站点默认可能启用团队登录限制，访问范围在项目的`General → Visitor access → Project visibility`中管理。

`data/publication/manifest.json`描述五个指数、快照ID和全部对象SHA256；每个指数一份价格/评估/收益标签对象，每个因子一份完整历史值/缺失状态/滚动IC/股债输入对象。所有对象采用确定性gzip和内容哈希命名。首次实际导出105份对象，压缩后约30MB，保留全部历史和股债因子120/252日期限；不向网页提供原始归档路径、采集错误详情、数据库或任务日志。

上传先校验大小和哈希、并行写入不可变对象，全部成功后通过ETag条件写切换唯一清单。相同导出跳过发布，失败时保留旧清单；打开中的网页按原清单继续读取对应快照。旧对象保留，需按实际存储规模自行制定归档/保留策略。

可执行`node scripts/publish-results.mjs --verify`核对远端清单与全部对象是否完整。上传支持本机`HTTP_PROXY`/`HTTPS_PROXY`；凭证及错误详情不会写入公开结果。

数据上传独立于前端部署。日常发布无需运行`deploy:netlify`；只有网页或读取函数代码变化才重新部署，减少部署额度消耗。Netlify按账号套餐计量；免费额度及超额行为以控制台为准，不承诺无限免费。

## PostgreSQL与Docker部署

在 `.env` 设置非空 `POSTGRES_PASSWORD`，建议使用十六进制字符串。Docker中前端、API、PostgreSQL和调度器分别运行，本机只暴露loopback端口。

```bash
docker compose up -d --build
docker compose exec api python -m backend.app.cli backfill
```

Docker/PostgreSQL需要目标机器已安装Docker。切换数据库后需要重新回填；本版不自动迁移已有SQLite内容。上线公网前再按实际部署环境加入认证和HTTPS。远程更新与管理配置查询使用Bearer Token且不返回密钥。

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
