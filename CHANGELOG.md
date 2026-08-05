# Changelog

## Unreleased (2026-08-05)

### Features

* **factor-target-overview:** 桌面主窗口和 Android 主界面新增 A 股行为因子目标前四总览，按 `target_weight_pct` 降序展示；桌面点击后切换到对应监控卡片，Android 点击后打开该标的委员会分析。入选卡片同步展示模型目标仓位，并明确标注“模型目标，不是持仓”。
* **weekend-news-analysis:** 桌面与 Android 周末新闻中的龙头标的改为直接进入现有委员会分析界面；已监控标的复用当前快照，未关注标的先展示分析进度并解析代码、最新行情，不再经过 Android 中间信息气泡。

### Improvements

* **factor-semantics:** Kotlin 网络模型和缓存解析器透传可选 `selection_scope` / `represents_account_holding`，主界面只把 `selected=true` 解释为因子目标组合成员，不据此修改或描述真实账户持仓；旧快照和旧客户端可继续忽略新增字段。
* **weekend-news-refresh:** 桌面文件监听加入最新周末 `summary/report` 的文件名、修改时间和大小签名，新闻任务重写摘要后会主动刷新已打开的新闻悬浮层。
* **weekend-news-provenance:** 周末摘要记录原始缓存的新闻日期区间，桌面来源栏区分新闻日期与摘要生成文件；新闻源测试改用临时缓存目录，避免商业航天测试样本覆盖真实 `data/weekend_news/summary_*.json`。

## Unreleased (2026-07-17)

### Improvements

* **desktop-tray:** 桌面监控新增 Windows 系统托盘，直接复用 APK 的高分辨率 `ic_launcher.webp`；关闭按钮隐藏到托盘，单击托盘图标恢复并临时置顶，右键菜单可显式退出并停止后台服务，托盘线程通过队列回到 Tk 主线程执行 UI 操作。
* **event-provenance:** `events.db` 在线新增可选 `ingested_by` 溯源字段，区分“新闻发布来源”和“由哪个任务/代理投喂”；`event_watch` 入库自动标记来源，便于从异常委员会结论反查输入链路。
* **macro-recall:** 事件任务不再只用单条 Fed 查询兜底；无论持仓内容都常驻检索中国 CPI/PPI/PMI/LPR/MLF 与 FOMC/CPI/非农等高影响宏观发布，减少按标的搜索漏掉数据发布本身的问题。
* **sqlite-lifecycle:** 账本、行情、事件、交易和洞察库统一使用 WAL 生命周期工具；连接启动/关闭做非阻塞 checkpoint，仅在 WAL 超过可配置阈值且无活跃读者时截断，关闭前回滚未提交事务。
* **llm-audit:** LLM 客户端传输协议和审计 provider 解耦，千问、智谱等 OpenAI 兼容服务不再被遥测硬标为 DeepSeek。
* **sell-decisions:** 生产卖出提醒统一由 A 股行为因子、委员会/确定性优化器方向、负向建议金额、卖出评分、可卖手数、A 股 T+1 和重复成交冷却决定；删除成本锚定持仓策略模块与周度调度，`position_exit_policy` 仅保留空接口对象。
* **factor-repair:** A 股请求缺少有效行为因子时，自动合并目标标的、真实持仓和关注列表，补拉两年历史并重建横截面及五日目标；仍无法形成可靠因子时返回 `WAIT/无法判断`，桌面卡片显示灰色且禁止操作，不再伪装成 `HOLD` 或回退旧技术收益模型。
* **trading-mode:** 桌面端与 Android 只保留主动盈利、现金回收两种模式；旧 `risk_off`、`bear`、`defensive` 及“主动避险”配置统一迁移为 `cash_recovery`，避免旧配置静默回落成主动盈利。

### Bug Fixes

* **event-semantics:** `opportunity` 只表示值得进一步核验的潜在催化剂，不再声明未来收益方向或单独构成买入信号；通知图标改为中性检查语义，避免视觉上暗示必涨。
* **sell-guard:** 修复停用持仓纪律线后，重复交易护栏仍要求已持仓卖出命中旧价格线，导致委员会 `SELL/TRIM` 被永久降级为 `HOLD` 的问题；卖出保留同方向冷却和下游 T+1/可卖手数约束，买回仍须出现新的回调、再入场或突破触发。
* **app-parity:** 手机 Chaquopy 本地委员会与后端直连保持相同的持仓纪律停用口径，并继续返回空的 `position_exit_policy` 兼容对象；App/桌面不再导入、上传或同步旧止盈参数与板块纪律环境变量。

