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

Android 客户端通过调用该函数，在手机本地执行多 Agent 闭门辩论并得出投资结论。

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
    change_pct: float = 0.0             # 今日涨跌幅百分比 (如 5.3 代表 +5.3%)
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
  "position_exit_policy": {             // 持仓减仓与止损线计算
    "stop_loss_price": 19.10,
    "take_profit_price": 25.00,
    "trailing_stop_active": false
  },
  "right_side_trend_gate": {            // 右侧趋势确认状态
    "gate_passed": true,
    "rationale": "..."
  },
  "optimizer_review": "...",            // CIO 决策审查优化记录
  "elapsed_sec": 12.4                   // 本次决策耗时(秒)
}
```

兼容说明：
- `verdict` 枚举保持不变，只能是 `BUY, ACCUMULATE, HOLD, WAIT, TRIM, SELL`。
- `suggested_alloc_cny` 语义保持不变：正数表示买入/加仓，负数表示卖出/减仓。
- Python 侧可能额外返回 `trim_reason`、`reentry_price`、`reentry_condition`、`expected_path` 等解释字段；客户端应按可选字段处理。风控型减仓（如 `stop_loss/bearish/risk/drawdown/exit_policy`）不要求买回点，战术型减仓才要求低于现价的 `reentry_price`。

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
  "confirmed_alerts": [                 // 触发了买卖或止损纪律的真实告警列表
    {
      "symbol": "601138",
      "type": "stop_loss",              // 告警类型: stop_loss (止损), take_profit (止盈), entry_trigger (技术入场), exit_trigger (技术出场)
      "price": 19.05,                   // 触发告警时的市场价格
      "trigger_line": 19.10,            // 设定的触发线
      "message": "工业富联(601138) 已跌破锁定止损线 19.10，触发纪律卖出告警。"
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
