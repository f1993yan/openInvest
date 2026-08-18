# 港股时空动量代理

## 1. 定位

港股生产决策使用 `core/hk_spatio_temporal_factor.py`。该模型是 [Spatio-Temporal Momentum (arXiv:2302.10175)](https://arxiv.org/abs/2302.10175) 启发的透明线性代理，不是论文神经网络的复现。

它只处理 `market=hk`。A 股继续使用 `core/ashare_behavioral_factor.py`，两者拥有独立请求字段、状态文件、调仓周期和优化器入口。

## 2. 点时公式

每只港股使用已完成日线计算：

```text
temporal = (
    ret20 / max(vol20, 0.08)
    + ret60 / max(vol60, 0.08)
    + ret120 / max(vol60, 0.08)
) / 3

score = 0.55 * percentile_rank(temporal)
      + 0.45 * percentile_rank(ret60)

eligible = ret20 > 0 and ret60 > 0
```

其中 `vol20`、`vol60` 是按 252 个交易日年化的收益波动率。生产排名池使用与冻结回测一致的 2026-08-18 恒生指数 93 只公开成分股，并合并用户列表中不属于该指数的港股；随后只把用户持仓/关注标的的评估返回界面。公开参考池不会自动加入关注列表或真实持仓。排名完成后再应用正动量门槛；门槛失败的得分为 0。

组合规则：

- 按分数选前四只符合门槛的港股。
- 使用 20 日波动率倒数分配目标权重。
- 总目标暴露上限 95%，单只上限 35%。
- 目标成员和权重每 20 个已完成港股交易日更新一次。
- 20 日内分数和校准收益可以更新，但目标成员与目标权重保持冻结。

## 3. 预期收益与优化器

历史样本按 20 个交易日取一次点时截面，记录随后 20 日收益。当前得分取最近的 40 个历史得分样本，并与全样本均值做经验贝叶斯收缩：

```text
mu = (n * local_mean + 20 * prior_mean) / (n + 20)
```

结果限制在 `[-15%, +15%]`。`20` 个先验样本对应一个完整的年度调仓周期，用于抑制小样本极端值。优化器约束权重为：

```text
optimizer_weight = 0.55 + 0.45 * n / (n + 20)
```

有效港股评估会替换旧 `_estimate_expected_return_pct()` 的 regime、RSI、价格分位和 LLM 方向回退，同时替换战略目标仓位。基本面只保留最多 `+/-1.5` 个百分点的收益修正，交易成本、离散手数、现金和集中度约束继续生效。

冻结目标中 `selected=false` 的港股禁止产生买入候选，即使它当日重算的局部校准收益为正也不例外；已有持仓仍可向 0 目标减仓。这样实际候选集合与回测的“只持有前四”一致。

如果横截面少于四只有效历史，或校准样本少于 20，评估标记 `low_confidence=true`。生产 Direct 路径返回 `WAIT` 和零建议金额，不会悄悄退回旧港股技术模型。

## 4. 状态与接口

```text
jobs.market_monitor_runtime
  -> jobs.market_monitor_quotes.build_hk_spatio_factor_context
  -> core.hk_spatio_temporal_factor.assess_hk_spatio_universe
  -> backend.server.CommitteeRequest.hk_spatio_factor
  -> core.decision_optimizer.optimize_committee_decision
  -> CommitteeResponse.hk_spatio_factor
  -> data/market_monitor/latest_window.json
```

调仓状态写入被 Git 忽略的 `data/hk_spatio_factor_state.json`。它不读取或写入 A 股的 `data/behavioral_factor_state.json`。

两年日线统一通过 `utils.market_data_provider` 读取并进入 SQLite 日线缓存。首次启用需要补齐公开参考池，之后盘中轮次复用日线缓存，不应每十分钟重新下载 93 只股票的完整历史。

`selected=true` 只表示进入港股模型目标组合，不表示真实持仓；`represents_account_holding` 始终为 `false`。

## 5. 回测边界

冻结回测使用 2026-08-18 时点的 93 只恒生指数成分，90 只有足够历史，区间为 2024-05-08 至 2026-08-17。时空动量代理总收益 `+164.96%`、年化 `+55.16%`、最大回撤 `-25.35%`、Sortino `2.08`；最后 126 个交易日验证收益 `+9.93%`，相对等权持有超额 `+14.38` 个百分点。

这些结果不能解释为未来收益保证：

- 当前成分股回看历史存在生存者偏差。
- 最大回撤差于等权持有的 `-19.75%`。
- 全期换手率约 `3971%`，对费用和执行质量敏感。
- 回测证明的是当前线性代理与组合包装，不是论文神经网络本体。

公式或参数变更前，必须重新运行冻结样本、费用压力和最终验证窗口，并保留点时不变性测试。