## Unreleased (2026-07-12)

### Features

* **a-share-behavioral-factor:** 将 A 股确定性基座替换为可审计的行为因子：65% 多周期动量、25% 低换手代理、10% 低波动，并以 `60 日收益 > 0`、`收盘价 > EMA100` 作为资格门；横截面选择前四，按逆波动分配目标仓位，单股不超过 35%。
* **factor-calibration:** 每只 A 股使用独立的优化器权重。系统回看最近 63 个交易日，以 5 个交易日为一个持有块，计算该标的被选中后的横截面超额收益或未选中时成功规避的反向超额收益，再由样本可靠度和后验正收益概率生成 `0.55~1.0` 的权重。
* **decision-audit:** 每次盘中或桌面单标的委员会分析生成一个 `analysis_id`，并为 `real` / `committee` 派生互不相同的 `decision_id`；真实用户成交可关联决策，影子成交继续自动记账，但影子决策不进入桌面快照。
* **intraday-sentinel:** 新增按标的/板块和交易时段校准的 10 分钟经验残差收益哨兵；至少 200 个样本后才启用 99.5% 双尾阈值，带行情陈旧校验和方向独立冷却，只触发 Python 委员会复核、快照更新和震动，不机械下单。
* **backup-restore:** 配置与双账户账本导入在覆盖前生成时间戳备份和 SHA-256 manifest；SQLite 上传先做 header/schema/quick-check/非空校验，下载使用 online backup 合并 WAL 后导出一致快照。

### Improvements

* **committee-display:** `decision_synthesis` 和手机格式化报告优先展示 A 股行为因子分数、是否进入前四、目标仓位、最近三个月因子收益/命中率/样本数和标的独立优化器权重，避免因子已参与优化但用户只能看到普通 30 日预期收益。
* **android-factor-ui:** Android 委员会弹窗新增行为因子参数区，Kotlin 网络模型和本地委员会桥完整透传可选 `behavioral_factor`；旧客户端仍可忽略该对象。
* **optimizer:** A 股行为因子的 20 日期望收益和目标仓位优先于旧的 regime/动量/RSI 技术回退；目标成员和目标权重每 5 个交易日刷新，并使用 5 个百分点免交易带抑制十分钟监控造成的换手。可靠的持仓止盈止损卖出证据仍可越过免交易带。
* **daily-selection:** 日度选股历史窗口从 3 个月扩展到 2 年；有足够横截面和 252 日历史时，行为因子成为 A 股技术排序基座，新闻、板块资金、基本面、风险和一手资金约束继续作为独立证据。
* **committee-api:** `CommitteeRequest` / `CommitteeResponse` 新增向后兼容的可选 `behavioral_factor` 字段；旧 App/HTTP 调用不传时可使用持仓横截面或单标的低置信度降级，非 A 股继续走原优化器。
* **accuracy:** 命中率统一使用成熟的固定 30 日窗口，补充同样本市场基率、标准 balanced accuracy、Wilson 下界和相对多数类基准优势；小样本桶同时隐藏 hit/rate，并阻断由整体数字反推单个隐藏桶。
* **as-of:** 历史 verdict 复盘的 ATR 改为只使用决策日及以前的数据，价格锚点不再用决策日前不存在的未来首根 K 线。
* **market-data:** A 股前复权缓存增加重叠对数价格比例的 median/MAD 拼接检测；确认统一复权基准平移后原子替换完整两年历史，避免新旧 qfq 基准混存。
* **backtest:** 持仓标的缺失当日 K 线时默认使用最近有效收盘价估值，不再回退成本价；新增手续费、成交额换手、Sortino/下行风险、缺失持仓日诊断和成对区块 bootstrap 新旧对照脚本；对照行情默认冻结复用并校验总表/逐标的 SHA-256，避免后台缓存刷新让同一实验的绝对收益漂移。
* **concurrency:** 真实/影子重复交易保护按账户各自读取成交历史；账本成交使用 `BEGIN IMMEDIATE + UNIQUE idempotency_key`，跨连接重试只执行一次；Windows memory 文件锁改为有界重试，EventStore 显式释放 WAL 句柄。
* **desktop-sync:** 桌面端从远程同步账本前先停止本项目的 `backend.server` / `uvicorn` 和后台任务，释放 Windows SQLite 文件锁；同步完成后仅在原后端确实运行时通过统一启动器静默恢复。配置、参数和账本均在覆盖前备份，账本恢复成功后再由 ledger 回写 config/snapshot。

