package com.f1993yan.openInvest

import android.content.Context
import android.os.Bundle
import android.net.Uri
import android.util.Log
import android.widget.Toast
import android.content.Intent
import android.app.NotificationChannel
import android.app.NotificationManager
import androidx.core.app.NotificationCompat
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.content.ContextCompat
import android.content.pm.PackageManager
import android.Manifest
import android.os.Build
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.core.*
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.material3.pulltorefresh.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.shadow
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalSoftwareKeyboardController
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.window.Dialog
import androidx.compose.ui.window.DialogProperties
import com.f1993yan.openInvest.network.*
import com.f1993yan.openInvest.ui.theme.*
import okhttp3.*
import java.io.IOException
import com.google.gson.Gson
import com.google.gson.reflect.TypeToken
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.coroutines.joinAll
import kotlinx.coroutines.sync.Semaphore
import kotlinx.coroutines.sync.withPermit
import kotlinx.coroutines.delay
import org.json.JSONObject


class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        // Initialize Python
        if (!com.chaquo.python.Python.isStarted()) {
            com.chaquo.python.Python.start(com.chaquo.python.android.AndroidPlatform(this))
        }

        // Load saved server URL
        val prefs = getSharedPreferences("open_invest_prefs", Context.MODE_PRIVATE)
        val savedUrl = prefs.getString("server_url", "http://10.0.2.2:8765") ?: "http://10.0.2.2:8765"
        NetworkClient.setBaseUrl(savedUrl)

        enableEdgeToEdge()
        setContent {
            OpenInvestTheme {
                Scaffold(modifier = Modifier.fillMaxSize()) { innerPadding ->
                    MainScreen(
                        modifier = Modifier.padding(innerPadding),
                        onSaveUrl = { newUrl ->
                            prefs.edit().putString("server_url", newUrl).apply()
                            NetworkClient.setBaseUrl(newUrl)
                        }
                    )
                }
            }
        }
    }
}

// Custom shake animation modifier for action required state
@Composable
fun Modifier.shake(enabled: Boolean): Modifier = if (enabled) {
    val infiniteTransition = rememberInfiniteTransition(label = "shake")
    val translationX by infiniteTransition.animateFloat(
        initialValue = -4f,
        targetValue = 4f,
        animationSpec = infiniteRepeatable(
            animation = tween(durationMillis = 60, easing = LinearEasing),
            repeatMode = RepeatMode.Reverse
        ),
        label = "shake_translation"
    )
    this.graphicsLayer { this.translationX = translationX }
} else {
    this
}

