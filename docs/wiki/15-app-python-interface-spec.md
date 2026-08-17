# Android App 与 Python 算法基座接口规范 (App-Python Interface Spec)

为了使 Android 客户端与桌面端、服务端复用完全相同的 Python 投资策略与算法核，且不增加多份代码的维护成本，openInvest 采用了 **Gradle 编译期同步复制 + Chaquopy 运行时进程内调用** 的架构。

本规范固定了 Android App (Kotlin) 与 Python 算法基座之间的对外调用接口。任何后续对 Python 基座的逻辑调整，都应确保以下两个核心入口函数及其参数、返回 JSON 格式的向下兼容性。

---

## 1. 架构与编译期同步机制

1. **统一源头**：Python 算法基座的代码源头位于项目根目录的 `core/`, `utils/`, `agents/`, `db/`, `jobs/` 文件夹中。
2. **编译期拷贝**：在编译 APK 时，`openinvest-core` 模块的 Gradle 任务 `copyPythonSources` 会在 `preBuild` 阶段自动将上述基座文件夹拷贝到 `app/openinvest-core/src/main/python/`。
3. **Git 忽略**：`app/openinvest-core/src/main/python/` 目录已被加入 `.gitignore` 且不包含在 Git 仓库中。这确保了开发者在日常开发中**只需维护一份 Python 代码**（即根目录下的桌面版代码），编译时 Android 客户端会自动拉取最新算法基座进行打包。

---

## 2. 核心接口规范

所有 Python 入口都收拢在 `utils.phone_committee` 模块中，并通过 JSON 字符串传输复杂结构数据。

### 2.1 接口 1: 本地委员会决策 (`run_committee_local`)

Android 客户端通过调用该函数，在手机本地得出投资结论。默认 `decision_mode=algorithm_only` 时不再执行多 Agent LLM 辩论，也不要求 LLM API Key；只有显式切到 `llm_committee` / `llm` 时才走旧委员会链路。

#### 2.1.1 函数签名
```python
def run_committee_local(
    symbol: str,                        # 股票代码 (如 "601138")
    name: str,                          # 股票名称 (如 "工业富联")
    market: str,                        # 市场类型 ("A股", "港股", "美股")
    sector: str,                        # 行业板块
    industry: str,                      # 细分行业
    position_pct: float,                # 当前持仓占比 (0.0 ~ 100.0)
    target_position_pct: Optional[float],# 目标持仓占比
    cost: float,                        # 持仓成本价
    current_price: Optional[float],     # 当前现价 (传入 None 则在 Python 侧通过接口实时拉取)
    total_assets: float,                # 总资产规模 (CNY)
    cash: float,                        # 账户可用现金 (CNY)
    llm_api_key: str,                   # LLM 密钥 (DeepSeek / Gemini 等)
    llm_model: str,                     # LLM 模型名称 (如 "deepseek-v4-flash")
    server_ip: str,                     # 远程服务器 IP (用于拉取宏观、历史等共用数据)
    holdings_json: str,                 # 当前完整持仓列表的 JSON 字符串
    llm_base_url: str = "",             # 可选：LLM API 代理网关地址
    news_brief: str = "",               # 可选：最新新闻简讯 (若传入则作为舆情输入)
    min_lot_size: int = 100,            # 最小交易单位 (A股默认为 100)
    t_plus_1: bool = True,              # 是否受 T+1 交易限制约束
    available_cash: float = 0.0,        # 实战账户可用现金 (用于计算购买力)
    t2_pending_cash: float = 0.0,       # T+2 待入账现金
    optimizer_review_enabled: bool = True,# 是否开启 CIO 决策优化器校验
    max_debate_rounds: int = 4,         # 多 Agent 最大辩论轮数
    server_port: str = "8765",          # 远程服务器端口
    change_pct: float = 0.0,            # 今日涨跌幅百分比 (如 5.3 代表 +5.3%)
    trading_mode: str = "active_profit",# 可选交易模式: active_profit/cash_recovery
    ma20: Optional[float] = None,        # 可选：快照传入20日均线
    ma120: Optional[float] = None,       # 可选：快照传入120日均线
    atr_pct: Optional[float] = None,     # 可选：快照传入ATR百分比
    decision_mode: str = ""             # 可选：algorithm_only / llm_committee / auto
) -> str:                               # 返回结果 JSON 字符串
```

