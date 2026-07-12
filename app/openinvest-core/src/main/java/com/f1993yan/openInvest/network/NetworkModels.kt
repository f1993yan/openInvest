package com.f1993yan.openInvest.network

data class SnapshotResponse(
    val version: Int,
    val generated_at: String?,
    val round_time: String?,
    val cash_cny: Double,
    val total_assets_cny: Double,
    val t2_pending_cash_cny: Double = 0.0,
    val total_cash_cny: Double = 0.0,
    val counts: Map<String, Int>?,
    val rows: List<HoldingRow>?
)

data class HoldingRow(
    val symbol: String,
    val name: String,
    val market: String,
    val sector: String? = null,
    val industry: String? = null,
    val min_lot_size: Int = 100,
    val units: Double = 0.0,
    val is_holding: Boolean = false,
    val position_pct: Double = 0.0,
    val target_position_pct: Double? = null,
    val cost: Double = 0.0,
    val price: HoldingRowPrice,
    val state: String,
    val buy_criteria: BuyCriteria? = null,
    val exit_points: ExitPoints? = null,
    val fundamental: Fundamental? = null,
    val technical: Technical? = null,
    val operation: Operation? = null,
    val llm_review: LlmReview? = null,
    val behavioral_factor: BehavioralFactor? = null,
    val error: String? = null,
    val success: Boolean = true,
    var _is_resolving: Boolean = false // Client side flag
)

data class HoldingRowPrice(
    val current: Double,
    val prev_close: Double,
    val change_pct: Double,
    val name: String? = null
)

data class BuyCriteria(
    val pullback_price: Double? = null,
    val breakout_price: Double? = null,
    val reentry_price: Double? = null,
    val reward_risk_ratio: Double? = null,
    val reason: String? = null
)

data class ExitPoints(
    val stop_loss_price: Double? = null,
    val take_profit_price: Double? = null,
    val trim_price: Double? = null
)

data class Fundamental(
    val model: String? = null,
    val score: Double? = null,
    val coverage: Double? = null,
    val anchor_multiplier: Double? = null
)

data class Technical(
    val regime: String? = null,
    val quant_view: String? = null,
    val market_data_excerpt: String? = null,
    val entry_exit_model: String? = null,
    val low_confidence: Boolean? = null,
    val atr_pct: Double? = null,
    val expected_return_pct: Double? = null,
    val ma20: Double? = null,
    val ma120: Double? = null
)

data class Operation(
    val status: String? = null,
    val reason: String? = null,
    val verdict: String? = null,
    val confidence: Double? = null,
    val suggested_alloc_cny: Double? = null,
    val optimizer_lots: Int? = null,
    val llm_review_lots: Int? = null,
    val alert_score: Double? = null,
    val confirmed: Boolean? = null,
    val llm_conflict: Boolean? = null,
    val execution_blocked: Boolean? = null,
    val triggers: List<TriggerItem>? = null
)

data class TriggerItem(
    val side: String? = null,
    val kind: String? = null,
    val level: Double? = null,
    val price: Double? = null
)

data class LlmReview(
    val conclusion: String? = null,
    val one_line: String? = null,
    val risk_note: String? = null,
    val execution_plan: String? = null,
    val raw_excerpt: String? = null
)

data class BehavioralFactor(
    val model_key: String? = null,
    val score: Double? = null,
    val expected_return_pct: Double? = null,
    val target_weight_pct: Double? = null,
    val eligible: Boolean? = null,
    val selected: Boolean? = null,
    val low_confidence: Boolean? = null,
    val optimizer_weight: Double? = null,
    val trailing_3m_factor_return_pct: Double? = null,
    val trailing_3m_hit_rate: Double? = null,
    val trailing_3m_sample_size: Int? = null,
)

// Daily Stock Selection Models
data class DailySelectionResponse(
    val stocks: List<SelectionStock>? = null,
    val message: String? = null
)

data class SelectionStock(
    val symbol: String,
    val name: String?,
    val score: Double?,
    val sector: String?,
    val reasons: List<String>?,
    val money_flow_score: Double?,
    val fundamental_score: Double?,
    val tape: Map<String, Any>?,
    val trend: Map<String, Any>?,
    val entry_plan: EntryPlan?,
    val path_distribution: Map<String, Any>?,
    val risk_defense: Map<String, Any>?,
    val calibration: Map<String, Any>?
)

data class EntryPlan(
    val action: String?,
    val trigger_price: Double?,
    val stop_loss_price: Double?,
    val note: String?
)

// Weekend News Models
data class WeekendNewsResponse(
    val cards: List<NewsCard>?,
    val source: String?
)

data class NewsCard(
    val title: String,
    val sector: String,
    val logic: String,
    val heat_score: Double,
    val freshness_score: Double,
    val leaders: List<NewsLeader>?,
    val risk_note: String
)

data class NewsLeader(
    val symbol: String?,
    val name: String?,
    val code: String?,
    val reason: String?
)

// Committee API Models
data class CommitteeRunRequest(
    val symbols: List<String> = emptyList(),
    val max_debate_rounds: Int = 4,
    val note: String = "android_client_trigger"
)

data class CommitteeRunResponse(
    val task_id: String,
    val status: String,
    val started_at: String,
    val poll_url: String
)

data class CommitteeStatusResponse(
    val task_id: String,
    val status: String,
    val phase: String,
    val started_at: String,
    val ended_at: String?,
    val error: String?,
    val progress: Double?,
    val output: String?, // HTML or plain text log of debate
    val verdict: String?,
    val symbols: List<String>?,
    val debate_summary: String?,
    val final_verdicts: Map<String, Any>?, // Symbol to verdict details
    val events: List<Map<String, Any>>?,
    val result: Map<String, Any>?,
    var rawJsonResult: String? = null
)

data class ConfigResponse(
    val ok: Boolean,
    val message: String
)

data class CrawlerSettings(
    val frequency_minutes: Int,
    val target_refresh_enabled: Boolean,
    val news_refresh_enabled: Boolean
)