### Security

* **api:** `backend.server` 支持可选 `INVEST_API_TOKEN` Bearer 鉴权；未配置时保持旧行为，配置后除 `/api/health` 外均需 token。CORS 从通配符改为 `INVEST_CORS_ORIGINS` 明确白名单，桌面远程同步支持独立 `INVEST_REMOTE_API_TOKEN`。
* **privacy:** 快照和桌面 UI 递归剔除 `shadow_result` / `shadow_*`；准确率公开 JSON 使用互补小样本抑制，避免从总数反推隐藏方向命中数。

### Bug Fixes

* **android-levels:** 修复本地委员会分析完成后弹窗继续读取分析前 `HoldingRow`，导致本轮 `entry_exit_points` 已生成但回踩、突破、再入场、止损和止盈仍显示 `-` 的问题；弹窗现在优先读取本轮 `symbolSummary.entry_exit_points`。
* **android-cache:** 修复手机委员会缓存解析器只接受 `result.by_asset`、无法解析 `run_committee_local` 单标的根 JSON 的问题。解析器改为纯 Kotlin/Gson，同时恢复买卖点、基本面、操作和行为因子字段，不再依赖 Android `org.json` 桩。
* **windows:** 后端 UTF-8 初始化改为原地 `stdio.reconfigure()`，不再替换并关闭 pytest、IDE 或桌面宿主提供的输出流。
* **event-store:** 修复事件任务正常返回后未关闭 SQLite 连接导致 Windows 临时数据库无法删除的问题。

### Docs

* **android-debug:** 补充 APK 行为因子展示、买卖点刷新链路和真机调试数据保护红线；禁止在保存真实配置的手机上直接运行 `connectedDebugAndroidTest`，避免测试安装流程卸载主包并清除私有数据。
* **behavioral-factor:** 新增 A 股行为因子专题文档，说明公式、三个月标的级权重、5 日生产调仓、接口兼容、状态文件、无未来数据约束，以及 20 日研究回测与 5 日生产配置不可直接等同的边界。
* **wiki/readme:** 补充决策审计、幂等成交、经验分位数哨兵、复权拼接检测、固定期限准确率、可选 API token、安全恢复、Windows 远程同步停启顺序和可归因回测边界。

## Unreleased (2026-07-04)

### Features

* **app:** Android/Kotlin 网络模型、Chaquopy 本地调用封装和股票卡片支持展示 `technical.ma20`、`technical.ma120` 与 `technical.atr_pct`；Python wrapper 增加 `resolved_snapshot` 兜底，确保 App、桌面窗口和快照字段缺失时仍能解析最近一次可用技术指标。
* **monitor:** 新增板块恐慌性杀跌保护和上传链路加固；已持仓纪律复核会识别同板块共振下跌、个股相对板块 MAD Z 值和继续持有证据，避免把板块级急跌机械解读成个股必须止损。

### Improvements

* **monitor-window:** 配置兜底快照里的 `technical.ma20/ma120/atr_pct` 改为先读委员会/监控缓存，缺失时只读本地 `db/market_data.db` 计算；桌面窗口刷新不再调用历史行情 provider，不触发联网、行情同步或 DB 写入。
* **market-data:** A 股实时与历史行情优先使用腾讯 `qt` 接口，保留新浪作为实时行情兜底，降低单一行情源失败导致窗口和委员会缺价的概率。
* **sync:** 支持完整配置集与 SQLite 数据库的无缝同步，为迁移到另一台电脑时同步持仓配置、周度参数和本地账本数据提供统一路径。
* **alerts:** 强化买卖提醒和交易护栏：现金/风险约束、反向交易重新触发价格线、涨停买入拦截、通知分级和执行状态展示保持同一套口径。

