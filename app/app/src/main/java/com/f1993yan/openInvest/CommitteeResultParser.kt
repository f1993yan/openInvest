package com.f1993yan.openInvest

import com.f1993yan.openInvest.network.*
import com.google.gson.JsonObject
import com.google.gson.JsonParser

data class ParsedCommitteeFields(
    val exitPoints: ExitPoints?,
    val buyCriteria: BuyCriteria?,
    val operation: Operation?,
    val fundamental: Fundamental?,
    val llmReview: LlmReview?,
    val behavioralFactor: BehavioralFactor?
)

private fun JsonObject.number(key: String): Double? =
    get(key)?.takeIf { it.isJsonPrimitive && it.asJsonPrimitive.isNumber }?.asDouble

private fun JsonObject.positive(key: String): Double? = number(key)?.takeIf { it > 0.0 }

private fun JsonObject.text(key: String): String? =
    get(key)?.takeIf { it.isJsonPrimitive && it.asJsonPrimitive.isString }?.asString?.takeIf { it.isNotBlank() }

fun parseCachedResult(symbol: String, rawJson: String): ParsedCommitteeFields? {
    return try {
    val root = JsonParser.parseString(rawJson).asJsonObject
    val cleanSymbol = symbol.uppercase()
    val symbolObj = if (root.has("entry_exit_points") || root.has("verdict")) {
        root
    } else {
        val result = root.getAsJsonObject("result")
        val byAsset = result?.getAsJsonObject("by_asset") ?: root.getAsJsonObject("by_asset") ?: return null
        byAsset.getAsJsonObject(cleanSymbol)
            ?: byAsset.getAsJsonObject(cleanSymbol.substringBefore('.'))
            ?: return null
    }
    val levels = symbolObj.getAsJsonObject("entry_exit_points") ?: JsonObject()
    val exitPoints = ExitPoints(levels.positive("stop_loss_price"), levels.positive("take_profit_price"), levels.positive("trim_price"))
    val buyCriteria = BuyCriteria(
        levels.positive("buy_pullback_price"), levels.positive("buy_breakout_price"),
        levels.positive("reentry_price"), levels.positive("reward_risk_ratio"), levels.text("reason")
    )
    val triggers = symbolObj.getAsJsonArray("triggers")?.mapNotNull { item ->
        item.takeIf { it.isJsonObject }?.asJsonObject?.let { value ->
            TriggerItem(value.text("side"), value.text("kind"), value.number("level"), value.number("price"))
        }
    } ?: emptyList()
    val operation = Operation(
        verdict = symbolObj.text("verdict") ?: "HOLD",
        suggested_alloc_cny = symbolObj.number("suggested_alloc_cny") ?: 0.0,
        status = "monitoring",
        triggers = triggers
    )
    val fundamental = Fundamental(
        symbolObj.text("fundamental_model"), symbolObj.number("fundamental_score") ?: 50.0,
        symbolObj.number("fundamental_coverage") ?: 0.0, symbolObj.number("fundamental_anchor_multiplier") ?: 1.0
    )
    val review = symbolObj.getAsJsonObject("llm_review")?.let {
        LlmReview(it.text("conclusion"), it.text("one_line"), it.text("risk_note"), it.text("execution_plan"), it.text("raw_excerpt"))
    }
    val behavioral = symbolObj.getAsJsonObject("behavioral_factor")?.let {
        BehavioralFactor(
            model_key = it.text("model_key"),
            score = it.number("score"),
            expected_return_pct = it.number("expected_return_pct"),
            target_weight_pct = it.number("target_weight_pct"),
            eligible = it.get("eligible")?.asBoolean,
            selected = it.get("selected")?.asBoolean,
            low_confidence = it.get("low_confidence")?.asBoolean,
            optimizer_weight = it.number("optimizer_weight"),
            trailing_3m_factor_return_pct = it.number("trailing_3m_factor_return_pct"),
            trailing_3m_hit_rate = it.number("trailing_3m_hit_rate"),
            trailing_3m_sample_size = it.number("trailing_3m_sample_size")?.toInt(),
            selection_scope = it.text("selection_scope"),
            represents_account_holding = it.get("represents_account_holding")?.asBoolean,
        )
    }
        ParsedCommitteeFields(exitPoints, buyCriteria, operation, fundamental, review, behavioral)
    } catch (_: Exception) {
        null
    }
}
