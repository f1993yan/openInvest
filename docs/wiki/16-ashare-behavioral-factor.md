# A 股行为因子生产基座

[← 回 Wiki 索引](README.md)

## 1. 解决什么问题

旧 A 股确定性回退把 regime、30 日动量、MA20/MA120、价格分位、RSI 和弱 LLM 先验直接相加。历史对照中它容易产生高换手，且单标的技术信号不能回答“当前横截面应该持有哪些股票”。

生产基座现在把 A 股改为可审计的横截面行为因子。LLM 仍负责观点和审核，基本面、止盈止损、现金、手数、T+1、涨跌停等仍是约束；因子只替换 A 股的确定性收益和目标仓位基座。非 A 股不受影响。

## 2. 因子公式

对同一时点的 A 股样本池做百分位排名：

```text
Momentum = 0.35 Rank(R60)
         + 0.30 Rank(R120)
         + 0.20 Rank(R252)
         + 0.15 Rank(R20)

BehavioralScore = 0.65 Momentum
                + 0.25 Rank(-VolumeMean20 / VolumeMean120)
                + 0.10 Rank(-Volatility20)
```

资格门：

```text
R60 > 0  且  Close > EMA100
```

合格标的按 `BehavioralScore` 取前四。目标仓位按 20 日年化波动率的倒数分配，总风险资产上限 95%，单股上限 35%。基本面可以对期望收益做有界调整，但不能再次放大行为因子的 35% 目标上限。

## 3. 每标的独立优化器权重

优化器权重不是所有股票共用一个常数。每只股票回看最近 63 个交易日，历史模拟以 5 个交易日为一个目标持有块：

```text
入选前四：payoff = stock_return - cross_section_mean_return
未入选：  payoff = cross_section_mean_return - stock_return
```

这样衡量的是因子对该股票的排序能力，而不是市场普涨带来的裸收益。令样本数为 `n`，样本日均值为 `mean`，样本波动为 `sigma`：

```text
reliability = n / (n + 20)
posterior_mean = reliability * mean
posterior_se = max(sigma, 0.5%) / sqrt(n + 20)
p_positive = NormalCDF(posterior_mean / posterior_se)

optimizer_weight = 0.55 + 0.45 * reliability * p_positive
```

最终权重限制在 `[0.55, 1.0]`。它表示优化器服从该标的行为因子目标仓位的强度，不是目标仓位本身，也不是买入手数。

响应审计字段包括：

- `optimizer_weight`
- `trailing_3m_factor_return_pct`
- `trailing_3m_hit_rate`
- `trailing_3m_sample_size`
- `components.trailing_3m_p_positive`

委员会面向用户的综合结论会把行为因子放在普通技术证据之前，显示“因子分数、前四状态、目标仓位”和“近三个月因子收益、命中率、样本数、优化器权重”。Android 弹窗使用同一组字段单独展示。`optimizer_weight` 是优化器服从因子目标的强度，`target_weight_pct` 才是仓位，两者不得在 UI 中互换。

桌面主窗口和 Android 主界面会单独列出“行为因子目标前四”，并在对应标的卡片显示模型目标仓位。该区域点击后打开对应标的，固定标注“模型目标，不是持仓”；不得依据 `selected=true` 修改或描述真实账户持仓。

## 4. 收益校准与调仓

行为分数不是直接的收益百分比。系统在可用历史中每 20 个交易日抽取一次点时样本，寻找和当前分数最接近的最多 40 条后续 20 日收益，并用 20 条横截面总体先验等价样本收缩：

```text
expected_return_20d = (n * local_mean + 20 * prior_mean) / (n + 20)
```

生产目标成员和目标权重每 5 个交易日更新，状态文件是：

```text
data/behavioral_factor_state.json
```

该文件包含关注/持仓标的派生出的**因子模型目标组合**，属于本地生成数据，必须保持 git ignored。`targets.<symbol>.selected=true` 只表示标的进入因子目标前四，不表示真实账户或委员会影子账户已经持有。真实持仓只能以 `accounts.db` 的 `real` 账户为准。状态文件和 API 评估同时通过 `selection_scope=factor_model_target_portfolio`、`represents_account_holdings=false`（单标的评估中为 `represents_account_holding=false`）明确这个边界。