### Bug Fixes

* **db:** 修复空 ledger 同步时误清空或覆盖 config 的风险；当账本没有可同步持仓时保留现有配置，避免迁移或启动阶段把真实持仓快照冲掉。

### Docs

* **wiki/readme:** 按最近 git log 重新补充 7 月 4 日的 App 技术指标展示、主窗口只读技术指标兜底、板块恐慌保护、上传加固、ledger 空同步保护和行情源切换说明。
* **app-interface:** 明确 `technical.ma20/ma120/atr_pct`、`resolved_snapshot` 和 `sector_panic_guard` 都是向后兼容的可选字段；旧 App 可忽略，新 App 可用于卡片展示和风险解释。

## Unreleased (2026-06-27)

### Improvements

* **android:** 新增可选后台悬浮信号岛；App 退到后台后展示自动刷新/委员会分析进度，触发买卖信号时用紧凑悬浮卡片提醒，并可从设置关闭。
* **android:** App 交易模式 `主动盈利/现金回收/主动避险` 透传到本地 Chaquopy 委员会入口，Python 侧保持尾部可选参数默认值，旧版调用仍兼容。
* **exit-policy:** 手机端/桌面端止盈止损参数读取兼容 `sector_exit_policies` 与旧 `sector_policies` 字段，避免不同同步路径写入字段名不一致导致板块参数回退到全局默认值。
* **weekend-news:** 国内热点新闻板块龙头改为“内置行业锚点 + AkShare 动态成分股”合并去重，避免实时概念排行覆盖掉工业富联、胜宏科技等明确龙头。
* **committee:** 将 `TRIM` 明确拆成风控型减仓与战术型减仓；`stop_loss/bearish/risk/drawdown/exit_policy` 不再因为缺少买回点被强制降级 `HOLD`，只有 `range_trade/take_profit_reentry/swing` 这类高抛低接动作仍要求低于现价的 `REENTRY_PRICE`。
* **sell-alerts:** 已持仓 `SELL/TRIM` 的提醒选择加入持仓风险释放模型，综合委员会置信度、建议卖出比例、保守卖出胜率、卖出后路径效用和当日走弱程度，减少强卖出信号被长期压在候选态的问题。
* **decision-optimizer:** 板块级周度止盈止损优化默认回看最近 62 天，并把 `sell_utility_adjustment_pct`、`sell_reliability`、`sell_evidence_score` 写入策略；确定性优化器对已有持仓按 Wilson 下界、卖出后路径净优势和样本可靠性折减继续持有的 30 日期望收益，避免高风险持仓只降为 `HOLD` 而不触发减仓。
* **exit-policy:** 周度板块映射改为多源合并，东方财富直连/AkShare 只返回部分标的时继续用 `data/sector_cache.json` 和本地配置补缺；新增 `sample_quality` 并对薄样本板块收缩卖出效用，减少“未分组”和单票过拟合。
* **monitor-window:** 双击标的单独运行委员会分析后，会同步更新主窗口行、操作优先级排序、最后更新时间和 `data/market_monitor/latest_window.json`；明确 `SELL + 负金额 + 已持仓` 直接进入执行提醒，普通 `TRIM` 仍保持待确认。
* **monitor-window:** 委员会详情弹窗改为按信息优先级渲染：最高风险内容红色加粗，主要正向证据绿色加粗，背景与辅助信息灰色显示，并删除低价值泛化提醒。
* **monitor-window:** 桌面窗口新增真实账户现金/T+2修正入口，写入路径统一为 `AccountLedger(real).correct_cash()`，再由 ledger 即时同步本地配置和窗口快照。
* **market-monitor:** 新增交易模式 `主动盈利`、`现金回收`、`主动避险`；模式只影响提醒优化层的现金保留、买入阈值、买入手数和卖出执行门槛，不改委员会原始 verdict 和接口旧字段。
* **sell-alerts:** 已持仓 A 股新增 `position_exit_discipline_review` 纪律复核路径；止损即时复核，止盈/减仓连续确认后按“历史胜率折减后的纪律卖出期望 - 委员会继续持有证据 - 交易摩擦”决定是否进入执行提醒，避免 `HOLD` 静默吞掉已确认纪律触发，也避免把止盈止损线当成 100% 可靠的机械卖点。
* **market-monitor:** 盘中监控运行时读取 `data/sector_cache.json`，把 A 股标的板块统一成周更优化使用的东方财富板块，并把原配置板块保存在 `config_sector`；委员会、窗口、告警分布和 `position_exit_plan` 参数读取不再因为 config/ledger 旧板块名回退到全局参数。