private fun getDisplayUpdateTime(roundTime: String?, generatedAt: String?): String {
    if (roundTime != null && roundTime != "config") {
        return roundTime
    }
    if (!generatedAt.isNullOrEmpty()) {
        try {
            // e.g. "2026-06-20T22:40:14"
            val tIndex = generatedAt.indexOf('T')
            if (tIndex != -1 && generatedAt.length >= tIndex + 6) {
                val datePart = generatedAt.substring(maxOf(0, tIndex - 5), tIndex) // e.g. "06-20"
                val timePart = generatedAt.substring(tIndex + 1, tIndex + 6) // e.g. "22:40"
                return "$datePart $timePart"
            }
        } catch (e: Exception) {
            // fallback
        }
        return generatedAt
    }
    return "-"
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun MainScreen(modifier: Modifier = Modifier, onSaveUrl: (String) -> Unit) {
    val context = LocalContext.current
    val gson = remember { Gson() }
    var serverUrl by remember { mutableStateOf(NetworkClient.getBaseUrl()) }
    var snapshot by remember {
        mutableStateOf<SnapshotResponse?>(
            readLocalFile(context, "resolved_snapshot.json")?.let { jsonStr ->
                try {
                    gson.fromJson(jsonStr, SnapshotResponse::class.java)
                } catch (e: Exception) {
                    Log.e("MainActivity", "Failed to parse cached resolved snapshot", e)
                    null
                }
            }
        )
    }
    var showReleaseT2Confirm by remember { mutableStateOf(false) }
    var displayCashTarget by remember(snapshot?.cash_cny) { mutableStateOf(snapshot?.cash_cny ?: 0.0) }
    var displayT2Target by remember(snapshot?.t2_pending_cash_cny) { mutableStateOf(snapshot?.t2_pending_cash_cny ?: 0.0) }

    val animatedCash by animateFloatAsState(
        targetValue = displayCashTarget.toFloat(),
        animationSpec = tween(durationMillis = 1000)
    )
    val animatedT2 by animateFloatAsState(
        targetValue = displayT2Target.toFloat(),
        animationSpec = tween(durationMillis = 1000)
    )
    var selection by remember {
        mutableStateOf<DailySelectionResponse?>(
            readLocalFile(context, "selection_cache.json")?.let { jsonStr ->
                try {
                    gson.fromJson(jsonStr, DailySelectionResponse::class.java)
                } catch (e: Exception) {
                    Log.e("MainActivity", "Failed to parse cached selection", e)
                    null
                }
            }
        )
    }
    var news by remember { mutableStateOf<WeekendNewsResponse?>(null) }
    var isConnected by remember { mutableStateOf<Boolean?>(null) }
    var isLoading by remember { mutableStateOf(false) }

    // Trade execution states from Swipe Actions
    var activeTradeSymbol by remember { mutableStateOf<String?>(null) }
    var activeTradeRow by remember { mutableStateOf<HoldingRow?>(null) }
    var activeTradeVerdict by remember { mutableStateOf<String?>(null) }
    var activeTradeSuggestedAlloc by remember { mutableStateOf<Double>(0.0) }

    var searchQuery by remember { mutableStateOf("") }
    var showSettings by remember { mutableStateOf(false) }
    var showNews by remember { mutableStateOf(false) }
    var selectedSelectionStock by remember { mutableStateOf<SelectionStock?>(null) }
    var activeCommitteeSymbol by remember { mutableStateOf<String?>(null) }
    var activeCommitteeRow by remember { mutableStateOf<HoldingRow?>(null) }

    var deleteConfirmSymbol by remember { mutableStateOf<String?>(null) }
    var deleteConfirmName by remember { mutableStateOf<String?>(null) }

    val coroutineScope = rememberCoroutineScope()
    val localCommitteeResults = remember {
        val initialMap = mutableStateMapOf<String, String>()
        readLocalFile(context, "committee_results_cache.json")?.let { jsonStr ->
            try {
                val mapType = object : TypeToken<Map<String, String>>() {}.type
                val map = gson.fromJson<Map<String, String>>(jsonStr, mapType)
                initialMap.putAll(map)
            } catch (e: Exception) {
                Log.e("MainActivity", "Failed to parse cached committee results", e)
            }
        }
        initialMap
    }
    var isBackgroundRunning by remember { mutableStateOf(false) }

    fun updateSnapshotRow(symbol: String, updateBlock: (HoldingRow) -> HoldingRow) {
        snapshot = snapshot?.let { snap ->
            val updatedRows = snap.rows?.map { row ->
                if (row.symbol.equals(symbol, ignoreCase = true)) {
                    updateBlock(row)
                } else {
                    row
                }
            }
            snap.copy(rows = updatedRows)
        }
    }

    fun runLocalCommitteeForSnapshot(snap: SnapshotResponse) {
        val prefs = context.getSharedPreferences("open_invest_prefs", Context.MODE_PRIVATE)
        val apiKey = prefs.getString("llm_api_key", "") ?: ""
        if (apiKey.isEmpty()) {
            Log.w("MainActivity", "Skipping background committee run: API key is empty.")
            return
        }
        val isCommitteeEnabled = prefs.getBoolean("committee_analysis_enabled", true)
        if (!isCommitteeEnabled) {
            Log.d("MainActivity", "Background committee run disabled in settings.")
            return
        }
        if (isBackgroundRunning) {
            Log.d("MainActivity", "Background analysis is already running. Skipping duplicate run.")
            return
        }

        val rows = snap.rows ?: return
        if (rows.isEmpty()) return

        isBackgroundRunning = true

        coroutineScope.launch(Dispatchers.Default) {
            val cachedSnap = try {
                readLocalFile(context, "resolved_snapshot.json")?.let { jsonStr ->
                    gson.fromJson(jsonStr, SnapshotResponse::class.java)
                }
            } catch (e: Exception) {
                null
            }

            val rowsToAnalyze = mutableListOf<HoldingRow>()

            for (row in rows) {
                val cachedRow = cachedSnap?.rows?.find { it.symbol.equals(row.symbol, ignoreCase = true) }
                val holdingsJson = try {
                    val list = snap.rows.filter { it.is_holding && it.symbol != row.symbol }.map { h ->
                        mapOf(
                            "symbol" to h.symbol,
                            "name" to h.name,
                            "weight_pct" to h.position_pct,
                            "cost" to h.cost,
                            "current_price" to h.price.current
                        )
                    }
                    gson.toJson(list)
                } catch (e: Exception) {
                    "[]"
                }

                val cachedHoldingsJson = try {
                    val list = cachedSnap?.rows?.filter { it.is_holding && it.symbol != row.symbol }?.map { h ->
                        mapOf(
                            "symbol" to h.symbol,
                            "name" to h.name,
                            "weight_pct" to h.position_pct,
                            "cost" to h.cost,
                            "current_price" to h.price.current
                        )
                    }
                    gson.toJson(list)
                } catch (e: Exception) {
                    "[]"
                }

                val isCached = cachedRow != null &&
                               cachedRow.price.current == row.price.current &&
                               cachedRow.price.change_pct == row.price.change_pct &&
                               cachedRow.units == row.units &&
                               cachedRow.cost == row.cost &&
                               cachedRow.position_pct == row.position_pct &&
                               cachedRow.target_position_pct == row.target_position_pct &&
                               snap.generated_at == cachedSnap.generated_at &&
                               snap.cash_cny == cachedSnap.cash_cny &&
                               snap.total_assets_cny == cachedSnap.total_assets_cny &&
                               holdingsJson == cachedHoldingsJson &&
                               cachedRow.buy_criteria != null &&
                               cachedRow.exit_points != null &&
                               cachedRow.fundamental != null &&
                               cachedRow.operation != null

                if (isCached && cachedRow != null) {
                    withContext(Dispatchers.Main) {
                        updateSnapshotRow(row.symbol) { r ->
                            r.copy(
                                exit_points = cachedRow.exit_points,
                                buy_criteria = cachedRow.buy_criteria,
                                operation = cachedRow.operation,
                                fundamental = cachedRow.fundamental,
                                error = cachedRow.error,
                                success = cachedRow.success
                            )
                        }
                    }
                } else {
                    rowsToAnalyze.add(row)
                }
            }

            if (rowsToAnalyze.isEmpty()) {
                Log.d("MainActivity", "All symbols are cached and up-to-date. Skipping committee run entirely.")
                isBackgroundRunning = false

                val resultsList = localCommitteeResults.values.toList()
                if (resultsList.isNotEmpty()) {
                    val resultsJson = "[" + resultsList.joinToString(",") + "]"
                    val pricesMap = snap.rows.associate { r -> r.symbol.uppercase() to mapOf("price" to r.price.current) }
                    val pricesJson = gson.toJson(pricesMap)
                    val holdingSymbols = snap.rows.filter { it.is_holding }.map { it.symbol.uppercase() }
                    val holdingSymbolsJson = gson.toJson(holdingSymbols)
                    val stocksList = snap.rows.map { r ->
                        mapOf(
                            "symbol" to r.symbol,
                            "name" to r.name,
                            "cost" to r.cost,
                            "position_pct" to r.position_pct,
                            "units" to r.units,
                            "min_lot_size" to r.min_lot_size,
                            "sector" to (r.sector ?: ""),
                            "industry" to (r.industry ?: "")
                        )
                    }
                    val stocksJson = gson.toJson(stocksList)

                    LocalCommitteeRunner.evaluateAlertsLocally(
                        context = context,
                        resultsJson = resultsJson,
                        pricesJson = pricesJson,
                        holdingSymbolsJson = holdingSymbolsJson,
                        stocksJson = stocksJson
                    ) { alertResult ->
                        alertResult.fold(
                            onSuccess = { rawAlertJson ->
                                Log.d("MainActivity", "evaluateAlertsLocally success (from cache): $rawAlertJson")
                                try {
                                    val map = gson.fromJson<Map<String, Any>>(rawAlertJson, object : TypeToken<Map<String, Any>>() {}.type)
                                    val success = map["success"] as? Boolean ?: false
                                    if (success) {
                                        val confirmedAlerts = map["confirmed_alerts"] as? List<Map<String, Any>>
                                        confirmedAlerts?.forEach { alert ->
                                            val name = alert["name"] as? String ?: ""
                                            val symbol = alert["symbol"] as? String ?: ""
                                            val curPrice = (alert["current_price"] as? Number)?.toDouble() ?: 0.0
                                            val triggers = alert["triggers"] as? List<Map<String, Any>>
                                            val matchedSides = alert["matched_sides"] as? List<String>
                                            val sideText = matchedSides?.joinToString("/")?.uppercase() ?: ""

                                            val triggerDetails = triggers?.joinToString(", ") { t ->
                                                val kind = t["kind"] as? String ?: ""
                                                val level = (t["level"] as? Number)?.toDouble() ?: 0.0
                                                val label = when (kind) {
                                                    "cost_stop_loss" -> "成本止损"
                                                    "position_stop" -> "持仓纪律止损"
                                                    "stop_loss" -> "技术止损"
                                                    "take_profit" -> "估算止盈"
                                                    "take_profit_1" -> "第一止盈"
                                                    "take_profit_2" -> "第二止盈"
                                                    "trim" -> "减仓"
                                                    "buy_pullback" -> "回调买入"
                                                    "buy_breakout" -> "突破买入"
                                                    "reentry" -> "重新入场"
                                                    else -> kind
                                                }
                                                "$label@$level"
                                            } ?: ""

                                            showNotification(
                                                context,
                                                "交易信号触发: $name ($symbol)",
                                                "当前价 $curPrice 触发 $sideText: $triggerDetails"
                                            )

                                            coroutineScope.launch(Dispatchers.Main) {
                                                updateSnapshotRow(symbol) { r ->
                                                    r.copy(
                                                        operation = r.operation?.copy(
                                                            status = "action_required",
                                                            reason = triggerDetails
                                                        )
                                                    )
                                                }
                                            }
                                        }
                                    }
                                } catch (e: Exception) {
                                    Log.e("MainActivity", "Error parsing confirmed alerts", e)
                                }
                            },
                            onFailure = { err ->
                                Log.e("MainActivity", "evaluateAlertsLocally failed", err)
                            }
                        )
                    }
                }

                withContext(Dispatchers.Main) {
                    val updatedSnapJson = gson.toJson(snapshot)
                    saveLocalFile(context, "resolved_snapshot.json", updatedSnapJson)
                }
                return@launch
            }

            Log.d("MainActivity", "Starting background local committee analysis for ${rowsToAnalyze.size} symbols...")

            val semaphore = Semaphore(3)
            val jobs = rowsToAnalyze.map { row ->
                launch {
                    semaphore.withPermit {
                        withContext(Dispatchers.Main) {
                            updateSnapshotRow(row.symbol) { it.copy(_is_resolving = true) }
                        }

                        val holdingsJson = try {
                            val list = snap.rows.filter { it.is_holding && it.symbol != row.symbol }.map { h ->
                                mapOf(
                                    "symbol" to h.symbol,
                                    "name" to h.name,
                                    "weight_pct" to h.position_pct,
                                    "cost" to h.cost,
                                    "current_price" to h.price.current
                                )
                            }
                            gson.toJson(list)
                        } catch (e: Exception) {
                            "[]"
                        }

                        val serverUrlStr = NetworkClient.getBaseUrl()
                        var serverIp = serverUrlStr.replace("http://", "").replace("https://", "")
                        val colonIdx = serverIp.indexOf(":")
                        if (colonIdx != -1) serverIp = serverIp.substring(0, colonIdx)
                        val slashIdx = serverIp.indexOf("/")
                        if (slashIdx != -1) serverIp = serverIp.substring(0, slashIdx)
                        serverIp = serverIp.trim()

                        val runChannel = kotlinx.coroutines.channels.Channel<Result<CommitteeStatusResponse>>()
                        LocalCommitteeRunner.runCommitteeLocally(
                            context = context,
                            symbol = row.symbol,
                            name = row.name,
                            market = row.market,
                            sector = row.sector ?: "",
                            industry = row.industry ?: "",
                            positionPct = row.position_pct,
                            targetPositionPct = row.target_position_pct,
                            cost = row.cost,
                            currentPrice = if (row.price.current > 0.0) row.price.current else null,
                            totalAssets = snap.total_assets_cny,
                            cash = snap.cash_cny,
                            holdingsJson = holdingsJson,
                            newsBrief = "",
                            minLotSize = row.min_lot_size,
                            tPlus1 = row.market == "a",
                            availableCash = snap.cash_cny,
                            t2PendingCash = snap.t2_pending_cash_cny,
                            optimizerReviewEnabled = true,
                            maxDebateRounds = 2,
                            changePct = row.price.change_pct
                        ) { result ->
                            runChannel.trySend(result)
                        }

                        val channelResult = runChannel.receive()
                        withContext(Dispatchers.Main) {
                            channelResult.fold(
                                onSuccess = { done ->
                                    val rawJson = done.rawJsonResult ?: ""
                                    if (rawJson.isNotEmpty()) {
                                        localCommitteeResults[row.symbol.uppercase()] = rawJson
                                    }

                                    val symbolSummary = (done.result?.get("by_asset") as? Map<*, *>)?.get(row.symbol) as? Map<*, *>
                                    val eePoints = symbolSummary?.get("entry_exit_points") as? Map<*, *>
                                    val optVal = symbolSummary?.get("verdict") as? String ?: "HOLD"
                                    val suggestedAlloc = (symbolSummary?.get("suggested_alloc_cny") as? Number)?.toDouble() ?: 0.0
                                    val fScore = (symbolSummary?.get("fundamental_score") as? Number)?.toDouble() ?: 50.0
                                    val fModel = symbolSummary?.get("fundamental_model") as? String ?: ""
                                    val fCoverage = (symbolSummary?.get("fundamental_coverage") as? Number)?.toDouble() ?: 0.0
                                    val fAnchor = (symbolSummary?.get("fundamental_anchor_multiplier") as? Number)?.toDouble() ?: 1.0

                                    val exitPoints = ExitPoints(
                                        stop_loss_price = (eePoints?.get("stop_loss_price") as? Number)?.toDouble(),
                                        take_profit_price = (eePoints?.get("take_profit_price") as? Number)?.toDouble(),
                                        trim_price = (eePoints?.get("trim_price") as? Number)?.toDouble()
                                    )
                                    val buyCriteria = BuyCriteria(
                                        pullback_price = (eePoints?.get("buy_pullback_price") as? Number)?.toDouble(),
                                        breakout_price = (eePoints?.get("buy_breakout_price") as? Number)?.toDouble(),
                                        reentry_price = (eePoints?.get("reentry_price") as? Number)?.toDouble(),
                                        reward_risk_ratio = (eePoints?.get("reward_risk_ratio") as? Number)?.toDouble(),
                                        reason = eePoints?.get("reason") as? String
                                    )
                                    val operation = Operation(
                                        verdict = optVal,
                                        suggested_alloc_cny = suggestedAlloc,
                                        status = "monitoring"
                                    )
                                    val fundamental = Fundamental(
                                        model = fModel,
                                        score = fScore,
                                        coverage = fCoverage,
                                        anchor_multiplier = fAnchor
                                    )

                                    updateSnapshotRow(row.symbol) { r ->
                                        r.copy(
                                            _is_resolving = false,
                                            exit_points = exitPoints,
                                            buy_criteria = buyCriteria,
                                            operation = operation,
                                            fundamental = fundamental
                                        )
                                    }
                                },
                                onFailure = { err ->
                                    updateSnapshotRow(row.symbol) { it.copy(_is_resolving = false, error = err.message) }
                                }
                            )
                        }
                    }
                }
            }
            jobs.joinAll()
            isBackgroundRunning = false
            Log.d("MainActivity", "All background local committee runs completed! Evaluating triggers...")

            val resultsList = localCommitteeResults.values.toList()
            if (resultsList.isNotEmpty()) {
                val resultsJson = "[" + resultsList.joinToString(",") + "]"
                val pricesMap = snap.rows.associate { r -> r.symbol.uppercase() to mapOf("price" to r.price.current) }
                val pricesJson = gson.toJson(pricesMap)
                val holdingSymbols = snap.rows.filter { it.is_holding }.map { it.symbol.uppercase() }
                val holdingSymbolsJson = gson.toJson(holdingSymbols)
                val stocksList = snap.rows.map { r ->
                    mapOf(
                        "symbol" to r.symbol,
                        "name" to r.name,
                        "cost" to r.cost,
                        "position_pct" to r.position_pct,
                        "units" to r.units,
                        "min_lot_size" to r.min_lot_size,
                        "sector" to (r.sector ?: ""),
                        "industry" to (r.industry ?: "")
                    )
                }
                val stocksJson = gson.toJson(stocksList)

                LocalCommitteeRunner.evaluateAlertsLocally(
                    context = context,
                    resultsJson = resultsJson,
                    pricesJson = pricesJson,
                    holdingSymbolsJson = holdingSymbolsJson,
                    stocksJson = stocksJson
                ) { alertResult ->
                    alertResult.fold(
                        onSuccess = { rawAlertJson ->
                            Log.d("MainActivity", "evaluateAlertsLocally success: $rawAlertJson")
                            try {
                                val map = gson.fromJson<Map<String, Any>>(rawAlertJson, object : TypeToken<Map<String, Any>>() {}.type)
                                val success = map["success"] as? Boolean ?: false
                                if (success) {
                                    val confirmedAlerts = map["confirmed_alerts"] as? List<Map<String, Any>>
                                    confirmedAlerts?.forEach { alert ->
                                        val name = alert["name"] as? String ?: ""
                                        val symbol = alert["symbol"] as? String ?: ""
                                        val curPrice = (alert["current_price"] as? Number)?.toDouble() ?: 0.0
                                        val triggers = alert["triggers"] as? List<Map<String, Any>>
                                        val matchedSides = alert["matched_sides"] as? List<String>
                                        val sideText = matchedSides?.joinToString("/")?.uppercase() ?: ""

                                        val triggerDetails = triggers?.joinToString(", ") { t ->
                                            val kind = t["kind"] as? String ?: ""
                                            val level = (t["level"] as? Number)?.toDouble() ?: 0.0
                                            val label = when (kind) {
                                                "cost_stop_loss" -> "成本止损"
                                                "position_stop" -> "持仓纪律止损"
                                                "stop_loss" -> "技术止损"
                                                "take_profit" -> "估算止盈"
                                                "take_profit_1" -> "第一止盈"
                                                "take_profit_2" -> "第二止盈"
                                                "trim" -> "减仓"
                                                "buy_pullback" -> "回调买入"
                                                "buy_breakout" -> "突破买入"
                                                "reentry" -> "重新入场"
                                                else -> kind
                                            }
                                            "$label@$level"
                                        } ?: ""

                                        showNotification(
                                            context,
                                            "交易信号触发: $name ($symbol)",
                                            "当前价 $curPrice 触发 $sideText: $triggerDetails"
                                        )

                                        coroutineScope.launch(Dispatchers.Main) {
                                            updateSnapshotRow(symbol) { r ->
                                                r.copy(
                                                    operation = r.operation?.copy(
                                                        status = "action_required",
                                                        reason = triggerDetails
                                                    )
                                                )
                                            }
                                        }
                                    }
                                }
                            } catch (e: Exception) {
                                Log.e("MainActivity", "Error parsing confirmed alerts", e)
                            }
                        },
                        onFailure = { err ->
                            Log.e("MainActivity", "evaluateAlertsLocally failed", err)
                        }
                    )
                }
            }

            withContext(Dispatchers.Main) {
                val finalSnapJson = gson.toJson(snapshot)
                saveLocalFile(context, "resolved_snapshot.json", finalSnapJson)
                val resultsCacheJson = gson.toJson(localCommitteeResults.toMap())
                saveLocalFile(context, "committee_results_cache.json", resultsCacheJson)
            }
        }
    }

    val rebuildSnapshotFromConfig: (String) -> Unit = { configStr: String ->
        try {
            val configObj = org.json.JSONObject(configStr)
            val cash = configObj.optDouble("cash", 0.0)
            val totalAssets = configObj.optDouble("total_assets", 0.0)
            val t2PendingCash = configObj.optDouble("t2_pending_cash", 0.0)
            
            val holdingsArr = configObj.optJSONArray("holdings")
            val watchlistArr = configObj.optJSONArray("watchlist")
            
            val newRows = mutableListOf<HoldingRow>()
            
            fun findExistingRow(sym: String): HoldingRow? {
                return snapshot?.rows?.find { it.symbol.equals(sym, ignoreCase = true) }
            }
            
            if (holdingsArr != null) {
                for (i in 0 until holdingsArr.length()) {
                    val item = holdingsArr.getJSONObject(i)
                    val sym = item.getString("symbol")
                    val name = item.optString("name", sym)
                    val market = item.optString("market", "a")
                    val sector = item.optString("sector", "")
                    val industry = item.optString("industry", "")
                    val minLot = item.optInt("min_lot_size", 100)
                    var units = item.optDouble("units", 0.0)
                    val cost = item.optDouble("cost", 0.0)
                    var posPct = item.optDouble("position_pct", 0.0)
                    
                    if (units <= 0.0 && kotlin.math.abs(posPct) > 0.0 && kotlin.math.abs(cost) > 0.0 && totalAssets > 0.0) {
                        units = (totalAssets * kotlin.math.abs(posPct) / 100.0) / kotlin.math.abs(cost)
                    } else if (kotlin.math.abs(posPct) <= 0.0 && units > 0.0 && kotlin.math.abs(cost) > 0.0 && totalAssets > 0.0) {
                        posPct = (units * kotlin.math.abs(cost)) / totalAssets * 100.0
                    }
                    
                    val existing = findExistingRow(sym)
                    val price = existing?.price ?: HoldingRowPrice(cost, cost, 0.0)
                    
                    newRows.add(
                        HoldingRow(
                            symbol = sym,
                            name = name,
                            market = market,
                            sector = sector,
                            industry = industry,
                            min_lot_size = minLot,
                            units = units,
                            is_holding = true,
                            position_pct = posPct,
                            cost = cost,
                            price = price,
                            state = "monitoring",
                            buy_criteria = existing?.buy_criteria,
                            exit_points = existing?.exit_points,
                            fundamental = existing?.fundamental,
                            technical = existing?.technical,
                            operation = existing?.operation,
                            llm_review = existing?.llm_review
                        )
                    )
                }
            }
            
            if (watchlistArr != null) {
                for (i in 0 until watchlistArr.length()) {
                    val item = watchlistArr.getJSONObject(i)
                    val sym = item.getString("symbol")
                    val name = item.optString("name", sym)
                    val market = item.optString("market", "a")
                    val sector = item.optString("sector", "")
                    val industry = item.optString("industry", "")
                    
                    val existing = findExistingRow(sym)
                    val price = existing?.price ?: HoldingRowPrice(0.0, 0.0, 0.0)
                    
                    newRows.add(
                        HoldingRow(
                            symbol = sym,
                            name = name,
                            market = market,
                            sector = sector,
                            industry = industry,
                            min_lot_size = 100,
                            units = 0.0,
                            is_holding = false,
                            position_pct = 0.0,
                            cost = 0.0,
                            price = price,
                            state = "candidate",
                            buy_criteria = existing?.buy_criteria,
                            exit_points = existing?.exit_points,
                            fundamental = existing?.fundamental,
                            technical = existing?.technical,
                            operation = existing?.operation,
                            llm_review = existing?.llm_review
                        )
                    )
                }
            }
            
            val newSnapshot = SnapshotResponse(
                version = snapshot?.version ?: 1,
                generated_at = java.text.SimpleDateFormat("yyyy-MM-dd HH:mm:ss", java.util.Locale.getDefault()).format(java.util.Date()),
                round_time = "local_sync",
                cash_cny = cash,
                total_assets_cny = totalAssets,
                t2_pending_cash_cny = t2PendingCash,
                total_cash_cny = cash + t2PendingCash,
                counts = mapOf("symbols" to newRows.size),
                rows = newRows
            )
            
            snapshot = newSnapshot
            saveLocalFile(context, "resolved_snapshot.json", gson.toJson(newSnapshot))
            Log.d("MainActivity", "Successfully rebuilt snapshot from config with ${newRows.size} rows")
        } catch (e: Exception) {
            Log.e("MainActivity", "Failed to rebuild snapshot from config", e)
        }
    }

    val refreshData = {
        isLoading = true
        isConnected = null
        NetworkClient.testConnection { connectionResult ->
            isConnected = connectionResult.getOrDefault(false)
            if (connectionResult.getOrDefault(false)) {
                NetworkClient.fetchSnapshot { snapshotResult ->
                    val snap = snapshotResult.getOrNull()
                    snapshot = snap
                    if (snap != null) {
                        runLocalCommitteeForSnapshot(snap)
                    }
                }
                NetworkClient.fetchSelection { selectionResult ->
                    val sel = selectionResult.getOrNull()
                    selection = sel
                    if (sel != null) {
                        saveLocalFile(context, "selection_cache.json", gson.toJson(sel))
                    }
                }
                NetworkClient.fetchNews { newsResult ->
                    news = newsResult.getOrNull()
                    isLoading = false
                }
            } else {
                isLoading = false
                Toast.makeText(context, "无法连接到服务器", Toast.LENGTH_SHORT).show()
            }
        }
    }

    LaunchedEffect(Unit) {
        refreshData()
        triggerRefreshService(context)
    }

    DisposableEffect(Unit) {
        val listener = object : AutoRefreshService.RefreshListener {
            override fun onSnapshotUpdated(snap: SnapshotResponse) {
                android.os.Handler(android.os.Looper.getMainLooper()).post {
                    snapshot = snap
                    runLocalCommitteeForSnapshot(snap)
                }
            }

            override fun onSelectionUpdated(sel: DailySelectionResponse) {
                android.os.Handler(android.os.Looper.getMainLooper()).post {
                    selection = sel
                    saveLocalFile(context, "selection_cache.json", gson.toJson(sel))
                }
            }

            override fun onNewsUpdated(n: WeekendNewsResponse) {
                android.os.Handler(android.os.Looper.getMainLooper()).post {
                    news = n
                }
            }
        }
        AutoRefreshService.registerListener(listener)
        onDispose {
            AutoRefreshService.unregisterListener(listener)
        }
    }

    val keyboardController = LocalSoftwareKeyboardController.current

    val onSearchSubmit = {
        val query = searchQuery.trim()
        if (query.isNotEmpty()) {
            keyboardController?.hide()
            val queryUpper = query.uppercase()
            // Try to match in existing snapshot rows
            val match = snapshot?.rows?.find {
                it.symbol.uppercase() == queryUpper || it.name.trim().lowercase() == query.lowercase()
            }
            if (match != null) {
                activeCommitteeSymbol = match.symbol
                activeCommitteeRow = match
            } else {
                // Not in current list - open in resolving state
                val resolvingRow = HoldingRow(
                    symbol = queryUpper,
                    name = query,
                    market = "a",
                    state = "watch",
                    price = HoldingRowPrice(0.0, 0.0, 0.0, query),
                    _is_resolving = true
                )
                activeCommitteeSymbol = queryUpper
                activeCommitteeRow = resolvingRow
            }
        }
    }

    val infiniteTransition = rememberInfiniteTransition(label = "pulse")
    val dotAlpha by infiniteTransition.animateFloat(
        initialValue = 0.3f,
        targetValue = 1f,
        animationSpec = infiniteRepeatable(
            animation = tween(durationMillis = 1000, easing = FastOutSlowInEasing),
            repeatMode = RepeatMode.Reverse
        ),
        label = "pulse_alpha"
    )

    Column(
        modifier = modifier
            .fillMaxSize()
            .background(SlateBackground)
    ) {
        // --- Top Bar ---
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .background(Color.White)
                .padding(horizontal = 16.dp, vertical = 12.dp),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.SpaceBetween
        ) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    text = "Open",
                    fontSize = 20.sp,
                    fontWeight = FontWeight.ExtraBold,
                    color = IndigoPrimary
                )
                Text(
                    text = "Invest",
                    fontSize = 20.sp,
                    fontWeight = FontWeight.Bold,
                    color = TextPrimary
                )
                Spacer(modifier = Modifier.width(10.dp))
                // Premium connection indicator pill badge
                val badgeBgColor = when (isConnected) {
                    true -> Color(0xFFECFDF5) // Very light emerald
                    false -> Color(0xFFFEF2F2) // Very light red
                    else -> Color(0xFFF8FAFC) // Slate-50
                }
                val badgeTextColor = when (isConnected) {
                    true -> Color(0xFF047857) // emerald-700
                    false -> Color(0xFFB91C1C) // red-700
                    else -> Color(0xFF475569) // slate-600
                }
                val dotColor = when (isConnected) {
                    true -> Color(0xFF10B981) // emerald-500
                    false -> Color(0xFFEF4444) // red-500
                    else -> Color(0xFF94A3B8) // slate-400
                }
                val statusText = when (isConnected) {
                    true -> "在线"
                    false -> "离线"
                    else -> "检测中"
                }

                Row(
                    verticalAlignment = Alignment.CenterVertically,
                    modifier = Modifier
                        .clip(RoundedCornerShape(50))
                        .background(badgeBgColor)
                        .border(0.5.dp, badgeTextColor.copy(alpha = 0.2f), RoundedCornerShape(50))
                        .padding(horizontal = 8.dp, vertical = 3.dp)
                ) {
                    Box(
                        modifier = Modifier
                            .size(6.dp)
                            .clip(RoundedCornerShape(50))
                            .graphicsLayer {
                                alpha = if (isConnected == true) dotAlpha else 1f
                            }
                            .background(dotColor)
                    )
                    Spacer(modifier = Modifier.width(4.dp))
                    Text(
                        text = statusText,
                        fontSize = 10.sp,
                        fontWeight = FontWeight.Bold,
                        color = badgeTextColor
                    )
                }
            }

            Row(verticalAlignment = Alignment.CenterVertically) {
                IconButton(onClick = { showNews = true }) {
                    Icon(
                        imageVector = Icons.Default.Newspaper,
                        contentDescription = "周末新闻",
                        tint = IndigoPrimary
                    )
                }
                IconButton(onClick = { showSettings = true }) {
                    Icon(
                        imageVector = Icons.Default.Settings,
                        contentDescription = "设置服务器",
                        tint = TextSecondary
                    )
                }
            }
        }

        HorizontalDivider(color = SlateBorder, thickness = 1.dp)

        // --- Content Area ---
        Column(
            modifier = Modifier
                .weight(1f)
                .padding(horizontal = 16.dp)
        ) {
            Spacer(modifier = Modifier.height(12.dp))

            // --- Cash / Asset Summary Card ---
            snapshot?.let { snap ->
                Card(
                    modifier = Modifier.fillMaxWidth(),
                    colors = CardDefaults.cardColors(containerColor = Color.Transparent),
                    shape = RoundedCornerShape(16.dp),
                ) {
                    Column(
                        modifier = Modifier
                            .fillMaxWidth()
                            .background(
                                Brush.verticalGradient(
                                    listOf(DarkGradientStart, DarkGradientEnd)
                                )
                            )
                            .padding(16.dp)
                    ) {
                        Row(
                            modifier = Modifier.fillMaxWidth(),
                            horizontalArrangement = Arrangement.SpaceBetween
                        ) {
                            Column {
                                Text("总资产折算", fontSize = 11.sp, color = Color(0xFF94A3B8))
                                Spacer(modifier = Modifier.height(2.dp))
                                Text(
                                    "¥${String.format("%,.2f", snap.total_assets_cny)}",
                                    fontSize = 22.sp,
                                    fontWeight = FontWeight.ExtraBold,
                                    color = Color.White
                                )
                            }
                            Column(horizontalAlignment = Alignment.End) {
                                Text(
                                    text = if (animatedT2 > 0f) "可用 / T+2现金" else "可用现金",
                                    fontSize = 11.sp,
                                    color = Color(0xFF94A3B8)
                                )
                                Spacer(modifier = Modifier.height(2.dp))
                                Text(
                                    "¥${String.format("%,.2f", animatedCash.toDouble())}",
                                    fontSize = if (animatedT2 > 0f) 18.sp else 20.sp,
                                    fontWeight = FontWeight.Bold,
                                    color = Color(0xFF34D399) // Neon green/emerald
                                )
                                if (animatedT2 > 0f) {
                                    Spacer(modifier = Modifier.height(1.dp))
                                    Text(
                                        "T+2待收: ¥${String.format("%,.2f", animatedT2.toDouble())}",
                                        fontSize = 11.sp,
                                        fontWeight = FontWeight.Medium,
                                        color = Color(0xFFF87171), // Soft red
                                        modifier = Modifier.clickable {
                                            showReleaseT2Confirm = true
                                        }
                                    )
                                }
                            }
                        }

                        Spacer(modifier = Modifier.height(12.dp))

                        // Custom Cash Ratio Bar (Indigo/cyan gradient represents used/invested cash, background dark represents available cash)
                        val cashRatio = if (snap.total_assets_cny > 0) (snap.cash_cny / snap.total_assets_cny).toFloat() else 0f
                        val usedCashRatio = 1f - cashRatio
                        Row(
                            modifier = Modifier.fillMaxWidth(),
                            horizontalArrangement = Arrangement.SpaceBetween
                        ) {
                            Text(
                                text = "现金占比: ${String.format("%.1f", cashRatio * 100)}%",
                                fontSize = 11.sp,
                                color = Color(0xFF94A3B8)
                            )
                            Text(
                                text = "已投持仓: ${String.format("%.1f", usedCashRatio * 100)}%",
                                fontSize = 11.sp,
                                color = Color(0xFF94A3B8)
                            )
                        }
                        Spacer(modifier = Modifier.height(6.dp))
                        Box(
                            modifier = Modifier
                                .fillMaxWidth()
                                .height(6.dp)
                                .clip(RoundedCornerShape(3.dp))
                                .background(Color(0xFF334155)) // slate-700
                        ) {
                            Box(
                                modifier = Modifier
                                    .fillMaxHeight()
                                    .fillMaxWidth(usedCashRatio.coerceIn(0f, 1f))
                                    .background(
                                        Brush.horizontalGradient(
                                            listOf(Color(0xFF6366F1), Color(0xFF06B6D4)) // Indigo to Cyan gradient
                                        )
                                    )
                            )
                        }
                    }
                }
            }

            Spacer(modifier = Modifier.height(12.dp))

            // --- Search Bar ---
            OutlinedTextField(
                value = searchQuery,
                onValueChange = { searchQuery = it },
                modifier = Modifier.fillMaxWidth(),
                placeholder = { Text("搜索标的代码或名称...", fontSize = 14.sp, color = TextSecondary) },
                leadingIcon = { Icon(Icons.Default.Search, contentDescription = "搜索", tint = TextSecondary) },
                trailingIcon = {
                    if (searchQuery.isNotEmpty()) {
                        IconButton(onClick = { searchQuery = "" }) {
                            Icon(Icons.Default.Clear, contentDescription = "清空", tint = TextSecondary)
                        }
                    }
                },
                singleLine = true,
                keyboardOptions = KeyboardOptions(imeAction = ImeAction.Search),
                keyboardActions = KeyboardActions(onSearch = { onSearchSubmit() }),
                shape = RoundedCornerShape(12.dp),
                colors = OutlinedTextFieldDefaults.colors(
                    focusedContainerColor = Color(0xFFF1F5F9), // slate-100
                    unfocusedContainerColor = Color(0xFFF1F5F9),
                    focusedBorderColor = IndigoPrimary,
                    unfocusedBorderColor = Color.Transparent,
                    focusedTextColor = TextPrimary,
                    unfocusedTextColor = TextPrimary
                )
            )

            Spacer(modifier = Modifier.height(14.dp))

            // --- Monitor Title / List Header ---
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically
            ) {
                Text(
                    text = "标的监控",
                    fontSize = 15.sp,
                    fontWeight = FontWeight.Bold,
                    color = TextPrimary
                )
                snapshot?.let {
                    Text(
                        text = "更新时间 ${getDisplayUpdateTime(it.round_time, it.generated_at)}",
                        fontSize = 11.sp,
                        color = TextSecondary
                    )
                }
            }

            Spacer(modifier = Modifier.height(8.dp))

            // --- Monitored List ---
            val filteredRows = remember(snapshot, searchQuery) {
                val rows = snapshot?.rows ?: emptyList()
                if (searchQuery.isEmpty()) {
                    rows
                } else {
                    rows.filter {
                        it.symbol.contains(searchQuery, ignoreCase = true) ||
                                it.name.contains(searchQuery, ignoreCase = true)
                    }
                }
            }

            if (snapshot == null && isLoading) {
                Box(modifier = Modifier.weight(1f).fillMaxWidth(), contentAlignment = Alignment.Center) {
                    CircularProgressIndicator(color = Color(0xFF2F80ED))
                }
            } else {
                PullToRefreshBox(
                    isRefreshing = isLoading,
                    onRefresh = { refreshData() },
                    modifier = Modifier.weight(1f).fillMaxWidth()
                ) {
                    if (filteredRows.isEmpty()) {
                        Box(modifier = Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                            Text("无监控标的数据", color = Color(0xFF667085), fontSize = 14.sp)
                        }
                    } else {
                        LazyColumn(
                            modifier = Modifier.fillMaxSize(),
                            verticalArrangement = Arrangement.spacedBy(10.dp)
                        ) {
                            items(filteredRows, key = { it.symbol }) { row ->
                                val dismissState = rememberSwipeToDismissBoxState(
                                    confirmValueChange = { dismissValue ->
                                        when (dismissValue) {
                                            SwipeToDismissBoxValue.StartToEnd -> {
                                                if (row.is_holding) {
                                                    activeTradeSymbol = row.symbol
                                                    activeTradeRow = row
                                                    activeTradeVerdict = "BUY"
                                                    activeTradeSuggestedAlloc = row.operation?.suggested_alloc_cny ?: 0.0
                                                } else {
                                                    NetworkClient.deleteFromWatchlist(row.symbol) { res ->
                                                        res.fold(
                                                            onSuccess = { msg ->
                                                                Toast.makeText(context, msg, Toast.LENGTH_SHORT).show()
                                                                refreshData()
                                                            },
                                                            onFailure = { err ->
                                                                Toast.makeText(context, "删除失败: ${err.message}", Toast.LENGTH_LONG).show()
                                                            }
                                                        )
                                                    }
                                                }
                                                false
                                            }
                                            SwipeToDismissBoxValue.EndToStart -> {
                                                if (row.is_holding) {
                                                    activeTradeSymbol = row.symbol
                                                    activeTradeRow = row
                                                    activeTradeVerdict = "SELL"
                                                    activeTradeSuggestedAlloc = 0.0
                                                } else {
                                                    NetworkClient.deleteFromWatchlist(row.symbol) { res ->
                                                        res.fold(
                                                            onSuccess = { msg ->
                                                                Toast.makeText(context, msg, Toast.LENGTH_SHORT).show()
                                                                refreshData()
                                                            },
                                                            onFailure = { err ->
                                                                Toast.makeText(context, "删除失败: ${err.message}", Toast.LENGTH_LONG).show()
                                                            }
                                                        )
                                                    }
                                                }
                                                false
                                            }
                                            SwipeToDismissBoxValue.Settled -> false
                                        }
                                    }
                                )

                                SwipeToDismissBox(
                                    state = dismissState,
                                    backgroundContent = {
                                        val direction = dismissState.dismissDirection
                                        val alignment = when (direction) {
                                            SwipeToDismissBoxValue.StartToEnd -> Alignment.CenterStart
                                            SwipeToDismissBoxValue.EndToStart -> Alignment.CenterEnd
                                            else -> Alignment.Center
                                        }
                                        val color = when (direction) {
                                            SwipeToDismissBoxValue.StartToEnd -> {
                                                if (row.is_holding) Color(0xFFE8F8EE) else Color(0xFFFEECEB)
                                            }
                                            SwipeToDismissBoxValue.EndToStart -> Color(0xFFFEECEB)
                                            else -> Color.Transparent
                                        }
                                        val labelText = when (direction) {
                                            SwipeToDismissBoxValue.StartToEnd -> if (row.is_holding) "买入" else "取消关注"
                                            SwipeToDismissBoxValue.EndToStart -> if (row.is_holding) "卖出" else "取消关注"
                                            else -> ""
                                        }
                                        val textColor = when (direction) {
                                            SwipeToDismissBoxValue.StartToEnd -> if (row.is_holding) Color(0xFF089761) else Color(0xFFD92D20)
                                            SwipeToDismissBoxValue.EndToStart -> Color(0xFFD92D20)
                                            else -> Color.Transparent
                                        }
                                        val icon = when (direction) {
                                            SwipeToDismissBoxValue.StartToEnd -> if (row.is_holding) Icons.Default.Add else Icons.Default.Delete
                                            SwipeToDismissBoxValue.EndToStart -> if (row.is_holding) Icons.Default.Remove else Icons.Default.Delete
                                            else -> null
                                        }

                                        Box(
                                            modifier = Modifier
                                                .fillMaxSize()
                                                .clip(RoundedCornerShape(10.dp))
                                                .background(color)
                                                .padding(horizontal = 20.dp),
                                            contentAlignment = alignment
                                        ) {
                                            if (icon != null) {
                                                Row(verticalAlignment = Alignment.CenterVertically) {
                                                    if (direction == SwipeToDismissBoxValue.StartToEnd) {
                                                        Icon(icon, contentDescription = labelText, tint = textColor)
                                                        Spacer(modifier = Modifier.width(8.dp))
                                                        Text(labelText, color = textColor, fontWeight = FontWeight.Bold, fontSize = 14.sp)
                                                    } else {
                                                        Text(labelText, color = textColor, fontWeight = FontWeight.Bold, fontSize = 14.sp)
                                                        Spacer(modifier = Modifier.width(8.dp))
                                                        Icon(icon, contentDescription = labelText, tint = textColor)
                                                    }
                                                }
                                            }
                                        }
                                    },
                                    content = {
                                        StockCard(
                                            row = row,
                                            onClick = {
                                                activeCommitteeSymbol = row.symbol
                                                activeCommitteeRow = row
                                            },
                                            onLongClick = {
                                                if (row.is_holding || row.units > 0.0) {
                                                    Toast.makeText(context, "${row.name}有持仓，不能删除", Toast.LENGTH_SHORT).show()
                                                } else {
                                                    deleteConfirmSymbol = row.symbol
                                                    deleteConfirmName = row.name
                                                }
                                            }
                                        )
                                    }
                                )
                            }
                        }
                    }
                }
            }
        }

        // --- Bottom Stock Selection Recommendation Bar ---
        val stocksRecommendation = remember(selection) {
            selection?.stocks?.sortedByDescending { it.score ?: 0.0 }?.take(6) ?: emptyList()
        }
        if (stocksRecommendation.isNotEmpty()) {
            Column(
                modifier = Modifier
                    .fillMaxWidth()
                    .background(Color.White)
                    .padding(vertical = 8.dp)
            ) {
                HorizontalDivider(color = SlateBorder, thickness = 1.dp)
                Spacer(modifier = Modifier.height(6.dp))
                Text(
                    text = "选股推荐",
                    fontSize = 12.sp,
                    fontWeight = FontWeight.Bold,
                    color = TextSecondary,
                    modifier = Modifier.padding(horizontal = 16.dp, vertical = 2.dp)
                )
                Spacer(modifier = Modifier.height(6.dp))
                LazyRow(
                    modifier = Modifier.fillMaxWidth(),
                    contentPadding = PaddingValues(horizontal = 16.dp, vertical = 4.dp),
                    horizontalArrangement = Arrangement.spacedBy(8.dp)
                ) {
                    items(stocksRecommendation) { stock ->
                        val score = stock.score ?: 0.0
                        val dotColor = when {
                            score >= 80.0 -> AShareRiseRed
                            score >= 60.0 -> Color(0xFFD97706) // orange
                            else -> TextSecondary
                        }
                        Card(
                            modifier = Modifier
                                .clickable { selectedSelectionStock = stock },
                            shape = RoundedCornerShape(10.dp),
                            colors = CardDefaults.cardColors(containerColor = Color.White),
                            border = BorderStroke(1.dp, SlateBorder),
                            elevation = CardDefaults.cardElevation(defaultElevation = 1.dp)
                        ) {
                            Row(
                                modifier = Modifier.padding(horizontal = 12.dp, vertical = 8.dp),
                                verticalAlignment = Alignment.CenterVertically
                            ) {
                                Box(
                                    modifier = Modifier
                                        .size(6.dp)
                                        .clip(RoundedCornerShape(50))
                                        .background(dotColor)
                                )
                                Spacer(modifier = Modifier.width(6.dp))
                                Text(
                                    text = stock.name ?: stock.symbol,
                                    fontSize = 12.sp,
                                    fontWeight = FontWeight.Bold,
                                    color = TextPrimary
                                )
                                Spacer(modifier = Modifier.width(6.dp))
                                Text(
                                    text = String.format("%.0f分", score),
                                    fontSize = 10.sp,
                                    color = TextSecondary,
                                    fontWeight = FontWeight.Medium
                                )
                            }
                        }
                    }
                }
            }
        }
    }

    // --- Overlay Dialogs ---

    if (showReleaseT2Confirm) {
        AlertDialog(
            onDismissRequest = { showReleaseT2Confirm = false },
            title = { Text("释放T+2代收资金", fontWeight = FontWeight.Bold) },
            text = { Text("确定要将港股通 T+2 代收资金 ¥${String.format("%,.2f", snapshot?.t2_pending_cash_cny ?: 0.0)} 释放为可用资金吗？此操作将更新本地持仓配置。") },
            confirmButton = {
                TextButton(
                    onClick = {
                        showReleaseT2Confirm = false
                        val currentSnap = snapshot
                        if (currentSnap != null && currentSnap.t2_pending_cash_cny > 0.0) {
                            val originalT2 = currentSnap.t2_pending_cash_cny
                            val originalCash = currentSnap.cash_cny
                            
                            // Start animation
                            displayCashTarget = originalCash + originalT2
                            displayT2Target = 0.0
                            
                            coroutineScope.launch {
                                delay(1000) // Wait for animation
                                val updatedSnap = currentSnap.copy(
                                    cash_cny = originalCash + originalT2,
                                    t2_pending_cash_cny = 0.0
                                )
                                snapshot = updatedSnap
                                
                                // Update resolved_snapshot.json
                                val snapJson = gson.toJson(updatedSnap)
                                saveLocalFile(context, "resolved_snapshot.json", snapJson)
                                
                                // Update market_monitor_config.json
                                val configStr = readLocalFile(context, "market_monitor_config.json")
                                if (configStr != null) {
                                    try {
                                        val configObj = JSONObject(configStr)
                                        val configCash = configObj.optDouble("cash", 0.0)
                                        val configT2 = configObj.optDouble("t2_pending_cash", 0.0)
                                        configObj.put("cash", configCash + configT2)
                                        configObj.put("t2_pending_cash", 0.0)
                                        saveLocalFile(context, "market_monitor_config.json", configObj.toString(2))
                                        
                                        // Also ask for upload to cloud if remote url exists
                                        if (NetworkClient.getBaseUrl().isNotEmpty()) {
                                            NetworkClient.uploadMonitorConfig(configObj.toString()) { res ->
                                                coroutineScope.launch(Dispatchers.Main) {
                                                    res.fold(
                                                        onSuccess = {
                                                            Toast.makeText(context, "已更新且同步至云端", Toast.LENGTH_SHORT).show()
                                                        },
                                                        onFailure = { err ->
                                                            Toast.makeText(context, "本地已更新，但同步云端失败: ${err.message}", Toast.LENGTH_LONG).show()
                                                        }
                                                    )
                                                }
                                            }
                                        } else {
                                            Toast.makeText(context, "本地持仓数据更新成功！", Toast.LENGTH_SHORT).show()
                                        }
                                    } catch (e: Exception) {
                                        Log.e("MainActivity", "Failed to update market_monitor_config.json", e)
                                        Toast.makeText(context, "更新本地持仓配置失败: ${e.message}", Toast.LENGTH_LONG).show()
                                    }
                                } else {
                                    Toast.makeText(context, "未找到本地持仓配置文件", Toast.LENGTH_LONG).show()
                                }
                            }
                        }
                    }
                ) {
                    Text("确定", color = Color(0xFF10B981))
                }
            },
            dismissButton = {
                TextButton(onClick = { showReleaseT2Confirm = false }) {
                    Text("取消", color = Color.Gray)
                }
            }
        )
    }

    if (showSettings) {
        SettingsDialog(
            currentUrl = serverUrl,
            onClose = { showSettings = false },
            onSave = { newUrl ->
                serverUrl = newUrl
                onSaveUrl(newUrl)
                showSettings = false
                refreshData()
            },
            onRefresh = {
                refreshData()
            },
            onImportConfig = rebuildSnapshotFromConfig
        )
    }

    if (showNews) {
        NewsDialog(
            news = news,
            snapshot = snapshot,
            onClose = { showNews = false },
            onViewDetails = { leader ->
                val symbol = leader.symbol ?: leader.code ?: ""
                val name = leader.name ?: ""
                val existing = snapshot?.rows?.find { it.symbol.uppercase() == symbol.uppercase() || it.name == name }
                if (existing != null) {
                    activeCommitteeSymbol = existing.symbol
                    activeCommitteeRow = existing
                } else {
                    val dummy = HoldingRow(
                        symbol = symbol,
                        name = name,
                        market = if (symbol.length == 5) "hk" else "a",
                        state = "watch",
                        price = HoldingRowPrice(0.0, 0.0, 0.0),
                        operation = Operation(verdict = "HOLD"),
                        _is_resolving = true
                    )
                    activeCommitteeSymbol = symbol
                    activeCommitteeRow = dummy
                }
                showNews = false
            }
        )
    }

    selectedSelectionStock?.let { stock ->
        SelectionStockDialog(
            stock = stock,
            onClose = { selectedSelectionStock = null },
            onWatchlistAdded = { msg ->
                Toast.makeText(context, msg, Toast.LENGTH_SHORT).show()
                refreshData()
            }
        )
    }

    activeCommitteeSymbol?.let { symbol ->
        activeCommitteeRow?.let { row ->
            val initialWatchlist = remember(snapshot) {
                snapshot?.rows?.map { it.symbol.uppercase() }?.toSet() ?: emptySet()
            }
            val inWatchlist = symbol.uppercase() in initialWatchlist

            val holdingsList = snapshot?.rows?.filter { it.is_holding }?.map { r ->
                mapOf(
                    "symbol" to r.symbol,
                    "name" to r.name,
                    "weight_pct" to r.position_pct,
                    "cost" to r.cost,
                    "current_price" to r.price.current
                )
            } ?: emptyList()
            val holdingsJson = Gson().toJson(holdingsList)

            CommitteeAnalysisDialog(
                symbol = symbol,
                row = row,
                inWatchlist = inWatchlist,
                onClose = {
                    activeCommitteeSymbol = null
                    activeCommitteeRow = null
                },
                onWatchlistAdded = { msg ->
                    Toast.makeText(context, msg, Toast.LENGTH_SHORT).show()
                    refreshData()
                },
                onTradeExecuted = { msg ->
                    Toast.makeText(context, msg, Toast.LENGTH_LONG).show()
                    refreshData()
                },
                totalAssets = snapshot?.total_assets_cny ?: 100000.0,
                cash = snapshot?.cash_cny ?: 0.0,
                t2PendingCash = snapshot?.t2_pending_cash_cny ?: 0.0,
                holdingsJson = holdingsJson
            )
        }
    }

    if (deleteConfirmSymbol != null) {
        AlertDialog(
            onDismissRequest = {
                deleteConfirmSymbol = null
                deleteConfirmName = null
            },
            title = {
                Text(text = "确认删除", fontWeight = FontWeight.Bold, fontSize = 16.sp)
            },
            text = {
                Text(text = "确定要删除自选标的 ${deleteConfirmName ?: ""} (${deleteConfirmSymbol ?: ""}) 吗？")
            },
            confirmButton = {
                TextButton(
                    onClick = {
                        val sym = deleteConfirmSymbol!!
                        deleteConfirmSymbol = null
                        deleteConfirmName = null
                        NetworkClient.deleteFromWatchlist(sym) { res ->
                            res.fold(
                                onSuccess = { msg ->
                                    Toast.makeText(context, msg, Toast.LENGTH_SHORT).show()
                                    refreshData()
                                },
                                onFailure = { err ->
                                    Toast.makeText(context, "删除失败: ${err.message}", Toast.LENGTH_LONG).show()
                                }
                            )
                        }
                    }
                ) {
                    Text("确认", color = AShareRiseRed, fontWeight = FontWeight.Bold)
                }
            },
            dismissButton = {
                TextButton(
                    onClick = {
                        deleteConfirmSymbol = null
                        deleteConfirmName = null
                    }
                ) {
                    Text("取消", color = TextSecondary)
                }
            }
        )
    }

    if (activeTradeSymbol != null && activeTradeRow != null && activeTradeVerdict != null) {
        TradeExecutionDialog(
            row = activeTradeRow!!,
            verdict = activeTradeVerdict!!,
            suggestedAlloc = activeTradeSuggestedAlloc,
            onClose = {
                activeTradeSymbol = null
                activeTradeRow = null
                activeTradeVerdict = null
            },
            onDone = { msg ->
                activeTradeSymbol = null
                activeTradeRow = null
                activeTradeVerdict = null
                Toast.makeText(context, msg, Toast.LENGTH_LONG).show()
                refreshData()
            }
        )
    }
}