#### 2.1.2 成功返回 JSON 结构 (`success: true`)
```json
{
  "success": true,
  "symbol": "601138",
  "name": "工业富联",
  "market": "A股",
  "verdict": "BUY",                     // 决策结论: BUY, ACCUMULATE, HOLD, WAIT, TRIM, SELL
  "confidence": 0.85,                   // CIO 决策置信度 (0.0 ~ 1.0)
  "dominant_view": "bull",              // 辩论主导倾向: bull / bear / tie
  "suggested_alloc_cny": 5000.0,        // 建议调仓金额 (正数为买入，负数为卖出)
  "cio_memo": "...",                    // CIO 总结备忘录文本
  "formatted_recommendation": "...",    // 格式化后的排版建议文本 (Android 弹窗渲染的主要内容)
  "macro_view": "...",                  // 宏观分析师观点原件
  "quant_view": "...",                  // 量化分析师观点原件
  "risk_view": "...",                   // 风控合规官观点原件
  "quant_adjusted": "...",              // 经过修正后的量化模型表述
  "risk_adjusted": "...",               // 经过修正后的风控合规表述
  "market_data": "...",                 // 拼接后的底座行情与宏观输入快照
  "regime": "Uptrend",                  // 当前识别出的趋势状态
  "fundamental_model": "industrial_lead",// 基本面评估模型 key
  "fundamental_score": 75.0,            // 基本面评分 (0 ~ 100)
  "fundamental_coverage": 1.0,          // 基本面数据覆盖度
  "fundamental_anchor_multiplier": 1.0, // 基本面对仓位上限的锚定系数
  "entry_exit_points": {                // 入场出场点计算结果
    "entry_low": 20.50,
    "entry_high": 21.20,
    "exit_target": 25.00,
    "exit_stop": 19.10
  },
  "position_exit_policy": {},           // 兼容旧字段；生产链路已停用持仓纪律影响
  "right_side_trend_gate": {            // 右侧趋势确认状态
    "gate_passed": true,
    "rationale": "..."
  },
  "optimizer_review": "...",            // LLM 审核文本；algorithm_only 模式为空
  "decision_mode": "algorithm_only",     // 实际决策引擎
  "elapsed_sec": 12.4                   // 本次决策耗时(秒)
}
```

兼容说明：
- `verdict` 枚举保持不变，只能是 `BUY, ACCUMULATE, HOLD, WAIT, TRIM, SELL`。
- `suggested_alloc_cny` 语义保持不变：正数表示买入/加仓，负数表示卖出/减仓。
- Python 侧可能额外返回 `trim_reason`、`reentry_price`、`reentry_condition`、`expected_path` 等解释字段；客户端应按可选字段处理。风控型减仓（如 `stop_loss/bearish/risk/drawdown/exit_policy`）不要求买回点，战术型减仓才要求低于现价的 `reentry_price`。
- Python 侧和监控快照可能额外返回 `technical.ma20`、`technical.ma120`、`technical.atr_pct`，或在 `entry_exit_points` 中返回 `atr_pct`。这些字段用于 App/桌面卡片展示均线、波动率和风险线，不改变 `verdict`、`suggested_alloc_cny`、`entry_exit_points` 的既有语义；旧版客户端可以忽略。
- App 本地 wrapper 可使用 `resolved_snapshot` 作为展示兜底：当最新委员会结果缺少技术指标时，优先复用监控快照或最近缓存中的 `technical/entry_exit_points`。该兜底只补展示字段，不代表重新运行委员会，也不应触发行情同步。
- `trading_mode` 是新增尾部可选参数，旧版 App 不传时仍按 `active_profit` 执行。该参数只进入手机本地 Python 的委员会上下文与提醒优化口径，不改变 `verdict`、`suggested_alloc_cny` 等既有字段语义。
- `trading_mode` 当前规范值只有 `active_profit` 和 `cash_recovery`；历史 `risk_off`、`bear`、`defensive` 与“主动避险”输入会兼容归一化为 `cash_recovery`。
- `decision_mode` 为空时读取 `INVEST_COMMITTEE_MODE`，仍为空则默认 `algorithm_only`。`algorithm_only` 跳过 `run_macro_view`、`run_committee` 和 `run_optimizer_review_view`，不会消耗 LLM；`llm` / `llm_committee` 恢复旧多角色辩论；`auto` 表示 A 股算法直出、非 A 股使用 LLM 委员会。
- Android 本地 wrapper 不再强制要求 LLM API Key；在算法模式下，Key 为空也可以完成 A 股分析。
- `behavioral_factor` 是 A 股生产基座新增的可选审计对象。旧 App 不需要新增必填入参，也可以忽略响应中的该对象；`verdict`、`confidence`、`suggested_alloc_cny` 和 `entry_exit_points` 的既有类型与含义不变。`position_exit_policy` 仅保留为空对象兼容旧客户端，不再影响生产决策。`optimizer_weight` 表示标的级因子可信权重，不是建议买入比例；`target_weight_pct` 才是因子目标仓位。
- `behavioral_factor.selection_scope` 和 `behavioral_factor.represents_account_holding` 是尾部可选语义字段。当前前四目标使用 `selection_scope=factor_model_target_portfolio`、`represents_account_holding=false`；客户端不得把 `selected=true` 当作真实持仓。旧响应缺少这两个字段时仍按可选字段兼容，不改变其他字段解析。
- A 股行为因子缺失或低置信度时，服务端会先使用监控配置中的持仓/关注标的补建横截面；仍失败时 `verdict=WAIT`、`suggested_alloc_cny=0`，监控快照写 `state=factor_unavailable`。客户端应显示“无法判断”并禁用交易，不得解释成 `HOLD`。
- 旧 `/api/config/exit_params` 与 `/api/config/env_policies` 仅保留兼容响应：POST 忽略内容，GET 返回空对象/空策略，不再写报告或 `.env`。
- 手机本地 `run_committee_local` 返回单标的根 JSON，部分远程任务返回 `result.by_asset.<symbol>`。Android 缓存解析器必须同时支持两种响应形状，并恢复 `entry_exit_points`、`behavioral_factor`、基本面和操作字段，不能假设本地缓存一定有 `by_asset` 包装层。
- 委员会详情弹窗的价格线优先读取本轮 `symbolSummary.entry_exit_points`，只有本轮字段缺失时才回退 `HoldingRow.buy_criteria/exit_points`，避免 Python 已完成计算但 UI 仍显示分析前旧行数据。
- Android 行为因子区展示 `score/selected/target_weight_pct/trailing_3m_factor_return_pct/trailing_3m_hit_rate/trailing_3m_sample_size/optimizer_weight`。字段均为可选项，缺失时应局部降级，不得隐藏已有入场/出场点或改变 verdict。
- Android 主界面可从当前 `HoldingRow` 集合筛选 `behavioral_factor.selected=true`，按 `target_weight_pct` 降序显示最多四只。点击目标只打开对应委员会分析，不写持仓；没有有效目标时隐藏总览，不影响原监控列表。
- 新生产快照不再生成 `alert_source="position_exit_discipline_review"` 或有效 `discipline_review`。旧缓存里如果还存在这些字段，客户端应忽略；服务端快照层也会把它们降级为空对象。
- 监控快照的候选/抑制原因可能出现 `low_sell_score`、`recent_opposite_real_trade_without_new_entry_exit_trigger` 或 `recent_opposite_real_trade_without_price_progress`。前者表示卖出评分未达到当前模式阈值；后两者仅用于卖出后的重新买回，表示尚未出现新的回调、再入场或突破触发。持仓卖出不再要求旧纪律价格线。
- 远端配置上传 `POST /api/config/monitor_config` 会先落盘配置并立即返回；账本重建和旧快照清理在服务端后台执行，避免大配置上传时客户端等待超时。Android 网络层应保留较长读超时用于 SSE，同时配置独立写超时用于上传配置 payload。