五个交易日内保持的是“目标组合成员资格”，审计原因为 `target_membership_frozen_from_...`，不能写成 `held` 或翻译成“仍持有”。当前真实仓位距离模型目标不超过 5 个百分点时，因子动作降为 `HOLD`，降低十分钟委员会重复调仓。可靠的持仓风险卖出可以越过该免交易带。

## 5. 调用链

```text
jobs.market_monitor_runtime.run_monitor_round
  -> jobs.market_monitor_quotes.build_behavioral_factor_context
  -> core.ashare_behavioral_factor.assess_behavioral_universe
  -> backend.server.CommitteeRequest.behavioral_factor
  -> core.decision_optimizer.optimize_committee_decision
```

日度选股由 `core.daily_stock_selector.build_daily_selection()` 复用同一因子。新闻、板块资金和北向资金先生成候选池，行为因子再执行资格门与横截面排名；最终 `stocks`、`reference_pool` 和 `action_pool` 只包含 `behavioral_selected=true` 的前四成员。历史窗口为两年；综合分只负责排列这四只股票，因子占 40%，新闻、板块资金、基本面、路径风险、技术时点和现金可买约束仍保留，但不能把未进因子前四的标的重新加回结果。

周日 `behavioral_factor_calculation` 仍在内部计算持仓与关注列表的完整横截面，以便委员会知道未入选标的的目标仓位为 0；面向用户的 `data/behavioral_factor_assessments.json` 只写入前四，并通过 `candidate_count` / `selected_count` 记录过滤规模。内部状态 `data/behavioral_factor_state.json` 保持完整，不作为选股名单展示。

生产决策不再回退旧技术收益模型。缺少有效因子时，后端先合并目标标的、真实持仓与关注列表补建横截面；仍失败则输出 `WAIT/factor_unavailable`，桌面显示灰色卡片并禁止交易。日度选股会过滤这类候选，并在摘要写入 `factor_unavailable_filtered`。

HTTP/App 接口只新增可选 `behavioral_factor` 字段，原有字段没有删除或改名。旧客户端可以忽略它。Direct 单标的调用如果无法形成至少四只股票的有效横截面，会标记 `low_confidence=true` 并使用兼容回退，不会把单票百分位伪装成前四。

手机本地委员会返回单标的根 JSON，远程任务可能使用 `result.by_asset` 包装。APK 的纯 Kotlin/Gson 解析器同时接受两种结构，并把本轮 `entry_exit_points` 直接交给弹窗，保证行为因子接入不会导致回踩、突破、止损或止盈价格线在展示层丢失。

## 6. 无未来数据规则

- 特征、横截面排名和收益校准共用同一个 `as_of` 截点。
- 三个月权重在历史块起点确定成员，只使用之后一天的收益评价已经形成的信号。
- 任何修改必须通过 `tests/test_ashare_behavioral_factor.py` 的点时不变性测试。
- 日度选股至少读取 2 年历史；少于 252 根有效日线时明确低置信度。

## 7. 回测边界

82 个交易日研究对照（2026-03-12 至 2026-07-10，24 只本地冻结 A 股样本）中，研究版 20 日调仓行为因子得到：

| 指标 | 研究版行为因子 | 当时委员会基座 |
|------|----------------|----------------|
| 总收益 | +63.8573% | -12.9719% |
| 换手率 | 254.6893% | 5449.2038% |
| 最大回撤 | -11.0715% | -21.6536% |
| 交易数 | 17 | 351 |

这不是生产收益承诺。尤其要注意：上述胜出版本每 20 个交易日调仓，而当前生产按用户要求每 5 个交易日更新目标，并叠加委员会、基本面、止盈止损和实际账户约束。两者不是完全相同的策略，不能直接把 `+63.8573%` 写成生产预期。

研究脚本：

```powershell
uv run python scripts/compare_arxiv_factors.py --start 2026-03-12 --end 2026-07-10
uv run python scripts/compare_hybrid_factor.py --start 2026-03-12 --end 2026-07-10
```

报告写入 git ignored 的 `reports/`。迁移电脑时，代码会重建因子状态；若需要保持相同五日调仓锚点，可单独迁移 `data/behavioral_factor_state.json`，但不要提交到公开仓库。
