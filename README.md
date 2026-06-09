# openInvest

自部署的 AI 投资委员会与盘中监控工具。

openInvest 的目标不是替你下单，而是把投资决策过程变得可追踪、可复盘、可验证：多个 LLM 角色独立讨论标的，确定性模型负责约束仓位、风控和执行条件，本地账本记录真实账户与委员会影子账户的差异。

本项目仅用于研究、复盘和个人决策辅助，不构成投资建议。

## 当前能力

- 多角色投资委员会：Macro Strategist、Quant Analyst、Risk Officer、CIO 输出 BUY / ACCUMULATE / HOLD / TRIM / SELL。
- 国内热点新闻发现：抓取国内新闻源和热榜，不限于股票新闻，提炼事件、A 股板块、主题和候选龙头股。
- 危险新闻量化：对地缘冲突、制裁、供应链、政策、宏观冲击等明显危险信息，输出可解释的 A 股影响估计。
- 双账户账本：`real` 只记录用户明确告知的真实成交，`committee` 按委员会建议做影子执行。
- 盘中监控：拉取行情，跑持仓和自选标的委员会，过滤不可执行提醒，拦截涨停追买和重复同向影子交易。
- 买卖点模型：计算回调买点、突破买点、止损、止盈、减仓、再入场、CVaR、ATR、收益风险比。
- SMC 回测：支持 swing、BOS/CHOCH、FVG、流动性 sweep、ATR 止损、RR 止盈；A 股默认只做多。
- PnL 快照：按日记录真实账户和委员会账户的收盘后盈亏，并生成基准对比图。
- Web/API：提供委员会、账户、PnL、SMC 回测、系统规则、历史决策等接口。

## 目录结构

```text
agents/       LLM 角色 prompt 和 SDK agent 封装
backend/      盘中监控使用的直接委员会 API
connectors/   GUI/API 桥、NapCat bot、浏览器侧接口
core/         委员会编排、优化器、买卖点、SMC 回测
db/           SQLite 账本：账户、交易、insights、events
jobs/         盘中监控、周末新闻、PnL 快照等任务
services/     新闻源、国内热榜增强、通知服务
scripts/      CLI、回测、诊断、验证工具
docs/         Wiki、ADR、API 和基准说明
tests/        单元测试和回归测试
```

## 快速开始

要求：

- Python `3.13+`
- `uv`
- 建议安装 Playwright Chromium，用于国内新闻源抓取

安装：

```powershell
git clone https://github.com/longsizhuo/openInvest.git
cd openInvest
uv sync
uv run playwright install chromium
Copy-Item .env.example .env
```

编辑 `.env`，至少配置一个 OpenAI 兼容 LLM：

```env
LLM_API_KEY=
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-v4-flash
```

旧的 `DEEPSEEK_*` 变量仍然兼容，但推荐使用新的 `LLM_*`。

## 本地敏感文件

不要提交这些文件或目录：

```text
.env
jobs/market_monitor_config.json
data/
logs/
backend.log
backend_err.log
*.db
```

`jobs/market_monitor_config.example.json` 是盘中监控配置模板。真实的 `jobs/market_monitor_config.json` 会包含现金、持仓、自选股和账户同步设置，必须留在本地。

## 运行入口

启动 GUI/API 桥，默认端口 `8765`：

```powershell
uv run uvicorn connectors.web_api:app --host 127.0.0.1 --port 8765
```

启动盘中监控使用的直接委员会后端，默认端口 `8766`：

```powershell
uv run uvicorn backend.server:app --host 127.0.0.1 --port 8766
```

运行盘中监控：

```powershell
uv run python -m jobs.market_monitor
```

运行周末或非交易时段国内新闻机会发现：

```powershell
uv run python -m jobs.weekend_news_crawl
```

生成或刷新 PnL 快照：

```powershell
uv run python -m jobs.pnl_snapshot
```

## 关键接口

直接后端，默认 `http://127.0.0.1:8766`：

- `POST /api/committee`：对单个标的运行投资委员会。
- `GET /api/accounts`：查看真实账户和委员会账户。
- `GET /api/accounts/trades`：查看账户交易流水。
- `POST /api/accounts/real/trades`：记录用户明确执行的真实成交。
- `POST /api/accounts/snapshot`：写入日度收盘 PnL 快照。
- `GET /api/accounts/pnl`：查看账户 PnL 历史。

