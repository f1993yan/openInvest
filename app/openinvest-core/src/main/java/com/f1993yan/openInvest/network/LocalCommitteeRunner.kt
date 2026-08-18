package com.f1993yan.openInvest.network

import android.content.Context
import android.util.Log
import com.chaquo.python.Python
import com.google.gson.Gson
import java.io.IOException

object LocalCommitteeRunner {
    private const val TAG = "LocalCommitteeRunner"
    private val gson = Gson()

    private fun normalizeTradingMode(value: String?): String {
        val text = value?.trim()?.lowercase().orEmpty()
        return when (text) {
            "cash_recovery", "cash", "现金回收", "主动避险", "risk_off", "bear", "defensive" -> "cash_recovery"
            else -> "active_profit"
        }
    }

    /**
     * Executes the investment committee debate locally on the phone's Chaquopy runtime.
     * It replicates the server's API but runs it in-process.
     */
    fun runCommitteeLocally(
        context: Context,
        symbol: String,
        name: String,
        market: String,
        sector: String,
        industry: String,
        positionPct: Double,
        targetPositionPct: Double?,
        cost: Double,
        currentPrice: Double?,
        totalAssets: Double,
        cash: Double,
        holdingsJson: String,
        newsBrief: String,
        minLotSize: Int,
        tPlus1: Boolean,
        availableCash: Double,
        t2PendingCash: Double,
        optimizerReviewEnabled: Boolean,
        maxDebateRounds: Int,
        changePct: Double,
        ma20: Double? = null,
        ma120: Double? = null,
        atrPct: Double? = null,
        onResult: (Result<CommitteeStatusResponse>) -> Unit
    ) {
        val prefs = context.getSharedPreferences("open_invest_prefs", Context.MODE_PRIVATE)
        val apiKey = prefs.getString("llm_api_key", "") ?: ""
        val model = prefs.getString("llm_model", "gemini-2.5-pro") ?: "gemini-2.5-pro"
        val llmBaseUrl = prefs.getString("llm_base_url", "") ?: ""
        val tradingMode = normalizeTradingMode(prefs.getString("trading_mode", "active_profit"))
        val decisionMode = prefs.getString("decision_mode", "algorithm_only") ?: "algorithm_only"

        // Extract server IP and port from backend URL to pass to python for macro/history queries
        val backendUrl = NetworkClient.getBaseUrl()
        val serverIp = extractIpOrHostname(backendUrl)
        val serverPort = extractPort(backendUrl)

        Thread {
            try {
                val py = Python.getInstance()
                val pyModule = py.getModule("utils.phone_committee")

                Log.d(TAG, "Calling run_committee_local for $symbol ($name) with mode: $decisionMode, trading_mode: $tradingMode")

                val pyResult = pyModule.callAttr(
                    "run_committee_local",
                    symbol,
                    name,
                    market,
                    sector,
                    industry,
                    positionPct,
                    targetPositionPct,
                    cost,
                    currentPrice,
                    totalAssets,
                    cash,
                    apiKey,
                    model,
                    serverIp,
                    holdingsJson,
                    llmBaseUrl,
                    newsBrief,
                    minLotSize,
                    tPlus1,
                    availableCash,
                    t2PendingCash,
                    optimizerReviewEnabled,
                    maxDebateRounds,
                    serverPort,
                    changePct,
                    tradingMode,
                    ma20,
                    ma120,
                    atrPct,
                    decisionMode
                )

                val jsonResult = pyResult.toString()
                Log.d(TAG, "Local committee run raw result length: ${jsonResult.length}")

                val gson = Gson()
                val map = gson.fromJson<Map<String, Any>>(jsonResult, object : com.google.gson.reflect.TypeToken<Map<String, Any>>() {}.type)

                val success = map["success"] as? Boolean ?: false
                if (!success) {
                    val errorMsg = map["error"] as? String ?: "Unknown python error"
                    onResult(Result.failure(Exception(errorMsg)))
                    return@Thread
                }

                // Map python response to CommitteeStatusResponse format so that the UI dialog can display it
                // We create a finished "done" response
                val symbolSummary = mapOf<String, Any?>(
                    "verdict" to map["verdict"],
                    "suggested_alloc_cny" to (map["suggested_alloc_cny"] ?: 0.0),
                    "cio_memo" to map["cio_memo"],
                    "macro_view" to map["macro_view"],
                    "quant_view" to map["quant_view"],
                    "risk_view" to map["risk_view"],
                    "market_data" to map["market_data"],
                    "regime" to map["regime"],
                    "fundamental_model" to map["fundamental_model"],
                    "fundamental_score" to (map["fundamental_score"] ?: 50.0),
                    "fundamental_coverage" to (map["fundamental_coverage"] ?: 0.0),
                    "fundamental_anchor_multiplier" to (map["fundamental_anchor_multiplier"] ?: 1.0),
                    "entry_exit_points" to map["entry_exit_points"],
                    "position_exit_policy" to map["position_exit_policy"],
                    "right_side_trend_gate" to map["right_side_trend_gate"],
                    "optimizer_review" to map["optimizer_review"],
                    "decision_mode" to map["decision_mode"],
                    "decision_synthesis" to map["decision_synthesis"],
                    "behavioral_factor" to map["behavioral_factor"],
                    "hk_spatio_factor" to map["hk_spatio_factor"]
                )

                val finishedResponse = CommitteeStatusResponse(
                    task_id = "local_task_${System.currentTimeMillis()}",
                    status = "done",
                    phase = "done",
                    started_at = "",
                    ended_at = null,
                    error = null,
                    progress = 1.0,
                    output = (map["formatted_recommendation"] as? String) ?: (map["cio_memo"] as? String),
                    verdict = map["verdict"] as? String,
                    symbols = listOf(symbol),
                    debate_summary = map["debate_summary"] as? String,
                    final_verdicts = map["verdict"]?.let { mapOf(symbol to mapOf("verdict" to it, "suggested_alloc_cny" to (map["suggested_alloc_cny"] ?: 0.0))) },
                    events = listOf(mapOf("message" to "本地委员会计算完成")),
                    result = mapOf("by_asset" to mapOf(symbol to symbolSummary)),
                    rawJsonResult = jsonResult
                )

                onResult(Result.success(finishedResponse))
            } catch (e: Exception) {
                Log.e(TAG, "Error executing local committee", e)
                onResult(Result.failure(e))
            }
        }.start()
    }