// --- Card Item Composable ---
@OptIn(ExperimentalFoundationApi::class)
@Composable
fun StockCard(row: HoldingRow, onClick: () -> Unit, onLongClick: (() -> Unit)? = null) {
    val isActionRequired = row.state == "action_required"
    val isBuy = (row.operation?.verdict ?: "HOLD").uppercase() in listOf("BUY", "ACCUMULATE")
    val isSell = (row.operation?.verdict ?: "HOLD").uppercase() in listOf("SELL", "TRIM", "EXIT")
    val verdictLabel = row.operation?.verdict ?: "HOLD"

    val cardBorderColor = when {
        isActionRequired -> AShareRiseRed
        isBuy -> AShareRiseRed.copy(alpha = 0.5f)
        else -> SlateBorder
    }

    val stateColor = when (row.state) {
        "action_required" -> AShareRiseRed
        "trigger_confirmed" -> Color(0xFFD97706) // orange-600
        "watch_trigger" -> IndigoPrimary
        else -> TextSecondary
    }

    val isRise = row.price.change_pct >= 0
    val changeTextColor = if (isRise) AShareRiseRed else AShareFallGreen
    val changeBgColor = if (isRise) LightRiseRed else LightFallGreen
    val priceChangeSign = if (isRise) "+" else ""

    Card(
        modifier = Modifier
            .fillMaxWidth()
            .shake(isActionRequired)
            .combinedClickable(
                onClick = onClick,
                onDoubleClick = onClick,
                onLongClick = onLongClick
            ),
        shape = RoundedCornerShape(12.dp),
        colors = CardDefaults.cardColors(containerColor = Color.White),
        border = BorderStroke(if (isActionRequired) 1.5.dp else 1.dp, cardBorderColor)
    ) {
        Column(modifier = Modifier.padding(14.dp)) {
            // Header Row
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically
            ) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Box(
                        modifier = Modifier
                            .size(7.dp)
                            .clip(RoundedCornerShape(50))
                            .background(stateColor)
                    )
                    Spacer(modifier = Modifier.width(6.dp))
                    Text(
                        text = row.name,
                        fontSize = 14.sp,
                        fontWeight = FontWeight.Bold,
                        color = TextPrimary
                    )
                    Spacer(modifier = Modifier.width(6.dp))
                    Text(
                        text = row.symbol,
                        fontSize = 11.sp,
                        color = TextSecondary
                    )
                }

                // State indicator label
                val labelBg = if (isActionRequired) LightRiseRed else Color(0xFFF1F5F9)
                val labelText = if (isActionRequired) "需要操作" else "观察中"
                val labelTextColor = if (isActionRequired) AShareRiseRed else TextSecondary
                Box(
                    modifier = Modifier
                        .clip(RoundedCornerShape(6.dp))
                        .background(labelBg)
                        .padding(horizontal = 8.dp, vertical = 3.dp)
                ) {
                    Text(
                        text = labelText,
                        fontSize = 10.sp,
                        fontWeight = FontWeight.Bold,
                        color = labelTextColor
                    )
                }
            }

            Spacer(modifier = Modifier.height(10.dp))

            // Body content Row
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.Bottom
            ) {
                Column {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Text(
                            text = String.format("%.2f", row.price.current),
                            fontSize = 20.sp,
                            fontWeight = FontWeight.ExtraBold,
                            color = TextPrimary
                        )
                        Spacer(modifier = Modifier.width(8.dp))
                        // Price change capsule chip
                        Box(
                            modifier = Modifier
                                .clip(RoundedCornerShape(6.dp))
                                .background(changeBgColor)
                                .padding(horizontal = 6.dp, vertical = 2.dp)
                        ) {
                            Text(
                                text = "$priceChangeSign${String.format("%.2f", row.price.change_pct)}%",
                                fontSize = 11.sp,
                                fontWeight = FontWeight.Bold,
                                color = changeTextColor
                            )
                        }
                    }
                    Spacer(modifier = Modifier.height(6.dp))
                    Text(
                        text = "建议配资: ¥${String.format("%,.0f", row.operation?.suggested_alloc_cny ?: 0.0)}",
                        fontSize = 11.sp,
                        color = TextSecondary
                    )
                }

                Column(horizontalAlignment = Alignment.End) {
                    // Verdict Badge
                    val verdictBg = when {
                        isBuy -> LightRiseRed
                        isSell -> LightFallGreen
                        else -> Color(0xFFF1F5F9)
                    }
                    val verdictColor = when {
                        isBuy -> AShareRiseRed
                        isSell -> AShareFallGreen
                        else -> TextSecondary
                    }
                    Box(
                        modifier = Modifier
                            .clip(RoundedCornerShape(6.dp))
                            .background(verdictBg)
                            .padding(horizontal = 8.dp, vertical = 4.dp)
                    ) {
                        Text(
                            text = verdictLabel,
                            fontSize = 12.sp,
                            fontWeight = FontWeight.ExtraBold,
                            color = verdictColor
                        )
                    }
                }
            }

            val buyCriteria = row.buy_criteria
            val exitPoints = row.exit_points
            val hasBuyPoints = buyCriteria?.pullback_price != null || buyCriteria?.breakout_price != null || buyCriteria?.reentry_price != null
            val hasExitPoints = exitPoints?.stop_loss_price != null || exitPoints?.take_profit_price != null

            if (row._is_resolving) {
                Spacer(modifier = Modifier.height(8.dp))
                Row(verticalAlignment = Alignment.CenterVertically) {
                    CircularProgressIndicator(
                        modifier = Modifier.size(12.dp),
                        strokeWidth = 1.5.dp,
                        color = AShareRiseRed
                    )
                    Spacer(modifier = Modifier.width(6.dp))
                    Text(
                        text = "本地委员会分析中...",
                        fontSize = 10.sp,
                        fontWeight = FontWeight.Medium,
                        color = TextSecondary
                    )
                }
            } else if (hasBuyPoints || hasExitPoints) {
                Spacer(modifier = Modifier.height(8.dp))
                Column(
                    modifier = Modifier.fillMaxWidth(),
                    verticalArrangement = Arrangement.spacedBy(3.dp)
                ) {
                    if (hasBuyPoints) {
                        val buyPointsText = buildString {
                            val pullback = buyCriteria?.pullback_price
                            val reentry = buyCriteria?.reentry_price
                            val pbPrice = if (pullback != null && pullback > 0.0) pullback else if (reentry != null && reentry > 0.0) reentry else null
                            pbPrice?.let { append("回调:¥${String.format("%.2f", it)} ") }
                            buyCriteria?.breakout_price?.let { append("突破:¥${String.format("%.2f", it)} ") }
                        }.trim()
                        Text(
                            text = "买点: $buyPointsText",
                            fontSize = 11.sp,
                            fontWeight = FontWeight.Bold,
                            color = AShareRiseRed
                        )
                    }
                    if (hasExitPoints) {
                        val exitPointsText = buildString {
                            exitPoints?.stop_loss_price?.let { append("止损:¥${String.format("%.2f", it)} ") }
                            exitPoints?.take_profit_price?.let { append("止盈:¥${String.format("%.2f", it)} ") }
                        }.trim()
                        Text(
                            text = "出场: $exitPointsText",
                            fontSize = 11.sp,
                            fontWeight = FontWeight.Bold,
                            color = AShareFallGreen
                        )
                    }
                }
            }
        }
    }
}