### Bug Fixes

* **committee:** 修复 `HOLD + 负 suggested_alloc_cny` 的语义冲突；被后处理强制 `HOLD` 时同步归零建议金额，避免接口语义和窗口显示互相矛盾。
* **monitor-window:** 修复最新委员会正文已经显示卖出，但顶部摘要仍停留在“待确认卖”的弹窗刷新问题。
* **monitor-window:** 修复 `HOLD + 已确认止盈/止损纪律触发` 被展示成普通观察的问题；现在会显示“止盈复核/止损复核”或具体“止盈卖N手/止损卖N手”，并在可选字段中保留原始委员会 verdict。

### Docs

* **wiki/readme:** 补充 TRIM 类型边界、卖出提醒模型、单标的手动委员会分析回写主窗口快照、详情弹窗颜色语义和 App 接口兼容说明。
* **wiki/readme:** 补充交易模式、ledger-first 现金修正和模式字段向后兼容说明。
* **wiki/readme:** 补充周度卖出效用参数的 62 天校准、Wilson 下界收缩和“只影响已有持仓、不影响入场点、不破坏 App 接口”的边界。
* **wiki/readme:** 补充行业映射多源补缺和 `sample_quality` 对卖出效用的影响。
* **wiki/readme:** 补充持仓纪律复核的期望效用公式、历史胜率可靠性折减和 App 可选字段兼容说明。
* **wiki/readme:** 补充东方财富板块缓存如何同时服务周更优化、盘中委员会、窗口展示和止盈止损参数读取。

## Unreleased (2026-06-19)

### Improvements

* **committee:** 盘中监控每个标的一次调用同时生成真实账户和委员会影子账户两套独立评估；真实账户结果用于窗口展示和用户决策，影子账户结果只给账本自动执行与后验胜率复盘使用。
* **accuracy:** 吸收上游 2026-06-16 到 2026-06-23 的公开命中率红线：样本数低于 30 时保留 hit/total，但不公开具体 hit rate，避免小样本胜率被误读为稳定模型能力。
* **sell-alerts:** A 股卖出提醒从固定 `45` 分阈值改为价格触发、Wilson 胜率下界、保守卖出期望、卖出后路径效用和板块策略质量共同校准；已触发持仓纪律线的卖出提醒可降低门槛，未触发价格线的委员会卖出只显示为“候选卖”，避免误判为普通观察。
* **exit-policy:** 周度止盈止损优化新增卖出后 5 日路径效用，度量“规避回撤 - 错过反弹”，并写入板块级参数 JSON，避免只看卖出频率或裸胜率导致卖点过早。

### Refactor

* **market-monitor:** 将 `jobs/market_monitor.py` 从单文件大模块拆成入口 facade + 行情、通知、买卖点、风控护栏、告警优化、窗口快照和运行编排等职责模块，保留旧 `jobs.market_monitor` 导入路径兼容。
* **exit-policy:** A 股止盈止损周度优化改为风险调整期望效用目标，卖出胜率使用 Wilson 下界和样本可靠性收缩，避免小样本裸胜率误导参数选择。
* **account-ledger:** `db.account_ledger.AccountLedger` 成为真实持仓的单一可信源；用户交易、手动增删关注、现金/T+2修正优先写 `real` 账本，再由默认生产账本即时同步 `jobs/market_monitor_config.json` 和 `data/market_monitor/latest_window.json`。

### Bug Fixes