#### 2.1.3 失败返回 JSON 结构 (`success: false`)
```json
{
  "success": false,
  "symbol": "601138",
  "name": "工业富联",
  "market": "A股",
  "error": "ExceptionName: 错误堆栈信息描述",
  "elapsed_sec": 1.2
}
```

---

### 2.2 接口 2: 盘中告警与指标更新 (`evaluate_alerts_local`)

Android 客户端在后台服务轮询时调用，用于判定持仓和自选列表中是否有人触发了止损、止盈或技术买卖点。

#### 2.2.1 函数签名
```python
def evaluate_alerts_local(
    results_json: str,                  # 所有被监控资产最近一次委员会结论(run_committee_local)结果的字典 JSON
    prices_json: str,                   # 最新市场价格映射字典 JSON (格式: {"601138": 22.40, ...})
    holding_symbols_json: str,          # 包含所有持仓股票代码的列表 JSON (格式: ["601138", "300750"])
    stocks_json: str                    # 被监控资产的基础属性字典列表 JSON
) -> str:                               # 返回判定结果 JSON 字符串
```

#### 2.2.2 成功返回 JSON 结构 (`success: true`)
```json
{
  "success": true,
  "confirmed_alerts": [                 // 触发了买卖技术线的真实告警列表
    {
      "symbol": "601138",
      "type": "entry_trigger",          // 告警类型: entry_trigger (技术入场), exit_trigger (技术出场)
      "price": 19.05,                   // 触发告警时的市场价格
      "trigger_line": 19.10,            // 设定的触发线
      "message": "工业富联(601138) 触及技术入场/出场参考线。"
    }
  ],
  "watch_rows": [                       // 更新了最新现价和距离百分比后的监控详情行
    {
      "symbol": "601138",
      "current_price": 22.40,
      "stop_loss_line": 19.10,
      "distance_to_stop": -14.73,        // 距离止损线百分比
      "take_profit_line": 25.00,
      "distance_to_profit": 11.61,       // 距离止盈目标百分比
      "regime_brief": "上升趋势",
      "verdict": "BUY"
    }
  ],
  "elapsed_sec": 0.05
}
```

---

## 3. 对后继调整的指导原则

1. **接口参数的向后兼容**：
   - 严禁删除 `run_committee_local` 与 `evaluate_alerts_local` 的现有参数。
   - 如需引入新的策略参数，必须将其作为**可选参数 (Keyword Argument)**，并提供合理的默认值，确保 Android 侧旧版本调用不报错。
2. **返回 JSON 结构的健壮性**：
   - App 侧在解析返回结果时，应使用安全的 `get` 访问字段（并配备合理的默认回退值），以防 Python 基座返回的字典中缺失某些字段导致崩溃。
   - 保持 "success" 键为布尔值，用作是否成功调用的首要判断依据。
3. **真机数据保护**：保存真实 API Key、远程地址和本地快照的手机只允许通过 `assembleDebug` + `adb install -r` 覆盖安装。`connectedDebugAndroidTest` 可能由 Gradle/UTP 卸载目标包，只能在模拟器或专用测试机运行。