// --- Settings Dialog ---
@Composable
fun SettingsDialog(
    currentUrl: String,
    onClose: () -> Unit,
    onSave: (String) -> Unit,
    onRefresh: () -> Unit = {},
    onImportConfig: (String) -> Unit = {}
) {
    val context = LocalContext.current
    val prefs = remember { context.getSharedPreferences("open_invest_prefs", Context.MODE_PRIVATE) }

    var url by remember { mutableStateOf(prefs.getString("server_url", currentUrl) ?: currentUrl) }
    var apiKey by remember { mutableStateOf(prefs.getString("llm_api_key", "") ?: "") }
    var llmBaseUrl by remember { mutableStateOf(prefs.getString("llm_base_url", "") ?: "") }
    var model by remember { mutableStateOf(prefs.getString("llm_model", "gemini-2.5-pro") ?: "gemini-2.5-pro") }
    var autoRefresh by remember { mutableStateOf(prefs.getBoolean("auto_refresh_enabled", false)) }
    var interval by remember { mutableStateOf(prefs.getInt("auto_refresh_interval", 30).toString()) }
    var refreshNews by remember { mutableStateOf(prefs.getBoolean("auto_refresh_news_enabled", true)) }
    var refreshSelection by remember { mutableStateOf(prefs.getBoolean("auto_refresh_selection_enabled", true)) }
    var refreshPrices by remember { mutableStateOf(prefs.getBoolean("auto_refresh_prices_enabled", true)) }
    var notificationsEnabled by remember { mutableStateOf(prefs.getBoolean("notifications_enabled", true)) }
    var committeeAnalysisEnabled by remember { mutableStateOf(prefs.getBoolean("committee_analysis_enabled", true)) }
    var retentionLimit by remember { mutableStateOf(prefs.getInt("cache_retention_limit_months", 2).toString()) }

    var remoteCrawlerInterval by remember { mutableStateOf(prefs.getInt("remote_crawler_interval_minutes", 60).toString()) }
    var remoteTargetRefresh by remember { mutableStateOf(prefs.getBoolean("remote_target_refresh_enabled", true)) }
    var remoteNewsRefresh by remember { mutableStateOf(prefs.getBoolean("remote_news_refresh_enabled", true)) }

    LaunchedEffect(Unit) {
        NetworkClient.getCrawlerSettings { res ->
            res.fold(
                onSuccess = { settings ->
                    remoteCrawlerInterval = settings.frequency_minutes.toString()
                    remoteTargetRefresh = settings.target_refresh_enabled
                    remoteNewsRefresh = settings.news_refresh_enabled
                },
                onFailure = { err ->
                    Log.e("SettingsDialog", "Failed to fetch remote crawler settings", err)
                }
            )
        }
    }

    var connectionTestState by remember { mutableStateOf<Boolean?>(null) }
    var testingConnection by remember { mutableStateOf(false) }
    var clearingCache by remember { mutableStateOf(false) }
    var showSyncConfirmDialog by remember { mutableStateOf(false) }

    val permissionLauncher = rememberLauncherForActivityResult(
        contract = ActivityResultContracts.RequestPermission()
    ) { isGranted ->
        notificationsEnabled = isGranted
        if (!isGranted) {
            Toast.makeText(context, "未获得通知权限，已关闭通知", Toast.LENGTH_SHORT).show()
        }
    }

    val monitorConfigPicker = rememberLauncherForActivityResult(
        contract = ActivityResultContracts.GetContent()
    ) { uri ->
        uri?.let {
            val content = readTextFromUri(context, it)
            if (content != null) {
                saveLocalFile(context, "market_monitor_config.json", content)
                onImportConfig(content)
                NetworkClient.uploadMonitorConfig(content) { res ->
                    res.fold(
                        onSuccess = {
                            Toast.makeText(context, "仓位配置文件导入且上传成功！", Toast.LENGTH_LONG).show()
                            onRefresh()
                        },
                        onFailure = { err ->
                            Toast.makeText(context, "仓位配置已保存至本地，但上传失败: ${err.message}", Toast.LENGTH_LONG).show()
                        }
                    )
                }
            } else {
                Toast.makeText(context, "读取仓位配置文件失败", Toast.LENGTH_SHORT).show()
            }
        }
    }

    val exitParamsPicker = rememberLauncherForActivityResult(
        contract = ActivityResultContracts.GetContent()
    ) { uri ->
        uri?.let {
            val content = readTextFromUri(context, it)
            if (content != null) {
                saveLocalFile(context, "weekly_exit_param_optimization.json", content)
                NetworkClient.uploadExitParams(content) { res ->
                    res.fold(
                        onSuccess = {
                            Toast.makeText(context, "每周止盈参数导入且上传成功！", Toast.LENGTH_LONG).show()
                            onRefresh()
                        },
                        onFailure = { err ->
                            Toast.makeText(context, "参数配置已保存至本地，但上传失败: ${err.message}", Toast.LENGTH_LONG).show()
                        }
                    )
                }
            } else {
                Toast.makeText(context, "读取每周止盈参数 file 失败", Toast.LENGTH_SHORT).show()
            }
        }
    }

    Dialog(onDismissRequest = onClose) {
        Card(
            modifier = Modifier
                .fillMaxWidth()
                .heightIn(max = 640.dp),
            shape = RoundedCornerShape(16.dp),
            colors = CardDefaults.cardColors(containerColor = Color.White),
            elevation = CardDefaults.cardElevation(defaultElevation = 8.dp)
        ) {
            Column(
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(20.dp)
            ) {
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Text("系统与连接设置", fontSize = 18.sp, fontWeight = FontWeight.Bold, color = TextPrimary)
                    
                    // Display background service status pill
                    val serviceRunning = AutoRefreshService.isRunning
                    val serviceBg = if (serviceRunning) Color(0xFFECFDF5) else Color(0xFFF1F5F9)
                    val serviceTextCol = if (serviceRunning) Color(0xFF047857) else TextSecondary
                    val serviceText = if (serviceRunning) "后台服务: 运行中" else "后台服务: 已关闭"
                    Box(
                        modifier = Modifier
                            .clip(RoundedCornerShape(50))
                            .background(serviceBg)
                            .padding(horizontal = 8.dp, vertical = 3.dp)
                    ) {
                        Text(serviceText, fontSize = 9.sp, fontWeight = FontWeight.Bold, color = serviceTextCol)
                    }
                }
                Spacer(modifier = Modifier.height(14.dp))

                // Scrollable container for settings options
                Column(
                    modifier = Modifier
                        .weight(1f)
                        .fillMaxWidth()
                        .verticalScroll(rememberScrollState())
                ) {
                    // Help panel
                    Box(
                        modifier = Modifier
                            .fillMaxWidth()
                            .clip(RoundedCornerShape(10.dp))
                            .background(Color(0xFFF1F5F9))
                            .border(0.5.dp, SlateBorder, RoundedCornerShape(10.dp))
                            .padding(10.dp)
                    ) {
                        Column {
                            Row(verticalAlignment = Alignment.CenterVertically) {
                                Icon(
                                    imageVector = Icons.Default.Info,
                                    contentDescription = "连接帮助",
                                    tint = IndigoPrimary,
                                    modifier = Modifier.size(14.dp)
                                )
                                Spacer(modifier = Modifier.width(4.dp))
                                Text(
                                    text = "如何让 App 连接电脑端服务？",
                                    fontSize = 11.sp,
                                    fontWeight = FontWeight.Bold,
                                    color = TextPrimary
                                )
                            }
                            Spacer(modifier = Modifier.height(4.dp))
                            Text(
                                text = "方式 1 (局域网 Wi-Fi)：将手机与电脑连接在同一个 Wi-Fi。在电脑命令行运行 ipconfig 找到 IPv4 地址（如 192.168.1.100），然后在下方输入 http://<电脑IP>:8765",
                                fontSize = 10.sp,
                                color = TextSecondary,
                                lineHeight = 13.sp
                            )
                            Spacer(modifier = Modifier.height(2.dp))
                            Text(
                                text = "方式 2 (USB 线)：手机 USB 连接电脑并开启调试，在电脑端运行命令 adb reverse tcp:8765 tcp:8765，然后在下方输入 http://127.0.0.1:8765",
                                fontSize = 10.sp,
                                color = TextSecondary,
                                lineHeight = 13.sp
                            )
                        }
                    }

                    Spacer(modifier = Modifier.height(14.dp))

                    // Section 1: Server Connection
                    Text("服务器连接", fontSize = 13.sp, fontWeight = FontWeight.Bold, color = IndigoPrimary)
                    Spacer(modifier = Modifier.height(6.dp))
                    OutlinedTextField(
                        value = url,
                        onValueChange = { url = it; connectionTestState = null },
                        modifier = Modifier.fillMaxWidth(),
                        label = { Text("API Backend URL") },
                        singleLine = true,
                        shape = RoundedCornerShape(10.dp),
                        colors = OutlinedTextFieldDefaults.colors(
                            focusedBorderColor = IndigoPrimary,
                            focusedLabelColor = IndigoPrimary,
                            focusedTextColor = TextPrimary,
                            unfocusedTextColor = TextPrimary
                        )
                    )
                    Spacer(modifier = Modifier.height(6.dp))
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.SpaceBetween
                    ) {
                        TextButton(
                            onClick = {
                                testingConnection = true
                                connectionTestState = null
                                NetworkClient.setBaseUrl(url)
                                NetworkClient.testConnection { res ->
                                    testingConnection = false
                                    connectionTestState = res.getOrDefault(false)
                                }
                            },
                            enabled = !testingConnection,
                            colors = ButtonDefaults.textButtonColors(contentColor = IndigoPrimary)
                        ) {
                            Text(if (testingConnection) "测试中..." else "测试连接", fontSize = 13.sp, fontWeight = FontWeight.Bold)
                        }
                        connectionTestState?.let { success ->
                            Text(
                                text = if (success) "连接成功" else "连接失败",
                                color = if (success) Color(0xFF10B981) else AShareRiseRed,
                                fontSize = 13.sp,
                                fontWeight = FontWeight.Bold
                            )
                        }
                    }

                    Spacer(modifier = Modifier.height(12.dp))
                    HorizontalDivider(color = SlateBorder, thickness = 1.dp)
                    Spacer(modifier = Modifier.height(12.dp))

                    // Section 2: LLM Config
                    Text("LLM 模型配置", fontSize = 13.sp, fontWeight = FontWeight.Bold, color = IndigoPrimary)
                    Spacer(modifier = Modifier.height(6.dp))
                    OutlinedTextField(
                        value = apiKey,
                        onValueChange = { apiKey = it },
                        modifier = Modifier.fillMaxWidth(),
                        label = { Text("LLM API Key") },
                        singleLine = true,
                        shape = RoundedCornerShape(10.dp),
                        visualTransformation = PasswordVisualTransformation(),
                        colors = OutlinedTextFieldDefaults.colors(
                            focusedBorderColor = IndigoPrimary,
                            focusedLabelColor = IndigoPrimary,
                            focusedTextColor = TextPrimary,
                            unfocusedTextColor = TextPrimary
                        )
                    )
                    Spacer(modifier = Modifier.height(8.dp))
                    OutlinedTextField(
                        value = llmBaseUrl,
                        onValueChange = { llmBaseUrl = it },
                        modifier = Modifier.fillMaxWidth(),
                        label = { Text("LLM API Base URL (可选)") },
                        singleLine = true,
                        shape = RoundedCornerShape(10.dp),
                        colors = OutlinedTextFieldDefaults.colors(
                            focusedBorderColor = IndigoPrimary,
                            focusedLabelColor = IndigoPrimary,
                            focusedTextColor = TextPrimary,
                            unfocusedTextColor = TextPrimary
                        )
                    )
                    Spacer(modifier = Modifier.height(8.dp))
                    OutlinedTextField(
                        value = model,
                        onValueChange = { model = it },
                        modifier = Modifier.fillMaxWidth(),
                        label = { Text("LLM Model Name") },
                        singleLine = true,
                        shape = RoundedCornerShape(10.dp),
                        colors = OutlinedTextFieldDefaults.colors(
                            focusedBorderColor = IndigoPrimary,
                            focusedLabelColor = IndigoPrimary,
                            focusedTextColor = TextPrimary,
                            unfocusedTextColor = TextPrimary
                        )
                    )

                    Spacer(modifier = Modifier.height(12.dp))
                    HorizontalDivider(color = SlateBorder, thickness = 1.dp)
                    Spacer(modifier = Modifier.height(12.dp))

                    // Section 3: Auto Refresh
                    Text("数据自动刷新", fontSize = 13.sp, fontWeight = FontWeight.Bold, color = IndigoPrimary)
                    Spacer(modifier = Modifier.height(6.dp))
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.SpaceBetween
                    ) {
                        Text("开启自动刷新", fontSize = 14.sp, color = TextPrimary)
                        Switch(
                            checked = autoRefresh, 
                            onCheckedChange = { autoRefresh = it },
                            colors = SwitchDefaults.colors(checkedTrackColor = IndigoPrimary)
                        )
                    }
                    if (autoRefresh) {
                        Spacer(modifier = Modifier.height(6.dp))
                        OutlinedTextField(
                            value = interval,
                            onValueChange = { interval = it },
                            modifier = Modifier.fillMaxWidth(),
                            label = { Text("刷新频率 (秒)") },
                            singleLine = true,
                            shape = RoundedCornerShape(10.dp),
                            colors = OutlinedTextFieldDefaults.colors(
                                focusedBorderColor = IndigoPrimary,
                                focusedLabelColor = IndigoPrimary,
                                focusedTextColor = TextPrimary,
                                unfocusedTextColor = TextPrimary
                            )
                        )
                        Spacer(modifier = Modifier.height(6.dp))
                        Row(
                            modifier = Modifier.fillMaxWidth(),
                            verticalAlignment = Alignment.CenterVertically,
                            horizontalArrangement = Arrangement.SpaceBetween
                        ) {
                            Text("刷新周末新闻", fontSize = 14.sp, color = TextPrimary)
                            Switch(
                                checked = refreshNews, 
                                onCheckedChange = { refreshNews = it },
                                colors = SwitchDefaults.colors(checkedTrackColor = IndigoPrimary)
                            )
                        }
                        Row(
                            modifier = Modifier.fillMaxWidth(),
                            verticalAlignment = Alignment.CenterVertically,
                            horizontalArrangement = Arrangement.SpaceBetween
                        ) {
                            Text("刷新每日选股", fontSize = 14.sp, color = TextPrimary)
                            Switch(
                                checked = refreshSelection, 
                                onCheckedChange = { refreshSelection = it },
                                colors = SwitchDefaults.colors(checkedTrackColor = IndigoPrimary)
                            )
                        }
                        Row(
                            modifier = Modifier.fillMaxWidth(),
                            verticalAlignment = Alignment.CenterVertically,
                            horizontalArrangement = Arrangement.SpaceBetween
                        ) {
                            Text("刷新标的价格", fontSize = 14.sp, color = TextPrimary)
                            Switch(
                                checked = refreshPrices, 
                                onCheckedChange = { refreshPrices = it },
                                colors = SwitchDefaults.colors(checkedTrackColor = IndigoPrimary)
                            )
                        }
                    }

                    Spacer(modifier = Modifier.height(12.dp))
                    HorizontalDivider(color = SlateBorder, thickness = 1.dp)
                    Spacer(modifier = Modifier.height(12.dp))

                    // Section 3.5: Remote Crawler
                    Text("远程数据获取设置 (服务器端)", fontSize = 13.sp, fontWeight = FontWeight.Bold, color = IndigoPrimary)
                    Spacer(modifier = Modifier.height(6.dp))
                    OutlinedTextField(
                        value = remoteCrawlerInterval,
                        onValueChange = { remoteCrawlerInterval = it },
                        modifier = Modifier.fillMaxWidth(),
                        label = { Text("远程数据获取频率 (分钟)") },
                        singleLine = true,
                        shape = RoundedCornerShape(10.dp),
                        colors = OutlinedTextFieldDefaults.colors(
                            focusedBorderColor = IndigoPrimary,
                            focusedLabelColor = IndigoPrimary,
                            focusedTextColor = TextPrimary,
                            unfocusedTextColor = TextPrimary
                        )
                    )
                    Spacer(modifier = Modifier.height(6.dp))
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.SpaceBetween
                    ) {
                        Text("开启远程标的刷新", fontSize = 14.sp, color = TextPrimary)
                        Switch(
                            checked = remoteTargetRefresh,
                            onCheckedChange = { remoteTargetRefresh = it },
                            colors = SwitchDefaults.colors(checkedTrackColor = IndigoPrimary)
                        )
                    }
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.SpaceBetween
                    ) {
                        Text("开启远程周末新闻", fontSize = 14.sp, color = TextPrimary)
                        Switch(
                            checked = remoteNewsRefresh,
                            onCheckedChange = { remoteNewsRefresh = it },
                            colors = SwitchDefaults.colors(checkedTrackColor = IndigoPrimary)
                        )
                    }

                    Spacer(modifier = Modifier.height(12.dp))
                    HorizontalDivider(color = SlateBorder, thickness = 1.dp)
                    Spacer(modifier = Modifier.height(12.dp))

                    // Section 4: Committee & Notifications
                    Text("分析与通知设置", fontSize = 13.sp, fontWeight = FontWeight.Bold, color = IndigoPrimary)
                    Spacer(modifier = Modifier.height(6.dp))
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.SpaceBetween
                    ) {
                        Text("启用 AI 委员会分析", fontSize = 14.sp, color = TextPrimary)
                        Switch(
                            checked = committeeAnalysisEnabled, 
                            onCheckedChange = { committeeAnalysisEnabled = it },
                            colors = SwitchDefaults.colors(checkedTrackColor = IndigoPrimary)
                        )
                    }
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.SpaceBetween
                    ) {
                        Text("启用分析完成系统通知", fontSize = 14.sp, color = TextPrimary)
                        Switch(
                            checked = notificationsEnabled,
                            colors = SwitchDefaults.colors(checkedTrackColor = IndigoPrimary),
                            onCheckedChange = { checked ->
                                if (checked) {
                                    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                                        val hasPermission = ContextCompat.checkSelfPermission(
                                            context,
                                            Manifest.permission.POST_NOTIFICATIONS
                                        ) == PackageManager.PERMISSION_GRANTED
                                        if (hasPermission) {
                                            notificationsEnabled = true
                                        } else {
                                            permissionLauncher.launch(Manifest.permission.POST_NOTIFICATIONS)
                                        }
                                    } else {
                                        notificationsEnabled = true
                                    }
                                } else {
                                    notificationsEnabled = false
                                }
                            }
                        )
                    }
                    Spacer(modifier = Modifier.height(8.dp))
                    OutlinedTextField(
                        value = retentionLimit,
                        onValueChange = { retentionLimit = it },
                        modifier = Modifier.fillMaxWidth(),
                        label = { Text("价格缓存保留上限 (月)") },
                        singleLine = true,
                        shape = RoundedCornerShape(10.dp),
                        colors = OutlinedTextFieldDefaults.colors(
                            focusedBorderColor = IndigoPrimary,
                            focusedLabelColor = IndigoPrimary,
                            focusedTextColor = TextPrimary,
                            unfocusedTextColor = TextPrimary
                        )
                    )

                    Spacer(modifier = Modifier.height(12.dp))
                    HorizontalDivider(color = SlateBorder, thickness = 1.dp)
                    Spacer(modifier = Modifier.height(16.dp))

                    // Section 5: Import Configs
                    Text("导入配置文件", fontSize = 13.sp, fontWeight = FontWeight.Bold, color = IndigoPrimary)
                    Spacer(modifier = Modifier.height(6.dp))
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.spacedBy(10.dp)
                    ) {
                        Button(
                            onClick = {
                                monitorConfigPicker.launch("application/json")
                            },
                            colors = ButtonDefaults.buttonColors(containerColor = IndigoPrimary),
                            shape = RoundedCornerShape(10.dp),
                            contentPadding = PaddingValues(horizontal = 4.dp, vertical = 2.dp),
                            modifier = Modifier
                                .weight(1f)
                                .height(40.dp)
                        ) {
                            Text("导入仓位配置", color = Color.White, fontSize = 11.sp, fontWeight = FontWeight.Bold)
                        }

                        Button(
                            onClick = {
                                exitParamsPicker.launch("application/json")
                            },
                            colors = ButtonDefaults.buttonColors(containerColor = IndigoPrimary),
                            shape = RoundedCornerShape(10.dp),
                            contentPadding = PaddingValues(horizontal = 4.dp, vertical = 2.dp),
                            modifier = Modifier
                                .weight(1f)
                                .height(40.dp)
                        ) {
                            Text("导入每周止盈配置", color = Color.White, fontSize = 11.sp, fontWeight = FontWeight.Bold)
                        }
                    }

                    Spacer(modifier = Modifier.height(14.dp))
                    HorizontalDivider(color = SlateBorder, thickness = 1.dp)
                    Spacer(modifier = Modifier.height(14.dp))

                    // Section 5b: Upload & Sync Data
                    Text("数据同步与备份", fontSize = 13.sp, fontWeight = FontWeight.Bold, color = IndigoPrimary)
                    Spacer(modifier = Modifier.height(6.dp))
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.spacedBy(10.dp)
                    ) {
                        Button(
                            onClick = {
                                val configContent = readLocalFile(context, "market_monitor_config.json")
                                val exitContent = readLocalFile(context, "weekly_exit_param_optimization.json")
                                if (configContent == null && exitContent == null) {
                                    Toast.makeText(context, "没有本地数据可上传，请先导入配置", Toast.LENGTH_LONG).show()
                                    return@Button
                                }
                                
                                var completedCount = 0
                                val totalToUpload = (if (configContent != null) 1 else 0) + (if (exitContent != null) 1 else 0)
                                
                                fun checkComplete() {
                                    completedCount++
                                    if (completedCount == totalToUpload) {
                                        Toast.makeText(context, "本地持仓/自选/参数数据上传成功！", Toast.LENGTH_LONG).show()
                                    }
                                }

                                configContent?.let {
                                    NetworkClient.uploadMonitorConfig(it) { res ->
                                        res.fold(
                                            onSuccess = { checkComplete() },
                                            onFailure = { err ->
                                                Toast.makeText(context, "上传仓位配置失败: ${err.message}", Toast.LENGTH_LONG).show()
                                            }
                                        )
                                    }
                                }
                                
                                exitContent?.let {
                                    NetworkClient.uploadExitParams(it) { res ->
                                        res.fold(
                                            onSuccess = { checkComplete() },
                                            onFailure = { err ->
                                                Toast.makeText(context, "上传参数配置失败: ${err.message}", Toast.LENGTH_LONG).show()
                                            }
                                        )
                                    }
                                }
                            },
                            colors = ButtonDefaults.buttonColors(containerColor = Color(0xFF10B981)),
                            shape = RoundedCornerShape(10.dp),
                            contentPadding = PaddingValues(horizontal = 4.dp, vertical = 2.dp),
                            modifier = Modifier
                                .weight(1f)
                                .height(40.dp)
                        ) {
                            Text("上传本地配置", color = Color.White, fontSize = 11.sp, fontWeight = FontWeight.Bold)
                        }

                        if (showSyncConfirmDialog) {
                            AlertDialog(
                                onDismissRequest = { showSyncConfirmDialog = false },
                                title = { Text("确认同步", fontWeight = FontWeight.Bold) },
                                text = { Text("您确定要从云端服务器同步配置吗？这将会覆盖本地现有的持仓与自选列表！") },
                                confirmButton = {
                                    TextButton(
                                        onClick = {
                                            showSyncConfirmDialog = false
                                            var syncCount = 0
                                            fun onSyncComplete() {
                                                syncCount++
                                                if (syncCount == 2) {
                                                    Toast.makeText(context, "配置同步成功，已覆盖本地数据！", Toast.LENGTH_LONG).show()
                                                    onRefresh()
                                                }
                                            }
                                            NetworkClient.downloadMonitorConfig { res ->
                                                res.fold(
                                                    onSuccess = { content ->
                                                        saveLocalFile(context, "market_monitor_config.json", content)
                                                        onImportConfig(content)
                                                        onSyncComplete()
                                                    },
                                                    onFailure = { err ->
                                                        Toast.makeText(context, "同步仓位配置失败: ${err.message}", Toast.LENGTH_LONG).show()
                                                        onSyncComplete()
                                                    }
                                                )
                                            }
                                            NetworkClient.downloadExitParams { res ->
                                                res.fold(
                                                    onSuccess = { content ->
                                                        saveLocalFile(context, "weekly_exit_param_optimization.json", content)
                                                        onSyncComplete()
                                                    },
                                                    onFailure = { err ->
                                                        Toast.makeText(context, "同步参数配置失败: ${err.message}", Toast.LENGTH_LONG).show()
                                                        onSyncComplete()
                                                    }
                                                )
                                            }
                                        }
                                    ) {
                                        Text("确认", color = Color(0xFFD97706))
                                    }
                                },
                                dismissButton = {
                                    TextButton(onClick = { showSyncConfirmDialog = false }) {
                                        Text("取消", color = TextSecondary)
                                    }
                                }
                            )
                        }

                        Button(
                            onClick = {
                                showSyncConfirmDialog = true
                            },
                            colors = ButtonDefaults.buttonColors(containerColor = Color(0xFFF59E0B)),
                            shape = RoundedCornerShape(10.dp),
                            contentPadding = PaddingValues(horizontal = 4.dp, vertical = 2.dp),
                            modifier = Modifier
                                .weight(1f)
                                .height(40.dp)
                        ) {
                            Text("同步云端配置", color = Color.White, fontSize = 11.sp, fontWeight = FontWeight.Bold)
                        }
                    }

                    Spacer(modifier = Modifier.height(14.dp))
                    HorizontalDivider(color = SlateBorder, thickness = 1.dp)
                    Spacer(modifier = Modifier.height(14.dp))

                    // Section 6: Cache Clear
                    Button(
                        onClick = {
                            clearingCache = true
                            val limitMonths = retentionLimit.toIntOrNull() ?: 2
                            NetworkClient.clearCache(limitMonths) { res ->
                                clearingCache = false
                                res.fold(
                                    onSuccess = {
                                        Toast.makeText(context, "服务器缓存清理成功", Toast.LENGTH_SHORT).show()
                                    },
                                    onFailure = { err ->
                                        Toast.makeText(context, "清理缓存失败: ${err.message}", Toast.LENGTH_SHORT).show()
                                    }
                                )
                            }
                        },
                        enabled = !clearingCache,
                        colors = ButtonDefaults.buttonColors(containerColor = Color(0xFFEF4444)), // Red delete
                        shape = RoundedCornerShape(10.dp),
                        modifier = Modifier.fillMaxWidth()
                    ) {
                        Text(if (clearingCache) "正在清理缓存..." else "清理服务器缓存", color = Color.White, fontWeight = FontWeight.Bold)
                    }
                }

                Spacer(modifier = Modifier.height(16.dp))

                // Action Footer
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.End,
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    TextButton(
                        onClick = onClose,
                        colors = ButtonDefaults.textButtonColors(contentColor = TextSecondary)
                    ) { 
                        Text("取消", fontWeight = FontWeight.SemiBold) 
                    }
                    Spacer(modifier = Modifier.width(12.dp))
                    Button(
                        colors = ButtonDefaults.buttonColors(containerColor = IndigoPrimary),
                        shape = RoundedCornerShape(10.dp),
                        onClick = {
                            val intervalVal = interval.toIntOrNull() ?: 30
                            val limitMonths = retentionLimit.toIntOrNull() ?: 2
                            val remoteFreq = remoteCrawlerInterval.toIntOrNull() ?: 60
                            
                            prefs.edit()
                                .putString("server_url", url)
                                .putString("llm_api_key", apiKey)
                                .putString("llm_base_url", llmBaseUrl)
                                .putString("llm_model", model)
                                .putBoolean("auto_refresh_enabled", autoRefresh)
                                .putInt("auto_refresh_interval", intervalVal)
                                .putBoolean("auto_refresh_news_enabled", refreshNews)
                                .putBoolean("auto_refresh_selection_enabled", refreshSelection)
                                .putBoolean("auto_refresh_prices_enabled", refreshPrices)
                                .putBoolean("notifications_enabled", notificationsEnabled)
                                .putBoolean("committee_analysis_enabled", committeeAnalysisEnabled)
                                .putInt("cache_retention_limit_months", limitMonths)
                                .putInt("remote_crawler_interval_minutes", remoteFreq)
                                .putBoolean("remote_target_refresh_enabled", remoteTargetRefresh)
                                .putBoolean("remote_news_refresh_enabled", remoteNewsRefresh)
                                .apply()

                            NetworkClient.updateCrawlerSettings(
                                CrawlerSettings(
                                    frequency_minutes = remoteFreq,
                                    target_refresh_enabled = remoteTargetRefresh,
                                    news_refresh_enabled = remoteNewsRefresh
                                )
                            ) { res ->
                                res.fold(
                                    onSuccess = {
                                        Toast.makeText(context, "设置已保存并同步云端！", Toast.LENGTH_SHORT).show()
                                    },
                                    onFailure = { err ->
                                        Toast.makeText(context, "本地已保存，但同步云端失败: ${err.message}", Toast.LENGTH_LONG).show()
                                    }
                                )
                            }

                            onSave(url)
                            onClose()
                        }
                    ) {
                        Text("保存设置", color = Color.White, fontWeight = FontWeight.Bold)
                    }
                }
            }
        }
    }
}