* **market-monitor:** 修复 A 股持仓纪律止损的边界触发，持仓止损改为跌破锁定止损线才触发，避免价格刚好等于止损价时误报；止盈仍按达到目标价触发。
* **monitor-window:** 修复委员会给出 `TRIM/SELL` 但未被现金/风险提醒层选中时主窗口仍显示“观察”的歧义，改为显示“候选卖/候选买”并在快照中保留未触发锁定出场线的原因。
* **exit-policy:** 东方财富行业映射新增浏览器请求头直连和 `data/sector_cache.json` 本地缓存；接口断连时先读缓存，再回退本地配置板块。
* **committee:** 修复双账户重构时漏掉 regime 概率/条件收益统计导致优化器引用未定义变量的问题，并移除重复 asset 构造。
* **monitor-window:** 选股加入关注和主窗口取消关注改为写入 `real` 账本，不再绕过账本直接改配置文件。
* **account-ledger:** 临时/测试账本禁用外部 config/snapshot 同步，避免测试数据库或工具脚本污染真实持仓配置。

### Docs

* **wiki:** 更新架构和执行路径文档，补充盘中监控的数据流、模块职责和止盈止损/入场出场边界，方便新对话快速定位代码逻辑。
* **exit-policy:** 补充 `policy_quality_score`、保守卖出胜率、卖出后路径效用、行业映射缓存、卖出提醒阈值诊断和委员会影响边界说明。
* **accounts:** 补充真实账户单源同步、影子账户评估隔离和上游 changelog 可借鉴的胜率数学纪律。

## Unreleased (2026-06-14)

### Features

* **daily-selection:** 新增交易日 11:30 / 15:00 的 A 股日度选股任务，结合新闻主题、板块资金流、基本面、技术面和现金可买约束输出候选标的。
* **monitor-window:** 独立桌面监控窗口改为卡片堆叠展示，支持标的、周末新闻、日度选股原因的悬浮详情，并展示最后更新时间、现金占比和持仓手数。
* **startup:** 重写本地启动入口，使用可移植的 Python 启动器自动处理项目路径、日志目录、已有进程、后端服务、调度器和桌面窗口。

### Improvements

* **committee:** LLM 工具改为面向 A 股/港股/国内指数和国内宏观数据，历史行情在 AkShare 不可用时自动回退到本地市场 provider，并明确提示未覆盖标的。
* **committee:** 决议快照里的宏观上下文改为上证指数、北向资金、人民币汇率和中国 10 年期国债，减少对海外 VIX/TNX/yfinance 风格数据的依赖。
* **committee:** Regime brief 输出改为中文指标名和阈值名，缓存长度扩展到 500 字，减少数学模型解释被截断或难以阅读的问题。
* **macro:** 国内宏观快照新增 5 分钟缓存，并优化中国 10 年期国债收益率和北向资金获取逻辑，降低同一轮监控重复抓取耗时。
* **startup:** 双击启动脚本和 Python 启动器支持读取项目 `.env`，可通过 `OPENINVEST_PYTHON`、`OPENINVEST_UV`、窗口/后端开关等变量完成本机启动配置。
* **daily-selection:** 吸收上游路径概率、walk-forward 校准和波动防御思路，为每只候选 A 股输出 1/5/20 日收益路径分布、ATR 风险、防守惩罚和历史相似样本命中率。
* **monitor-window:** 新增主窗口 `+` 悬浮交易面板，列出持仓和关注标的，可切换买卖、调整手数并按最新价写入真实账户。
* **monitor-window:** 双击标的后的委员会分析改为更适合新手阅读的结论、买点、风险线、仓位建议和下一步提示。
* **monitor-window:** 选股原因气泡自动识别已在持仓或关注列表的标的，手动交易面板最多展示 20 个标的并扩大可视高度。
* **monitor-window:** 标的堆叠背景按状态显示红/橙/蓝/灰，并在每次数据更新后回到按操作优先级排序的第一张卡片。
* **monitor-window:** 标的卡片改为双击打开委员会分析，并在后台获取最新价格和分析结果前显示缓冲进度条。
* **monitor-window:** 窗口数据改为跟随委员会/选股/新闻输出文件变化主动刷新，避免重复定义业务刷新频率和整窗闪烁。
* **daily-selection:** 增强板块大资金流向兜底数据源，并过滤资金不足以买入一手的候选股票。
* **weekend-news:** 周末新闻抓取流程改为直接调用 Python 逻辑，减少对本地 HTTP 后端端口的依赖。