GUI/API 桥，默认 `http://127.0.0.1:8765`：

- `POST /api/committee/run`：运行 GUI 使用的委员会任务。
- `GET /api/committee/{task_id}`：轮询委员会任务状态。
- `GET /api/committee_sessions`：查看历史委员会记录。
- `POST /api/backtest/smc`：运行 SMC 策略回测。
- `GET /api/accounts`、`GET /api/accounts/pnl`：账户与 PnL 视图。
- `GET /api/regime_rules`：查看硬规则、角色 prompt 和系统工具元数据。

## CLI 示例

SMC 回测：

```powershell
uv run python -m scripts.backtest_smc 300001 --period 2y
uv run python -m scripts.backtest_smc 600150 --period 1y --risk 0.5 --json
```

SMC 默认 `allow_short=False`，符合普通 A 股交易约束。只有研究可做空市场时才使用 `--allow-short`。

常用测试：

```powershell
uv run pytest tests/test_smc_backtest.py -q
uv run pytest tests/test_account_ledger.py tests/test_market_monitor_entry_exit_alerts.py -q
uv run pytest tests/test_news_sources.py -q
```

## 决策流程

1. 收集行情、基本面、宏观环境、国内新闻和组合状态。
2. 委员会角色独立输出观点，CIO 生成 memo。
3. 确定性优化器根据现金、手数、regime、概率、基本面和风控约束修正仓位。
4. 买卖点模型附加回调买点、突破买点、止损、止盈、减仓和再入场价格。
5. 可选的 LLM 审核只评价优化器结果，不重新编造价格和仓位。
6. 执行护栏拦截涨停追买、重复同向影子交易、低质量或不可执行提醒。
7. 委员会账户记录系统会怎么做，真实账户只在用户明确录入成交后变化。

## 双账户账本

`db/account_ledger.py` 维护两个账户：

| 账户 | 可信来源 | 更新方式 |
| --- | --- | --- |
| `real` | 用户明确告知的真实成交 | `apply_user_trade` 和 `/api/accounts/real/trades` |
| `committee` | 委员会影子执行 | `apply_committee_result` |

`sync_real_account_from_config` 只用于初始化和刷新元数据，不会覆盖已有真实账户的股数、均价或现金。这样可以长期比较：委员会指标是否真的优于用户真实执行。

## 新闻机会发现

`jobs.weekend_news_crawl` 的目标是寻找下一个可能的热门股票，而不是只评估当前持仓。它会：

- 读取 `data/weekend_news/` 下的新闻缓存；
- 合并国内热榜、新闻源和 RSS；
- 调用 LLM 提炼 A 股板块、主题、催化和候选龙头；
- 输出机会列表和需要委员会复核的标的；
- 运行结束后弹出本地摘要。

危险新闻量化是辅助判断模型，不是收益承诺。

## 回测和研究边界

回测只能说明某组规则在历史 OHLCV 上会如何运行，不能证明未来盈利。

已知限制：

- LLM 委员会回测可能受到模型训练数据截止日影响，存在知识层面的 lookahead。
- SMC 回测避免同 K 线前视偏差，但仍是对 SMC 概念的简化实现。
- 盘中提醒依赖行情可用性和本地配置质量。
- A 股真实执行还受手数、T+1、涨跌停、停牌和流动性约束，需要人工复核。

## 文档

- 架构：`docs/wiki/01-architecture.md`
- 角色：`docs/wiki/02-agents.md`
- 执行路径：`docs/wiki/04-execution-paths.md`
- 数据模型：`docs/wiki/05-data-model.md`
- API：`docs/wiki/06-api.md`
- 故障排查：`docs/wiki/09-troubleshooting.md`
- ADR：`docs/wiki/adr/`

## 免责声明

openInvest 是 LLM 驱动的研究和决策辅助工具。它可能出错、过期、过度自信或遗漏信息。它不提供金融、法律或税务建议。

所有投资决策和交易指令都由你负责。历史表现、回测结果、模型评分和 PnL 曲线都不代表未来收益。