@Composable
fun NewsDialog(
    news: WeekendNewsResponse?,
    snapshot: SnapshotResponse?,
    onClose: () -> Unit,
    onViewDetails: (NewsLeader) -> Unit
) {
    var activeLeaderBubble by remember { mutableStateOf<NewsLeader?>(null) }

    Dialog(
        onDismissRequest = onClose,
        properties = DialogProperties(usePlatformDefaultWidth = false)
    ) {
        Surface(
            modifier = Modifier
                .fillMaxSize()
                .padding(16.dp),
            shape = RoundedCornerShape(12.dp),
            color = Color(0xFFF3F6FB)
        ) {
            Column(modifier = Modifier.fillMaxSize()) {
                // Header
                Row(
                    modifier = Modifier
                        .fillMaxWidth()
                        .background(Color.White)
                        .padding(horizontal = 16.dp, vertical = 12.dp),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Text("周末新闻与机会", fontSize = 16.sp, fontWeight = FontWeight.Bold, color = Color(0xFF18202B))
                    IconButton(onClick = onClose) {
                        Icon(Icons.Default.Close, contentDescription = "关闭", tint = Color(0xFF667085))
                    }
                }

                // Body
                if (news == null || news.cards.isNullOrEmpty()) {
                    Box(modifier = Modifier.weight(1f).fillMaxWidth(), contentAlignment = Alignment.Center) {
                        Text("暂无周末机会新闻", color = Color(0xFF667085), fontSize = 14.sp)
                    }
                } else {
                    LazyColumn(
                        modifier = Modifier
                            .weight(1f)
                            .padding(16.dp),
                        verticalArrangement = Arrangement.spacedBy(12.dp)
                    ) {
                        item {
                            Text(
                                text = "数据源: ${news.source ?: "本地抓取"}",
                                fontSize = 12.sp,
                                color = Color(0xFF667085),
                                modifier = Modifier.padding(bottom = 4.dp)
                            )
                        }
                        items(news.cards) { card ->
                            Card(
                                modifier = Modifier.fillMaxWidth(),
                                colors = CardDefaults.cardColors(containerColor = Color.White),
                                shape = RoundedCornerShape(8.dp)
                            ) {
                                Column(modifier = Modifier.padding(12.dp)) {
                                    Row(
                                        modifier = Modifier.fillMaxWidth(),
                                        horizontalArrangement = Arrangement.SpaceBetween
                                    ) {
                                        Text(
                                            text = card.title,
                                            fontSize = 14.sp,
                                            fontWeight = FontWeight.Bold,
                                            color = Color(0xFF18202B)
                                        )
                                        Box(
                                            modifier = Modifier
                                                .clip(RoundedCornerShape(4.dp))
                                                .background(Color(0xFFFFE5E0))
                                                .padding(horizontal = 6.dp, vertical = 2.dp)
                                        ) {
                                            Text(
                                                text = "热度: ${card.heat_score.toInt()}",
                                                fontSize = 10.sp,
                                                fontWeight = FontWeight.Bold,
                                                color = Color(0xFFC01048)
                                            )
                                        }
                                    }
                                    Spacer(modifier = Modifier.height(4.dp))
                                    Text(
                                        text = "板块: ${card.sector}",
                                        fontSize = 11.sp,
                                        fontWeight = FontWeight.SemiBold,
                                        color = Color(0xFF2F80ED)
                                    )
                                    Spacer(modifier = Modifier.height(8.dp))
                                    Text(
                                        text = card.logic,
                                        fontSize = 12.sp,
                                        color = Color(0xFF18202B)
                                    )

                                    if (card.risk_note.isNotEmpty()) {
                                        Spacer(modifier = Modifier.height(8.dp))
                                        Text(
                                            text = "风险提示: ${card.risk_note}",
                                            fontSize = 11.sp,
                                            color = Color(0xFFB42318),
                                            fontWeight = FontWeight.SemiBold
                                        )
                                    }

                                    card.leaders?.let { leaders ->
                                        if (leaders.isNotEmpty()) {
                                            Spacer(modifier = Modifier.height(8.dp))
                                            Text("标的建议: ", fontSize = 11.sp, fontWeight = FontWeight.Bold, color = Color(0xFF667085))
                                            Spacer(modifier = Modifier.height(4.dp))
                                            Column {
                                                leaders.forEach { leader ->
                                                    Row(
                                                        verticalAlignment = Alignment.CenterVertically,
                                                        modifier = Modifier.padding(vertical = 2.dp)
                                                    ) {
                                                        Text(
                                                            text = "• ",
                                                            fontSize = 11.sp,
                                                            color = Color(0xFF18202B)
                                                        )
                                                        Text(
                                                            text = leader.name ?: leader.symbol ?: "",
                                                            fontSize = 11.sp,
                                                            fontWeight = FontWeight.Bold,
                                                            color = Color(0xFF4F46E5), // IndigoPrimary
                                                            modifier = Modifier
                                                                .clickable {
                                                                    activeLeaderBubble = leader
                                                                }
                                                                .padding(horizontal = 2.dp)
                                                        )
                                                        if (!leader.reason.isNullOrBlank()) {
                                                            Text(
                                                                text = " - ${leader.reason}",
                                                                fontSize = 11.sp,
                                                                color = Color(0xFF667085)
                                                            )
                                                        }
                                                    }
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    if (activeLeaderBubble != null) {
        val bubble = activeLeaderBubble!!
        val bubbleSymbol = bubble.symbol ?: bubble.code ?: ""
        val match = snapshot?.rows?.find { it.symbol.uppercase() == bubbleSymbol.uppercase() || it.name == bubble.name }

        AlertDialog(
            onDismissRequest = { activeLeaderBubble = null },
            title = {
                Text(
                    text = "${bubble.name ?: "未知标的"} (${bubbleSymbol})",
                    fontWeight = FontWeight.Bold,
                    fontSize = 16.sp,
                    color = Color(0xFF0F172A)
                )
            },
            text = {
                Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    if (match != null) {
                        val isRise = match.price.change_pct >= 0
                        val changeSign = if (isRise) "+" else ""
                        val priceColor = if (isRise) Color(0xFFE53E3E) else Color(0xFF38A169)

                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Text("当前价格: ", fontSize = 12.sp, color = Color(0xFF64748B))
                            Text(
                                "¥${String.format("%.2f", match.price.current)}",
                                fontSize = 13.sp,
                                fontWeight = FontWeight.Bold,
                                color = Color(0xFF0F172A)
                            )
                            Spacer(modifier = Modifier.width(6.dp))
                            Text(
                                "${changeSign}${String.format("%.2f", match.price.change_pct)}%",
                                fontSize = 11.sp,
                                fontWeight = FontWeight.Bold,
                                color = priceColor
                            )
                        }
                    } else {
                        Text(
                            text = "当前价格: 暂未在监视列表中（点击查看详情可自动搜索并运行本地分析）",
                            fontSize = 12.sp,
                            color = Color(0xFF64748B)
                        )
                    }

                    if (!bubble.reason.isNullOrBlank()) {
                        Text("研判原因: ", fontSize = 12.sp, color = Color(0xFF64748B))
                        Text(
                            text = bubble.reason,
                            fontSize = 12.sp,
                            color = Color(0xFF1E293B)
                        )
                    }
                }
            },
            confirmButton = {
                TextButton(
                    onClick = {
                        onViewDetails(bubble)
                        activeLeaderBubble = null
                    }
                ) {
                    Text("查看分析详情", color = Color(0xFF4F46E5), fontWeight = FontWeight.Bold)
                }
            },
            dismissButton = {
                TextButton(onClick = { activeLeaderBubble = null }) {
                    Text("关闭", color = Color.Gray)
                }
            },
            shape = RoundedCornerShape(12.dp),
            containerColor = Color.White
        )
    }
}

// --- Stock Selection Dialog ---
@Composable
fun SelectionStockDialog(stock: SelectionStock, onClose: () -> Unit, onWatchlistAdded: (String) -> Unit) {
    var addingToWatchlist by remember { mutableStateOf(false) }

    Dialog(onDismissRequest = onClose) {
        Card(
            modifier = Modifier
                .fillMaxWidth()
                .heightIn(max = 520.dp),
            shape = RoundedCornerShape(12.dp),
            colors = CardDefaults.cardColors(containerColor = Color.White)
        ) {
            Column(modifier = Modifier.padding(16.dp)) {
                // Header
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Column {
                        Text(
                            text = stock.name ?: stock.symbol,
                            fontSize = 16.sp,
                            fontWeight = FontWeight.Bold,
                            color = Color(0xFF18202B)
                        )
                        Text(
                            text = stock.symbol,
                            fontSize = 11.sp,
                            color = Color(0xFF667085)
                        )
                    }
                    Box(
                        modifier = Modifier
                            .clip(RoundedCornerShape(4.dp))
                            .background(Color(0xFFFFE5E0))
                            .padding(horizontal = 8.dp, vertical = 4.dp)
                    ) {
                        Text(
                            text = "综合评分: ${String.format("%.0f", stock.score ?: 0.0)}",
                            fontSize = 11.sp,
                            fontWeight = FontWeight.Bold,
                            color = Color(0xFFC01048)
                        )
                    }
                }

                Spacer(modifier = Modifier.height(12.dp))

                // Scrollable Reason Details
                Column(
                    modifier = Modifier
                        .weight(1f)
                        .verticalScroll(rememberScrollState())
                ) {
                    Text(
                        text = "板块: ${stock.sector ?: "-"}",
                        fontSize = 12.sp,
                        fontWeight = FontWeight.SemiBold,
                        color = Color(0xFF2F80ED)
                    )
                    Spacer(modifier = Modifier.height(8.dp))

                    Text("入选逻辑:", fontSize = 12.sp, fontWeight = FontWeight.Bold, color = Color(0xFF18202B))
                    Text(
                        text = stock.reasons?.joinToString("；") ?: "暂无详细逻辑说明",
                        fontSize = 12.sp,
                        color = Color(0xFF18202B)
                    )

                    Spacer(modifier = Modifier.height(8.dp))

                    val trendInterp = stock.trend?.get("interpretation") as? String ?: stock.tape?.get("interpretation") as? String ?: ""
                    if (trendInterp.isNotEmpty()) {
                        Text("走势解读:", fontSize = 12.sp, fontWeight = FontWeight.Bold, color = Color(0xFF18202B))
                        Text(
                            text = trendInterp,
                            fontSize = 12.sp,
                            color = Color(0xFF2F80ED)
                        )
                        Spacer(modifier = Modifier.height(8.dp))
                    }

                    stock.entry_plan?.let { plan ->
                        Text("交易计划:", fontSize = 12.sp, fontWeight = FontWeight.Bold, color = Color(0xFF18202B))
                        Text(
                            text = "买点动作: ${plan.action ?: "-"}\n触发价: ¥${plan.trigger_price ?: 0.0}\n止损价: ¥${plan.stop_loss_price ?: 0.0}",
                            fontSize = 12.sp,
                            color = Color(0xFF067647),
                            fontWeight = FontWeight.Bold
                        )
                        plan.note?.let {
                            Spacer(modifier = Modifier.height(4.dp))
                            Text(
                                text = "交易备注: $it",
                                fontSize = 11.sp,
                                color = Color(0xFF667085)
                            )
                        }
                        Spacer(modifier = Modifier.height(8.dp))
                    }

                    Text(
                        text = "资金流向 / 基本面评分: ${String.format("%.0f", stock.money_flow_score ?: 0.0)} / ${String.format("%.0f", stock.fundamental_score ?: 0.0)}",
                        fontSize = 11.sp,
                        color = Color(0xFF667085)
                    )
                }

                Spacer(modifier = Modifier.height(16.dp))

                // Actions
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.End
                ) {
                    TextButton(onClick = onClose) { Text("取消") }
                    Spacer(modifier = Modifier.width(8.dp))

                    Button(
                        onClick = {
                            addingToWatchlist = true
                            NetworkClient.addToWatchlist(stock.symbol, stock.name ?: stock.symbol, stock.sector ?: "") { res ->
                                addingToWatchlist = false
                                val msg = res.getOrDefault("已加入关注列表")
                                onWatchlistAdded(msg)
                                onClose()
                            }
                        },
                        enabled = !addingToWatchlist
                    ) {
                        Text(if (addingToWatchlist) "添加中..." else "加入关注列表")
                    }
                }
            }
        }
    }
}

// --- Committee Analysis Dialog ---
@Composable
fun CommitteeAnalysisDialog(
    symbol: String,
    row: HoldingRow,
    inWatchlist: Boolean,
    onClose: () -> Unit,
    onWatchlistAdded: (String) -> Unit,
    onTradeExecuted: (String) -> Unit,
    totalAssets: Double,
    cash: Double,
    t2PendingCash: Double = 0.0,
    holdingsJson: String
) {
    val context = LocalContext.current
    val prefs = remember { context.getSharedPreferences("open_invest_prefs", Context.MODE_PRIVATE) }
    val committeeAnalysisEnabled = remember { prefs.getBoolean("committee_analysis_enabled", true) }

    var statusLog by remember { mutableStateOf("正在获取最新价格并运行委员会分析...") }
    var taskStatus by remember { mutableStateOf<CommitteeStatusResponse?>(null) }
    var streamCall by remember { mutableStateOf<Call?>(null) }
    var isResolving by remember { mutableStateOf(row._is_resolving) }

    var resolvedRow by remember { mutableStateOf<HoldingRow?>(if (row._is_resolving) null else row) }

    var addingWatchlist by remember { mutableStateOf(false) }

    // Execute Trade Dialogue State
    var showTradeDialog by remember { mutableStateOf(false) }
    var showUpdateHoldingDialog by remember { mutableStateOf(false) }
    var tradeVerdict by remember { mutableStateOf("") }

    val startCommitteeTask: (String) -> Unit = { targetSymbol ->
        statusLog = "正在运行本地投资委员会辩论 (可能需要 1~2 分钟)..."
        taskStatus = CommitteeStatusResponse(
            task_id = "local_task_${System.currentTimeMillis()}",
            status = "running",
            phase = "local_debate",
            started_at = "",
            ended_at = null,
            error = null,
            progress = 0.3,
            output = null,
            verdict = null,
            symbols = listOf(targetSymbol),
            debate_summary = null,
            final_verdicts = null,
            events = listOf(mapOf("message" to "启动本地 4 角色投资委员会辩论...")),
            result = null
        )

        val targetRow = resolvedRow ?: row
        
        LocalCommitteeRunner.runCommitteeLocally(
            context = context,
            symbol = targetSymbol,
            name = targetRow.name,
            market = targetRow.market,
            sector = targetRow.sector ?: "",
            industry = targetRow.industry ?: "",
            positionPct = targetRow.position_pct,
            targetPositionPct = targetRow.target_position_pct,
            cost = targetRow.cost,
            currentPrice = if (targetRow.price.current > 0.0) targetRow.price.current else null,
            totalAssets = totalAssets,
            cash = cash,
            holdingsJson = holdingsJson,
            newsBrief = "",
            minLotSize = targetRow.min_lot_size,
            tPlus1 = targetRow.market == "a",
            availableCash = cash,
            t2PendingCash = t2PendingCash,
            optimizerReviewEnabled = true,
            maxDebateRounds = 4,
            changePct = targetRow.price.change_pct
        ) { runResult ->
            runResult.fold(
                onSuccess = { done ->
                    taskStatus = done
                    val finalVerdict = (done.result?.get("by_asset") as? Map<*, *>)
                        ?.get(targetSymbol)?.let { it as? Map<*, *> }
                        ?.get("verdict") as? String ?: "HOLD"
                    showNotification(
                        context,
                        "本地委员会分析完成: $targetSymbol",
                        "决策建议: $finalVerdict"
                    )
                },
                onFailure = { err ->
                    statusLog = "本地分析失败: ${err.message}"
                    taskStatus = CommitteeStatusResponse(
                        task_id = "local_task",
                        status = "error",
                        phase = "error",
                        started_at = "",
                        ended_at = null,
                        error = err.message,
                        progress = 0.0,
                        output = null,
                        verdict = null,
                        symbols = listOf(targetSymbol),
                        debate_summary = null,
                        final_verdicts = null,
                        events = listOf(mapOf("message" to "本地委员会分析失败: ${err.message}")),
                        result = null
                    )
                }
            )
        }
    }

    val startDataOnlyTask: (String) -> Unit = { targetSymbol ->
        statusLog = "正在获取最新行情并计算指标..."
        NetworkClient.fetchRegime(targetSymbol) { res ->
            res.fold(
                onSuccess = { regimeMap ->
                    val brief = regimeMap["brief"] as? String ?: "获取数据成功，无详细指标报告"
                    val inputs = regimeMap["inputs"] as? Map<*, *>
                    val currentPrice = (inputs?.get("current_price") as? Double) ?: 0.0
                    
                    // Update resolvedRow price
                    val curRow = resolvedRow ?: row
                    resolvedRow = curRow.copy(
                        price = curRow.price.copy(current = currentPrice)
                    )
                    
                    // Build dummy completed task status
                    taskStatus = CommitteeStatusResponse(
                        task_id = "data_only",
                        status = "done",
                        phase = "done",
                        started_at = "",
                        ended_at = null,
                        error = null,
                        progress = 1.0,
                        output = null,
                        verdict = null,
                        symbols = listOf(targetSymbol),
                        debate_summary = null,
                        final_verdicts = null,
                        events = listOf(mapOf("message" to brief)),
                        result = null
                    )
                },
                onFailure = { err ->
                    statusLog = "获取标的数据失败: ${err.message}"
                    taskStatus = CommitteeStatusResponse(
                        task_id = "data_only",
                        status = "error",
                        phase = "error",
                        started_at = "",
                        ended_at = null,
                        error = err.message,
                        progress = 0.0,
                        output = null,
                        verdict = null,
                        symbols = listOf(targetSymbol),
                        debate_summary = null,
                        final_verdicts = null,
                        events = listOf(mapOf("message" to "获取行情及纯数据计算失败: ${err.message}")),
                        result = null
                    )
                }
            )
        }
    }

    LaunchedEffect(symbol) {
        if (isResolving) {
            statusLog = "正在联网检索标的 '${row.name}' 的行情数据..."
            // Perform symbol search query
            NetworkClient.setBaseUrl(NetworkClient.getBaseUrl())
            val request = Request.Builder()
                .url("${NetworkClient.getBaseUrl()}/api/symbols/search?q=${row.name}&limit=1")
                .build()

            OkHttpClient().newCall(request).enqueue(object : Callback {
                override fun onFailure(call: Call, e: IOException) {
                    android.os.Handler(android.os.Looper.getMainLooper()).post {
                        statusLog = "检索失败: ${e.message}"
                    }
                }

                override fun onResponse(call: Call, response: Response) {
                    val body = response.body?.string() ?: ""
                    response.close()
                    try {
                        val gson = Gson()
                        val map = gson.fromJson<Map<String, Any>>(body, object : TypeToken<Map<String, Any>>() {}.type)
                        val results = map["results"] as? List<*>
                        if (!results.isNullOrEmpty()) {
                            val first = gson.fromJson(gson.toJson(results[0]), Map::class.java)
                            val resolvedCode = first["symbol"] as String
                            val resolvedName = first["shortname"] as? String ?: resolvedCode

                            android.os.Handler(android.os.Looper.getMainLooper()).post {
                                val dummy = HoldingRow(
                                    symbol = resolvedCode,
                                    name = resolvedName,
                                    market = if (resolvedCode.length == 5) "hk" else "a",
                                    state = "watch",
                                    price = HoldingRowPrice(0.0, 0.0, 0.0),
                                    operation = Operation(verdict = "HOLD")
                                )
                                isResolving = false
                                resolvedRow = dummy
                                if (committeeAnalysisEnabled) {
                                    statusLog = "已找到标的: $resolvedName ($resolvedCode)，正在运行委员会分析..."
                                    startCommitteeTask(resolvedCode)
                                } else {
                                    statusLog = "已找到标的: $resolvedName ($resolvedCode)，正在更新行情数据..."
                                    startDataOnlyTask(resolvedCode)
                                }
                            }
                        } else {
                            android.os.Handler(android.os.Looper.getMainLooper()).post {
                                statusLog = "未找到标的: ${row.name}"
                            }
                        }
                    } catch (e: Exception) {
                        android.os.Handler(android.os.Looper.getMainLooper()).post {
                            statusLog = "检索出错: ${e.message}"
                        }
                    }
                }
            })
        } else {
            if (committeeAnalysisEnabled) {
                startCommitteeTask(symbol)
            } else {
                startDataOnlyTask(symbol)
            }
        }
    }

    DisposableEffect(Unit) {
        onDispose {
            streamCall?.cancel()
        }
    }

    val isDebatingDone = taskStatus?.status == "done"
    val isDebatingError = taskStatus?.status == "error"

    // Construct debateLog from events list or output when done
    val debateLog = remember(taskStatus, statusLog) {
        val status = taskStatus
        if (status != null) {
            if (status.status == "done" || status.phase == "done") {
                status.output ?: status.debate_summary ?: "分析已完成，但未返回详细报告。"
            } else {
                val events = status.events
                if (!events.isNullOrEmpty()) {
                    events.mapNotNull { it["message"] as? String }.joinToString("\n")
                } else {
                    statusLog
                }
            }
        } else {
            statusLog
        }
    }

    // Resolve detailed verdicts for target symbol from status result
    val symbolDetails = remember(taskStatus, resolvedRow) {
        val resRow = resolvedRow ?: row
        val byAsset = taskStatus?.result?.get("by_asset") as? Map<*, *>
        byAsset?.get(resRow.symbol) as? Map<*, *>
    }

    Dialog(
        onDismissRequest = onClose,
        properties = DialogProperties(usePlatformDefaultWidth = false)
    ) {
        Surface(
            modifier = Modifier
                .fillMaxSize()
                .padding(16.dp),
            shape = RoundedCornerShape(12.dp),
            color = Color(0xFFF3F6FB)
        ) {
            Column(modifier = Modifier.fillMaxSize()) {
                // Header
                Row(
                    modifier = Modifier
                        .fillMaxWidth()
                        .background(Color.White)
                        .padding(horizontal = 16.dp, vertical = 12.dp),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Column {
                        val headerName = resolvedRow?.name ?: row.name
                        val headerSymbol = resolvedRow?.symbol ?: symbol
                        Text(
                            text = if (isResolving) "检索中: $headerName" else "$headerName ($headerSymbol)",
                            fontSize = 16.sp,
                            fontWeight = FontWeight.Bold,
                            color = Color(0xFF18202B)
                        )
                        Text(
                            text = if (isResolving) "联网查询中..." else if (isDebatingDone) "分析已完成" else "流式委员会分析中...",
                            fontSize = 11.sp,
                            color = if (isDebatingDone) Color(0xFF067647) else Color(0xFF2F80ED),
                            fontWeight = FontWeight.SemiBold
                        )
                    }

                    Row(verticalAlignment = Alignment.CenterVertically) {
                        // Watchlist Button
                        if (!isResolving && !inWatchlist) {
                            TextButton(
                                onClick = {
                                    addingWatchlist = true
                                    val r = resolvedRow ?: row
                                    NetworkClient.addToWatchlist(r.symbol, r.name, r.sector ?: "") { res ->
                                        addingWatchlist = false
                                        onWatchlistAdded(res.getOrDefault("已关注"))
                                    }
                                },
                                enabled = !addingWatchlist
                            ) {
                                Text(if (addingWatchlist) "添加中..." else "加入关注")
                            }
                        }

                        IconButton(onClick = onClose) {
                            Icon(Icons.Default.Close, contentDescription = "关闭", tint = Color(0xFF667085))
                        }
                    }
                }

                // Progress Bar
                if (!isResolving && !isDebatingDone && !isDebatingError) {
                    LinearProgressIndicator(
                        modifier = Modifier.fillMaxWidth(),
                        color = Color(0xFF2F80ED),
                        trackColor = Color(0xFFE6EBF2)
                    )
                }

                // Body content (Scrollable container to prevent overflow on smaller screens)
                Column(
                    modifier = Modifier
                        .weight(1f)
                        .padding(horizontal = 16.dp, vertical = 8.dp)
                        .verticalScroll(rememberScrollState())
                ) {
                    resolvedRow?.let { resRow ->
                        // 1. Stock Price & Action Panel
                        Card(
                            modifier = Modifier.fillMaxWidth(),
                            colors = CardDefaults.cardColors(containerColor = Color.White),
                            shape = RoundedCornerShape(12.dp)
                        ) {
                            Column(modifier = Modifier.padding(14.dp)) {
                                Row(
                                    modifier = Modifier.fillMaxWidth(),
                                    horizontalArrangement = Arrangement.SpaceBetween,
                                    verticalAlignment = Alignment.CenterVertically
                                ) {
                                    Column {
                                        Text(
                                            text = if (resRow.price.current > 0) "实时价: ¥${String.format("%.2f", resRow.price.current)}" else "实时价: -",
                                            fontSize = 18.sp,
                                            fontWeight = FontWeight.Bold,
                                            color = Color(0xFF18202B)
                                        )
                                        val changePct = resRow.price.change_pct
                                        val changeColor = if (changePct >= 0) Color(0xFF067647) else Color(0xFFB42318)
                                        val changeSign = if (changePct >= 0) "+" else ""
                                        Text(
                                            text = "$changeSign${String.format("%.2f", changePct)}%",
                                            fontSize = 12.sp,
                                            fontWeight = FontWeight.SemiBold,
                                            color = changeColor
                                        )
                                    }
                                    
                                    val finalVerdict = symbolDetails?.get("verdict") as? String ?: resRow.operation?.verdict ?: "HOLD"
                                    val verdictColor = if (finalVerdict.uppercase() in listOf("BUY", "ACCUMULATE")) Color(0xFF067647) else if (finalVerdict.uppercase() in listOf("SELL", "TRIM")) Color(0xFFB42318) else Color(0xFF18202B)
                                    val verdictBg = if (finalVerdict.uppercase() in listOf("BUY", "ACCUMULATE")) Color(0xFFE8F8EE) else if (finalVerdict.uppercase() in listOf("SELL", "TRIM")) Color(0xFFFEECEB) else Color(0xFFEEF3F8)
                                    
                                    Box(
                                        modifier = Modifier
                                            .clip(RoundedCornerShape(6.dp))
                                            .background(verdictBg)
                                            .padding(horizontal = 12.dp, vertical = 6.dp)
                                    ) {
                                        Text(
                                            text = "推荐: $finalVerdict",
                                            fontSize = 14.sp,
                                            fontWeight = FontWeight.Bold,
                                            color = verdictColor
                                        )
                                    }
                                }

                                Spacer(modifier = Modifier.height(12.dp))
                                HorizontalDivider(color = Color(0xFFEEF2F6))
                                Spacer(modifier = Modifier.height(12.dp))

                                // Quick Actions (Always Available!)
                                Row(
                                    modifier = Modifier.fillMaxWidth(),
                                    horizontalArrangement = Arrangement.spacedBy(8.dp)
                                ) {
                                    val quickVerdict = (symbolDetails?.get("verdict") as? String ?: resRow.operation?.verdict ?: "HOLD").uppercase()

                                    Button(
                                        onClick = {
                                            tradeVerdict = if (quickVerdict in listOf("BUY", "ACCUMULATE", "SELL", "TRIM")) quickVerdict else "BUY"
                                            showTradeDialog = true
                                        },
                                        modifier = Modifier.weight(1f),
                                        colors = ButtonDefaults.buttonColors(
                                            containerColor = if (quickVerdict in listOf("SELL", "TRIM")) Color(0xFFB42318) else Color(0xFF2F80ED)
                                        )
                                    ) {
                                        Text(
                                            text = if (quickVerdict in listOf("BUY", "ACCUMULATE", "SELL", "TRIM")) "直接记账 ($quickVerdict)" else "记账买入",
                                            fontSize = 12.sp,
                                            fontWeight = FontWeight.Bold
                                        )
                                    }

                                    OutlinedButton(
                                        onClick = { showUpdateHoldingDialog = true },
                                        modifier = Modifier.weight(1f),
                                        border = BorderStroke(1.dp, Color(0xFF9AA9BB))
                                    ) {
                                        Text("修正持仓", fontSize = 12.sp, color = Color(0xFF475467), fontWeight = FontWeight.Bold)
                                    }
                                }
                            }
                        }

                        Spacer(modifier = Modifier.height(12.dp))

                        // 2. Detailed Parameters & Holding Grid Card
                        Card(
                            modifier = Modifier.fillMaxWidth(),
                            colors = CardDefaults.cardColors(containerColor = Color.White),
                            shape = RoundedCornerShape(12.dp)
                        ) {
                            Column(modifier = Modifier.padding(14.dp)) {
                                Text(
                                    text = "交易参数与持仓计划",
                                    fontSize = 13.sp,
                                    fontWeight = FontWeight.Bold,
                                    color = Color(0xFF18202B)
                                )
                                Spacer(modifier = Modifier.height(8.dp))

                                // Grid values
                                Row(modifier = Modifier.fillMaxWidth()) {
                                    Column(modifier = Modifier.weight(1f)) {
                                        Text("当前持仓", fontSize = 10.sp, color = Color(0xFF667085))
                                        val holdingText = if (resRow.units > 0) {
                                            val lotSize = if (resRow.min_lot_size > 0) resRow.min_lot_size else 100
                                            "${resRow.units.toInt()} 股 (${String.format("%.1f", resRow.units / lotSize)} 手)"
                                        } else {
                                            "无持仓"
                                        }
                                        Text(holdingText, fontSize = 12.sp, fontWeight = FontWeight.SemiBold, color = Color(0xFF18202B))
                                    }
                                    Column(modifier = Modifier.weight(1f)) {
                                        Text("持仓均价 / 成本", fontSize = 10.sp, color = Color(0xFF667085))
                                        val costText = if (resRow.units > 0) "¥${String.format("%.2f", resRow.cost)}" else "-"
                                        Text(costText, fontSize = 12.sp, fontWeight = FontWeight.SemiBold, color = Color(0xFF18202B))
                                    }
                                }

                                Spacer(modifier = Modifier.height(8.dp))

                                Row(modifier = Modifier.fillMaxWidth()) {
                                    Column(modifier = Modifier.weight(1f)) {
                                        Text("建议分配资金", fontSize = 10.sp, color = Color(0xFF667085))
                                        val suggestedAlloc = symbolDetails?.get("suggested_alloc_cny") as? Double ?: resRow.operation?.suggested_alloc_cny ?: 0.0
                                        Text("¥${String.format("%.0f", suggestedAlloc)}", fontSize = 12.sp, fontWeight = FontWeight.SemiBold, color = Color(0xFF067647))
                                    }
                                    Column(modifier = Modifier.weight(1f)) {
                                        Text("建议交易手数", fontSize = 10.sp, color = Color(0xFF667085))
                                        val recLots = resRow.operation?.optimizer_lots ?: resRow.operation?.llm_review_lots
                                        Text(if (recLots != null && recLots > 0) "$recLots 手" else "-", fontSize = 12.sp, fontWeight = FontWeight.SemiBold, color = Color(0xFF067647))
                                    }
                                }

                                Spacer(modifier = Modifier.height(10.dp))
                                HorizontalDivider(color = Color(0xFFEEF2F6))
                                Spacer(modifier = Modifier.height(10.dp))

                                // Buy Criteria
                                Text("进场条件 (买点)", fontSize = 11.sp, fontWeight = FontWeight.Bold, color = Color(0xFF667085))
                                Spacer(modifier = Modifier.height(6.dp))
                                val buy = resRow.buy_criteria
                                Row(modifier = Modifier.fillMaxWidth()) {
                                    Column(modifier = Modifier.weight(1f)) {
                                        Text("回踩买入价", fontSize = 9.sp, color = Color(0xFF8896AB))
                                        val pullbackVal = buy?.pullback_price ?: 0.0
                                        Text(if (pullbackVal > 0) "¥${String.format("%.2f", pullbackVal)}" else "-", fontSize = 11.sp, fontWeight = FontWeight.SemiBold, color = Color(0xFF18202B))
                                    }
                                    Column(modifier = Modifier.weight(1f)) {
                                        Text("突破买入价", fontSize = 9.sp, color = Color(0xFF8896AB))
                                        val breakoutVal = buy?.breakout_price ?: 0.0
                                        Text(if (breakoutVal > 0) "¥${String.format("%.2f", breakoutVal)}" else "-", fontSize = 11.sp, fontWeight = FontWeight.SemiBold, color = Color(0xFF18202B))
                                    }
                                    Column(modifier = Modifier.weight(1f)) {
                                        Text("二次探底买入价", fontSize = 9.sp, color = Color(0xFF8896AB))
                                        val reentryVal = buy?.reentry_price ?: 0.0
                                        Text(if (reentryVal > 0) "¥${String.format("%.2f", reentryVal)}" else "-", fontSize = 11.sp, fontWeight = FontWeight.SemiBold, color = Color(0xFF18202B))
                                    }
                                }
                                
                                val rrVal = buy?.reward_risk_ratio ?: 0.0
                                if (rrVal > 0) {
                                    Spacer(modifier = Modifier.height(4.dp))
                                    Text("预计盈亏比: ${String.format("%.1f", rrVal)}", fontSize = 10.sp, color = Color(0xFF475467))
                                }

                                if (!buy?.reason.isNullOrBlank()) {
                                    Spacer(modifier = Modifier.height(4.dp))
                                    val translatedReason = when (buy.reason) {
                                        "conditional_quantile_volatility_scaled_levels" -> "基于历史分布(分位数)与波动率(ATR)自适应计算"
                                        "missing_current_price" -> "缺少当前最新价"
                                        "atr_regime_fallback" -> "基于波动率自适应估算(回退模型)"
                                        "regime_quantile_atr_cvar" -> "基于量化模型(分位数与条件在险价值CVaR)估算"
                                        else -> buy.reason
                                    }
                                    Text("进场理由: $translatedReason", fontSize = 10.sp, color = Color(0xFF475467))
                                }

                                Spacer(modifier = Modifier.height(10.dp))
                                HorizontalDivider(color = Color(0xFFEEF2F6))
                                Spacer(modifier = Modifier.height(10.dp))

                                // Exit points
                                Text("出场条件 (卖点)", fontSize = 11.sp, fontWeight = FontWeight.Bold, color = Color(0xFF667085))
                                Spacer(modifier = Modifier.height(6.dp))
                                val exit = resRow.exit_points
                                Row(modifier = Modifier.fillMaxWidth()) {
                                    Column(modifier = Modifier.weight(1f)) {
                                        Text("止损价", fontSize = 9.sp, color = Color(0xFF8896AB))
                                        val stopLossVal = exit?.stop_loss_price ?: 0.0
                                        Text(if (stopLossVal > 0) "¥${String.format("%.2f", stopLossVal)}" else "-", fontSize = 11.sp, fontWeight = FontWeight.SemiBold, color = Color(0xFFB42318))
                                    }
                                    Column(modifier = Modifier.weight(1f)) {
                                        Text("止盈价", fontSize = 9.sp, color = Color(0xFF8896AB))
                                        val takeProfitVal = exit?.take_profit_price ?: 0.0
                                        Text(if (takeProfitVal > 0) "¥${String.format("%.2f", takeProfitVal)}" else "-", fontSize = 11.sp, fontWeight = FontWeight.SemiBold, color = Color(0xFF067647))
                                    }
                                    Column(modifier = Modifier.weight(1f)) {
                                        Text("减仓触发价", fontSize = 9.sp, color = Color(0xFF8896AB))
                                        val trimVal = exit?.trim_price ?: 0.0
                                        Text(if (trimVal > 0) "¥${String.format("%.2f", trimVal)}" else "-", fontSize = 11.sp, fontWeight = FontWeight.SemiBold, color = Color(0xFFB54708))
                                    }
                                }
                            }
                        }
                    }

                    Spacer(modifier = Modifier.height(12.dp))

                    Text(
                        text = "辩论过程与流式分析",
                        fontSize = 13.sp,
                        fontWeight = FontWeight.Bold,
                        color = Color(0xFF18202B),
                        modifier = Modifier.padding(bottom = 6.dp)
                    )

                    // Text Debate logs
                    Box(
                        modifier = Modifier
                            .fillMaxWidth()
                            .clip(RoundedCornerShape(8.dp))
                            .background(Color.White)
                            .border(0.5.dp, Color(0xFFE6EBF2), RoundedCornerShape(8.dp))
                            .padding(12.dp)
                    ) {
                        Text(
                            text = debateLog,
                            fontSize = 13.sp,
                            color = Color(0xFF18202B),
                            lineHeight = 18.sp
                        )
                    }
                }
            }
        }
    }

    if (showTradeDialog && resolvedRow != null) {
        val resRow = resolvedRow!!
        val finalVerdict = tradeVerdict.ifEmpty { (symbolDetails?.get("verdict") as? String ?: resRow.operation?.verdict ?: "HOLD").uppercase() }
        val suggestedAlloc = if (committeeAnalysisEnabled) {
            symbolDetails?.get("suggested_alloc_cny") as? Double ?: resRow.operation?.suggested_alloc_cny ?: 0.0
        } else {
            0.0
        }

        TradeExecutionDialog(
            row = resRow,
            verdict = finalVerdict,
            suggestedAlloc = suggestedAlloc,
            onClose = { showTradeDialog = false },
            onDone = { msg ->
                showTradeDialog = false
                onTradeExecuted(msg)
                onClose()
            }
        )
    }

    if (showUpdateHoldingDialog && resolvedRow != null) {
        UpdateHoldingDialog(
            row = resolvedRow!!,
            onClose = { showUpdateHoldingDialog = false },
            onDone = { msg ->
                showUpdateHoldingDialog = false
                onTradeExecuted(msg)
                onClose()
            }
        )
    }
}

// --- Trade Execution Dialogue ---
@Composable
fun TradeExecutionDialog(
    row: HoldingRow,
    verdict: String,
    suggestedAlloc: Double,
    onClose: () -> Unit,
    onDone: (String) -> Unit
) {
    val context = LocalContext.current
    var showPostTradeUploadConfirm by remember { mutableStateOf(false) }
    var successMessage by remember { mutableStateOf("") }
    val direction = if (verdict in listOf("BUY", "ACCUMULATE")) "BUY" else "SELL"
    var unitsStr by remember {
        val estUnits = if (row.price.current > 0) (suggestedAlloc / row.price.current) else 0.0
        val lotSize = if (row.min_lot_size > 0) row.min_lot_size else 100
        val defaultUnits = if (estUnits >= lotSize) {
            (Math.floor(estUnits / lotSize) * lotSize).toInt()
        } else {
            lotSize
        }
        mutableStateOf(defaultUnits.toString())
    }
    var priceStr by remember { mutableStateOf(row.price.current.toString()) }
    var executing by remember { mutableStateOf(false) }

    Dialog(onDismissRequest = onClose) {
        Card(
            modifier = Modifier.fillMaxWidth(),
            shape = RoundedCornerShape(12.dp),
            colors = CardDefaults.cardColors(containerColor = Color.White)
        ) {
            Column(modifier = Modifier.padding(16.dp)) {
                Text(
                    text = "确认执行交易记账",
                    fontSize = 16.sp,
                    fontWeight = FontWeight.Bold,
                    color = Color(0xFF18202B)
                )
                Spacer(modifier = Modifier.height(12.dp))

                Text("标的: ${row.name} (${row.symbol})", fontSize = 13.sp, color = Color(0xFF18202B))
                Text("方向: $direction", fontSize = 13.sp, color = if (direction == "BUY") Color(0xFF067647) else Color(0xFFB42318), fontWeight = FontWeight.Bold)
                Text("建议分配资金: ¥${String.format("%.2f", suggestedAlloc)}", fontSize = 13.sp, color = Color(0xFF667085))

                Spacer(modifier = Modifier.height(12.dp))

                OutlinedTextField(
                    value = unitsStr,
                    onValueChange = { unitsStr = it },
                    modifier = Modifier.fillMaxWidth(),
                    label = { Text("股数 (1手 = 100股)") },
                    singleLine = true
                )

                Spacer(modifier = Modifier.height(8.dp))

                OutlinedTextField(
                    value = priceStr,
                    onValueChange = { priceStr = it },
                    modifier = Modifier.fillMaxWidth(),
                    label = { Text("成交单价 (CNY)") },
                    singleLine = true
                )

                Spacer(modifier = Modifier.height(16.dp))

                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.End
                ) {
                    TextButton(onClick = onClose) { Text("取消") }
                    Spacer(modifier = Modifier.width(8.dp))

                    Button(
                        onClick = {
                            val units = unitsStr.toDoubleOrNull() ?: 0.0
                            val price = priceStr.toDoubleOrNull() ?: 0.0
                            if (units <= 0.0 || price <= 0.0) {
                                return@Button
                            }
                            executing = true
                            NetworkClient.executeTrade(
                                symbol = row.symbol,
                                direction = direction,
                                units = units,
                                price = price,
                                note = "android_client_exec"
                            ) { res ->
                                executing = false
                                res.fold(
                                    onSuccess = { msg ->
                                        successMessage = "已执行记账: ${row.name} $direction ${units.toInt()}股 @ $price"
                                        showPostTradeUploadConfirm = true
                                    },
                                    onFailure = { err ->
                                        onDone("记账失败: ${err.message}")
                                    }
                                )
                            }
                        },
                        enabled = !executing,
                        colors = ButtonDefaults.buttonColors(containerColor = Color(0xFF067647))
                    ) {
                        Text(if (executing) "执行中..." else "确认记账")
                    }
                }
            }
        }
    }

    if (showPostTradeUploadConfirm) {
        AlertDialog(
            onDismissRequest = { 
                showPostTradeUploadConfirm = false
                onDone(successMessage)
            },
            title = { Text("交易成功", fontWeight = FontWeight.Bold) },
            text = { Text("买卖操作已记账成功，是否将最新的持仓数据上传到远程服务器？") },
            confirmButton = {
                TextButton(
                    onClick = {
                        showPostTradeUploadConfirm = false
                        NetworkClient.downloadMonitorConfig { dlRes ->
                            dlRes.fold(
                                onSuccess = { configContent ->
                                    saveLocalFile(context, "market_monitor_config.json", configContent)
                                    NetworkClient.uploadMonitorConfig(configContent) { ulRes ->
                                        ulRes.fold(
                                            onSuccess = {
                                                Toast.makeText(context, "持仓数据上传成功！", Toast.LENGTH_SHORT).show()
                                                onDone(successMessage)
                                            },
                                            onFailure = { err ->
                                                Toast.makeText(context, "上传持仓数据失败: ${err.message}", Toast.LENGTH_LONG).show()
                                                onDone(successMessage)
                                            }
                                        )
                                    }
                                },
                                onFailure = { err ->
                                    Toast.makeText(context, "获取最新配置失败，无法上传: ${err.message}", Toast.LENGTH_LONG).show()
                                    onDone(successMessage)
                                }
                            )
                        }
                    }
                ) {
                    Text("确认", color = Color(0xFF10B981))
                }
            },
            dismissButton = {
                TextButton(
                    onClick = {
                        showPostTradeUploadConfirm = false
                        onDone(successMessage)
                    }
                ) {
                    Text("取消", color = TextSecondary)
                }
            }
        )
    }
}

@Composable
fun UpdateHoldingDialog(
    row: HoldingRow,
    onClose: () -> Unit,
    onDone: (String) -> Unit
) {
    var unitsStr by remember { mutableStateOf(row.units.toInt().toString()) }
    var costStr by remember { mutableStateOf(row.cost.toString()) }
    var isTrackingOnly by remember { mutableStateOf(!row.is_holding) }
    var executing by remember { mutableStateOf(false) }

    Dialog(onDismissRequest = onClose) {
        Card(
            modifier = Modifier.fillMaxWidth(),
            shape = RoundedCornerShape(12.dp),
            colors = CardDefaults.cardColors(containerColor = Color.White)
        ) {
            Column(modifier = Modifier.padding(16.dp)) {
                Text(
                    text = "修正持仓/自选数据",
                    fontSize = 16.sp,
                    fontWeight = FontWeight.Bold,
                    color = Color(0xFF18202B)
                )
                Spacer(modifier = Modifier.height(12.dp))

                Text("标的: ${row.name} (${row.symbol})", fontSize = 13.sp, color = Color(0xFF18202B))
                Spacer(modifier = Modifier.height(12.dp))

                OutlinedTextField(
                    value = unitsStr,
                    onValueChange = { unitsStr = it },
                    modifier = Modifier.fillMaxWidth(),
                    label = { Text("持仓股数") },
                    singleLine = true
                )

                Spacer(modifier = Modifier.height(8.dp))

                OutlinedTextField(
                    value = costStr,
                    onValueChange = { costStr = it },
                    modifier = Modifier.fillMaxWidth(),
                    label = { Text("持仓均价 (CNY)") },
                    singleLine = true
                )

                Spacer(modifier = Modifier.height(12.dp))

                Row(
                    verticalAlignment = Alignment.CenterVertically,
                    modifier = Modifier.clickable { isTrackingOnly = !isTrackingOnly }
                ) {
                    Checkbox(
                        checked = isTrackingOnly,
                        onCheckedChange = { isTrackingOnly = it }
                    )
                    Spacer(modifier = Modifier.width(4.dp))
                    Text("仅加入自选观察 (无持仓)", fontSize = 13.sp, color = Color(0xFF18202B))
                }

                Spacer(modifier = Modifier.height(16.dp))

                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.End
                ) {
                    TextButton(onClick = onClose) { Text("取消") }
                    Spacer(modifier = Modifier.width(8.dp))

                    Button(
                        onClick = {
                            val units = unitsStr.toDoubleOrNull() ?: 0.0
                            val cost = costStr.toDoubleOrNull() ?: 0.0
                            executing = true
                            NetworkClient.updateHolding(
                                symbol = row.symbol,
                                units = if (isTrackingOnly) 0.0 else units,
                                avgCost = if (isTrackingOnly) 0.0 else cost,
                                isTrackingOnly = isTrackingOnly
                            ) { res ->
                                executing = false
                                res.fold(
                                    onSuccess = { msg ->
                                        onDone(msg)
                                    },
                                    onFailure = { err ->
                                        // Handled
                                    }
                                )
                            }
                        },
                        enabled = !executing
                    ) {
                        Text(if (executing) "更新中..." else "确认更新")
                    }
                }
            }
        }
    }
}

// --- Helpers ---


fun triggerRefreshService(context: Context) {
    val prefs = context.getSharedPreferences("open_invest_prefs", Context.MODE_PRIVATE)
    val enabled = prefs.getBoolean("auto_refresh_enabled", false)
    val intent = Intent(context, AutoRefreshService::class.java)
    if (enabled) {
        val interval = prefs.getInt("auto_refresh_interval", 30)
        val refreshNews = prefs.getBoolean("auto_refresh_news_enabled", true)
        val refreshSelection = prefs.getBoolean("auto_refresh_selection_enabled", true)
        val refreshPrices = prefs.getBoolean("auto_refresh_prices_enabled", true)
        
        intent.putExtra("interval", interval)
        intent.putExtra("refresh_news", refreshNews)
        intent.putExtra("refresh_selection", refreshSelection)
        intent.putExtra("refresh_prices", refreshPrices)
        
        try {
            context.startService(intent)
            android.util.Log.d("MainActivity", "Started AutoRefreshService with interval=$interval, news=$refreshNews, selection=$refreshSelection, prices=$refreshPrices")
        } catch (e: Exception) {
            android.util.Log.e("MainActivity", "Failed to start AutoRefreshService", e)
        }
    } else {
        try {
            context.stopService(intent)
            android.util.Log.d("MainActivity", "Stopped AutoRefreshService")
        } catch (e: Exception) {
            android.util.Log.e("MainActivity", "Failed to stop AutoRefreshService", e)
        }
    }
}

fun showNotification(context: Context, title: String, content: String) {
    val prefs = context.getSharedPreferences("open_invest_prefs", Context.MODE_PRIVATE)
    val notificationsEnabled = prefs.getBoolean("notifications_enabled", true)
    if (!notificationsEnabled) return

    val channelId = "open_invest_notifications"
    val notificationManager = context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager

    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
        val channel = NotificationChannel(
            channelId,
            "OpenInvest Notifications",
            NotificationManager.IMPORTANCE_DEFAULT
        ).apply {
            description = "Notification Channel for OpenInvest Analysis updates"
        }
        notificationManager.createNotificationChannel(channel)
    }

    val builder = NotificationCompat.Builder(context, channelId)
        .setSmallIcon(android.R.drawable.ic_dialog_info)
        .setContentTitle(title)
        .setContentText(content)
        .setPriority(NotificationCompat.PRIORITY_DEFAULT)
        .setAutoCancel(true)

    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
        if (ContextCompat.checkSelfPermission(
                context,
                Manifest.permission.POST_NOTIFICATIONS
            ) == PackageManager.PERMISSION_GRANTED
        ) {
            notificationManager.notify(System.currentTimeMillis().toInt(), builder.build())
        }
    } else {
        notificationManager.notify(System.currentTimeMillis().toInt(), builder.build())
    }
}

fun readTextFromUri(context: Context, uri: Uri): String? {
    return try {
        context.contentResolver.openInputStream(uri)?.use { inputStream ->
            inputStream.bufferedReader().use { it.readText() }
        }
    } catch (e: Exception) {
        Log.e("MainActivity", "Error reading uri: $uri", e)
        null
    }
}

fun saveLocalFile(context: Context, filename: String, content: String) {
    try {
        context.openFileOutput(filename, Context.MODE_PRIVATE).use {
            it.write(content.toByteArray(Charsets.UTF_8))
        }
    } catch (e: Exception) {
        Log.e("MainActivity", "Failed to save local file $filename", e)
    }
}

fun readLocalFile(context: Context, filename: String): String? {
    return try {
        context.openFileInput(filename).use {
            it.bufferedReader().use { reader -> reader.readText() }
        }
    } catch (e: Exception) {
        Log.e("MainActivity", "Failed to read local file $filename", e)
        null
    }
}