### Refactor

* **connectors:** 删除废弃 IM Bot、连接检查脚本、启动开关和相关测试/文档，保留桌面窗口与 Web/API 作为交互入口。
* **monitor-window:** 将 `monitor_desktop_window.py` 拆分为常量、数据服务、文案格式化、通用组件、新闻弹层、选股弹层、手动交易弹层和委员会分析弹层模块，降低单文件维护压力。

### Bug Fixes

* **fundamental:** 过滤连接失败和接口限流类低价值 warning，避免基本面模型把数据源噪声误当作交易风险输入。
* **scheduler:** 将任务 misfire 补跑宽限从 10 分钟扩展到 1 小时，降低重启后错过关键定时任务的概率。
* **startup:** 启动前旧进程清理改为按 OpenInvest 后台模块名识别，避免 `python -m jobs.market_monitor` 等命令行不包含项目路径时漏杀旧进程。
* **market-data:** 关闭 AkShare tqdm 和市场 provider 刷新打印，减少桌面窗口和启动日志中的噪声输出。
* **daily-selection:** 将日度选股调度拆成交易日 11:30 和 15:00 两个任务，修正原 cron 实际在 15:30 执行的问题。
* **monitor-window:** 修复选股气泡和手动交易面板滚轮事件被子控件吞掉的问题，并统一滚动条、列宽、执行按钮和悬浮圆按钮尺寸。
* **monitor-window:** 修复选股气泡窗内容过多时“加入关注列表”按钮被挤出可视区域的问题。
* **monitor-window:** 关闭桌面主窗口时同步停止本项目后台监控、调度、周末新闻和日度选股任务，避免窗口关闭后继续抓新闻。
* **weekend-news:** 旧版 Windows 消息框默认关闭，周末新闻结果只写入本地文件并由主窗口悬浮卡片展示；需要恢复时可设置 `INVEST_WEEKEND_NEWS_POPUP=1`。

### Security

* **repo:** 保持 API key、持仓配置、账户数据库、生成数据和日志文件在 git 忽略范围内，本次提交不包含这些敏感/本地数据文件。

### Docs

* **readme:** 更新当前启动方式、独立桌面窗口、可选 8766 后端、周末新闻展示和交易日日度选股说明。

## [0.5.0](https://github.com/longsizhuo/openInvest/compare/v0.4.0...v0.5.0) (2026-05-30)


### Features