    fun evaluateAlertsLocally(
        context: Context,
        resultsJson: String,
        pricesJson: String,
        holdingSymbolsJson: String,
        stocksJson: String,
        onResult: (Result<String>) -> Unit
    ) {
        Thread {
            try {
                val py = Python.getInstance()
                val pyModule = py.getModule("utils.phone_committee")

                val pyResult = pyModule.callAttr(
                    "evaluate_alerts_local",
                    resultsJson,
                    pricesJson,
                    holdingSymbolsJson,
                    stocksJson
                )
                onResult(Result.success(pyResult.toString()))
            } catch (e: Exception) {
                Log.e(TAG, "Error in evaluateAlertsLocally", e)
                onResult(Result.failure(e))
            }
        }.start()
    }

    private fun extractIpOrHostname(url: String): String {
        var clean = url.replace("http://", "").replace("https://", "")
        val colonIdx = clean.indexOf(":")
        if (colonIdx != -1) {
            clean = clean.substring(0, colonIdx)
        }
        val slashIdx = clean.indexOf("/")
        if (slashIdx != -1) {
            clean = clean.substring(0, slashIdx)
        }
        return clean.trim()
    }

    private fun extractPort(url: String): String {
        var clean = url.replace("http://", "").replace("https://", "")
        val slashIdx = clean.indexOf("/")
        if (slashIdx != -1) {
            clean = clean.substring(0, slashIdx)
        }
        val colonIdx = clean.indexOf(":")
        if (colonIdx != -1) {
            return clean.substring(colonIdx + 1).trim()
        }
        return "8765" // default port
    }
}
