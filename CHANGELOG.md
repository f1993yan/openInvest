# Changelog

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