* **logging:** ADR-014 生产代码 print→log 迁移 + RotatingFileHandler ([#21](https://github.com/longsizhuo/openInvest/issues/21)) ([b93a42c](https://github.com/longsizhuo/openInvest/commit/b93a42cd5eab758af80cb0f24efab056fc0d15cc))
* **regime:** 概率表/买回点数据源换成几十年 OHLC 直算（替代 verdict_review 276 条） ([d187faf](https://github.com/longsizhuo/openInvest/commit/d187fafe5dc233cb66e20f96572f71e6b2792884))


### Bug Fixes

* **committee:** backup_cny 读对 key + 抽 load_backup_cny 单一可信源 + force-HOLD 归零 alloc ([1e96d3e](https://github.com/longsizhuo/openInvest/commit/1e96d3ea103d59d69250f284cc44be49507954ac))
* **committee:** review fixes — store 未定义、solvency 拼写、Sanity4 confidence/alloc ([2888c44](https://github.com/longsizhuo/openInvest/commit/2888c440afa911c42712e6bb157f31f567a9edca))
* **regime:** 重叠窗口用 effective_n 判 low_confidence + forward-return correctness 测试 ([94c32d3](https://github.com/longsizhuo/openInvest/commit/94c32d361388bfd0c5ccf4e34c30d3ea079f4ec0))
* **sweep:** regime 阈值验证读全量历史，去掉 get_history_data 730 天 cap ([9fbf9b7](https://github.com/longsizhuo/openInvest/commit/9fbf9b7e8fe0cfcc9c961f8ff7d1f2bcdeab5665))

## [0.4.0](https://github.com/longsizhuo/openInvest/compare/v0.3.0...v0.4.0) (2026-05-28)


### Features

* **committee:** SOLVENCY=strong 时集中度不触发 TRIM（确定性后处理） ([82a2ec1](https://github.com/longsizhuo/openInvest/commit/82a2ec153d9888251e742699f325fec2754ee8f9))
* **committee:** TRIM 路径化 — 卖出后路径 + 买回点，给不出更低买回点则降级 HOLD ([dcbaa74](https://github.com/longsizhuo/openInvest/commit/dcbaa7419d96df49f9200d3c1db44ab7c8b3cfa8))
* **probability:** regime 概率表 — 按 (asset, regime) 给历史 forward return 分布 ([5f209de](https://github.com/longsizhuo/openInvest/commit/5f209de2c7f122db3dab51361df4d7ef1cbcc7ef))


### Bug Fixes

* **cio:** TRIM 约束字段名对齐 + 明确覆盖通用 TRIM 规则 ([305b162](https://github.com/longsizhuo/openInvest/commit/305b162e46548a85da902fc0ee420c7f8968e5b8))
* **cio:** TRIM 阈值改走 config 注入，消除魔法数字 ([a8578c3](https://github.com/longsizhuo/openInvest/commit/a8578c31e5fc23f9597e297ffe3bd7b386797835))
* **cio:** 零花钱账户小幅浮亏禁止 TRIM ([560fb6a](https://github.com/longsizhuo/openInvest/commit/560fb6a680df593221e601a16ba70175b892f74b))
* **config:** env override 多词 section 解析 + per-asset 支持 + CR 修复 ([52392de](https://github.com/longsizhuo/openInvest/commit/52392de1196b04ce4dbd7c55d3500dc2b401e813))
* **dreaming:** LLM REJECT 从 candidates.json 移除 + prompt 加 uptrend 怀疑清单 ([958430d](https://github.com/longsizhuo/openInvest/commit/958430d4a95fae3959bc904f5be175e8c7f6a06e))


### Refactor

* **cio:** TRIM 约束阈值默认 0（禁用），等 sweep OOS 验证后再启用 ([f7313e1](https://github.com/longsizhuo/openInvest/commit/f7313e128e00f580518e7832e13860c493124c83))


### Docs

* **adr:** ADR-011 HOLD Oracle 语义——hold_wrong 只判下跌方向 ([4201273](https://github.com/longsizhuo/openInvest/commit/4201273deb7ad9972a98e227e49a573a57a545cb))

## [0.3.0](https://github.com/longsizhuo/openInvest/compare/v0.2.0...v0.3.0) (2026-05-27)


### Features

* **config:** 50+ 参数 config 化，sweep runner + ADR-010 ([6a65680](https://github.com/longsizhuo/openInvest/commit/6a6568044788d1582b63d0a491bfef75b8404a46))


### Bug Fixes

* **web-api:** 修复 3 个生产风险：非原子交易、DB crash-loop、取款竞态 ([596590e](https://github.com/longsizhuo/openInvest/commit/596590ed3b8fb01d796ce79acc019d50f8171c0d))


### Docs

* **adr:** 新增 ADR 009 用户纪律承诺模板（理由段待本人填） ([e505c7c](https://github.com/longsizhuo/openInvest/commit/e505c7c1e21f81b330aa148d72f2615b084e85c2))

## [0.2.0](https://github.com/longsizhuo/openInvest/compare/v0.1.0...v0.2.0) (2026-05-27)


### Features

* **committee:** 指标修正 + regime 双触发器/recovery + dreaming lift-based caution + backtest 防穿越修复 ([88700fa](https://github.com/longsizhuo/openInvest/commit/88700fadf92e6fc641ea1179cfc93a04c509b861))


### Bug Fixes

* **dreaming:** LLM 验伪构造 payload 用 c["action"] 崩溃 ([ad9d964](https://github.com/longsizhuo/openInvest/commit/ad9d964ac8daccbcecd4f76bc8124c6f75c8f23c))
* **event-watch:** _run_committee_task 跑完补发 verdict 邮件 ([02362c1](https://github.com/longsizhuo/openInvest/commit/02362c1acdc618ceb7400a58378357595a5d86e1))
