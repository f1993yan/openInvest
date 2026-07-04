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

private val STATE_PRIORITY = mapOf(
    "action_required" to 0,
    "trigger_confirmed" to 1,
    "watch_trigger" to 2,
    "candidate" to 3,
    "monitoring" to 4,
    "blocked" to 5,
    "error" to 6
)

private val VERDICT_PRIORITY = mapOf(
    "SELL" to 0,
    "TRIM" to 1,
    "BUY" to 2,
    "ACCUMULATE" to 3,
    "HOLD" to 4,
    "WAIT" to 5
)

private val executionOrderComparator = Comparator<HoldingRow> { a, b ->
    val stateA = STATE_PRIORITY[a.state] ?: 99
    val stateB = STATE_PRIORITY[b.state] ?: 99
    if (stateA != stateB) return@Comparator stateA.compareTo(stateB)

    val statusAStr = a.operation?.status ?: a.state
    val statusBStr = b.operation?.status ?: b.state
    val statusA = STATE_PRIORITY[statusAStr] ?: 99
    val statusB = STATE_PRIORITY[statusBStr] ?: 99
    if (statusA != statusB) return@Comparator statusA.compareTo(statusB)

    val confA = if (a.operation?.confirmed == true) 1 else 0
    val confB = if (b.operation?.confirmed == true) 1 else 0
    if (confA != confB) return@Comparator confB.compareTo(confA)

    val alertA = a.operation?.alert_score ?: 0.0
    val alertB = b.operation?.alert_score ?: 0.0
    if (alertA != alertB) return@Comparator alertB.compareTo(alertA)

    val verdA = VERDICT_PRIORITY[a.operation?.verdict?.uppercase()] ?: 99
    val verdB = VERDICT_PRIORITY[b.operation?.verdict?.uppercase()] ?: 99
    if (verdA != verdB) return@Comparator verdA.compareTo(verdB)

    val allocA = a.operation?.suggested_alloc_cny ?: 0.0
    val allocB = b.operation?.suggested_alloc_cny ?: 0.0
    if (allocA != allocB) return@Comparator allocB.compareTo(allocA)

    val changeA = Math.abs(a.price.change_pct)
    val changeB = Math.abs(b.price.change_pct)
    if (changeA != changeB) return@Comparator changeB.compareTo(changeA)

    return@Comparator a.symbol.compareTo(b.symbol)
}

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
    var isCommitteeUpdated by remember { mutableStateOf(false) }

    LaunchedEffect(snapshot) {
        while (true) {
            val pendingSymbol = MainActivity.pendingTradeSymbol
            if (pendingSymbol != null) {
                val matchingRow = snapshot?.rows?.find { it.symbol.equals(pendingSymbol, ignoreCase = true) }
                    ?: com.f1993yan.openInvest.network.HoldingRow(
                        symbol = pendingSymbol,
                        name = pendingSymbol,
                        market = "A股",
                        price = com.f1993yan.openInvest.network.HoldingRowPrice(current = 0.0, prev_close = 0.0, change_pct = 0.0),
                        units = 0.0,
                        cost = 0.0,
                        position_pct = 0.0,
                        target_position_pct = 0.0,
                        is_holding = false,
                        min_lot_size = 100,
                        state = "MONITORING"
                    )

                activeTradeSymbol = pendingSymbol
                activeTradeRow = matchingRow
                activeTradeVerdict = MainActivity.pendingTradeVerdict ?: "BUY"
                activeTradeSuggestedAlloc = MainActivity.pendingTradeSuggestedAlloc

                // Clear pending fields
                MainActivity.pendingTradeSymbol = null
                MainActivity.pendingTradeVerdict = null
                MainActivity.pendingTradeSuggestedAlloc = 0.0
            }
            kotlinx.coroutines.delay(500)
        }
    }

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
                var cachedRow = cachedSnap?.rows?.find { it.symbol.equals(row.symbol, ignoreCase = true) }
                if ((cachedRow == null || cachedRow.operation == null) && localCommitteeResults.containsKey(row.symbol.uppercase())) {
                    val cachedJson = localCommitteeResults[row.symbol.uppercase()]
                    if (cachedJson != null) {
                        val parsedFields = parseCachedResult(row.symbol, cachedJson)
                        if (parsedFields != null) {
                            cachedRow = row.copy(
                                exit_points = parsedFields.exitPoints,
                                buy_criteria = parsedFields.buyCriteria,
                                operation = parsedFields.operation,
                                fundamental = parsedFields.fundamental,
                                llm_review = parsedFields.llmReview,
                                success = true
                            )
                        }
                    }
                }
                val holdingsJson = try {
                    val list = rows.filter { it.is_holding && it.symbol != row.symbol }.map { h ->
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
                               snap.generated_at == cachedSnap?.generated_at &&
                               snap.cash_cny == cachedSnap?.cash_cny &&
                               snap.total_assets_cny == cachedSnap?.total_assets_cny &&
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
                isCommitteeUpdated = true

                val resultsList = localCommitteeResults.values.toList()
                if (resultsList.isNotEmpty()) {
                    val resultsJson = "[" + resultsList.joinToString(",") + "]"
                    val pricesMap = rows.associate { r -> r.symbol.uppercase() to mapOf("price" to r.price.current) }
                    val pricesJson = gson.toJson(pricesMap)
                    val holdingSymbols = rows.filter { it.is_holding }.map { it.symbol.uppercase() }
                    val holdingSymbolsJson = gson.toJson(holdingSymbols)
                    val stocksList = rows.map { r ->
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

            val totalCount = rowsToAnalyze.size
            val completedCount = java.util.concurrent.atomic.AtomicInteger(0)
            DynamicIslandManager.startAnalysis(context, rowsToAnalyze.firstOrNull()?.name ?: rowsToAnalyze.firstOrNull()?.symbol ?: "", totalCount)
            Log.d("MainActivity", "Starting background local committee analysis for $totalCount symbols...")

            val semaphore = Semaphore(3)
            val jobs = rowsToAnalyze.map { row ->
                launch {
                    semaphore.withPermit {
                        DynamicIslandManager.updateAnalysisProgress(context, row.name ?: row.symbol, completedCount.get(), totalCount)
                        withContext(Dispatchers.Main) {
                            updateSnapshotRow(row.symbol) { it.copy(_is_resolving = true) }
                        }

                        val holdingsJson = try {
                            val list = rows.filter { it.is_holding && it.symbol != row.symbol }.map { h ->
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
                            changePct = row.price.change_pct,
                            ma20 = row.technical?.ma20,
                            ma120 = row.technical?.ma120,
                            atrPct = row.technical?.atr_pct
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
                                        val resultsCacheJson = gson.toJson(localCommitteeResults.toMap())
                                        saveLocalFile(context, "committee_results_cache.json", resultsCacheJson)
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
                            val currentCompleted = completedCount.incrementAndGet()
                            DynamicIslandManager.updateAnalysisProgress(context, row.name ?: row.symbol, currentCompleted, totalCount)
                        }
                    }
                }
            }
            jobs.joinAll()
            DynamicIslandManager.finishAnalysis()
            isBackgroundRunning = false
            isCommitteeUpdated = true
            Log.d("MainActivity", "All background local committee runs completed! Evaluating triggers...")

            val resultsList = localCommitteeResults.values.toList()
            if (resultsList.isNotEmpty()) {
                val resultsJson = "[" + resultsList.joinToString(",") + "]"
                val pricesMap = rows.associate { r -> r.symbol.uppercase() to mapOf("price" to r.price.current) }
                val pricesJson = gson.toJson(pricesMap)
                val holdingSymbols = rows.filter { it.is_holding }.map { it.symbol.uppercase() }
                val holdingSymbolsJson = gson.toJson(holdingSymbols)
                val stocksList = rows.map { r ->
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
        isCommitteeUpdated = false
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
            val filteredRows = remember(snapshot, searchQuery, isCommitteeUpdated) {
                val rows = snapshot?.rows ?: emptyList()
                val list = if (searchQuery.isEmpty()) {
                    rows
                } else {
                    rows.filter {
                        it.symbol.contains(searchQuery, ignoreCase = true) ||
                                it.name.contains(searchQuery, ignoreCase = true)
                    }
                }
                if (isCommitteeUpdated) {
                    list.sortedWith(executionOrderComparator)
                } else {
                    list.sortedByDescending { it.units * it.price.current }
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

                            if (NetworkClient.getBaseUrl().isNotEmpty()) {
                                NetworkClient.correctCash(originalCash + originalT2, 0.0) { res ->
                                    coroutineScope.launch(Dispatchers.Main) {
                                        res.fold(
                                            onSuccess = {
                                                Toast.makeText(context, "已更新且同步至云端", Toast.LENGTH_SHORT).show()
                                                refreshData()
                                            },
                                            onFailure = { err ->
                                                Toast.makeText(context, "同步云端失败: ${err.message}", Toast.LENGTH_LONG).show()
                                            }
                                        )
                                    }
                                }
                            } else {
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

                                    // Update market_monitor_config.json locally (offline fallback)
                                    val configStr = readLocalFile(context, "market_monitor_config.json")
                                    if (configStr != null) {
                                        try {
                                            val configObj = JSONObject(configStr)
                                            val configCash = configObj.optDouble("cash", 0.0)
                                            val configT2 = configObj.optDouble("t2_pending_cash", 0.0)
                                            configObj.put("cash", configCash + configT2)
                                            configObj.put("t2_pending_cash", 0.0)
                                            saveLocalFile(context, "market_monitor_config.json", configObj.toString(2))
                                            Toast.makeText(context, "本地持仓数据更新成功！", Toast.LENGTH_SHORT).show()
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

class ParsedCommitteeFields(
    val exitPoints: com.f1993yan.openInvest.network.ExitPoints?,
    val buyCriteria: com.f1993yan.openInvest.network.BuyCriteria?,
    val operation: com.f1993yan.openInvest.network.Operation?,
    val fundamental: com.f1993yan.openInvest.network.Fundamental?,
    val llmReview: com.f1993yan.openInvest.network.LlmReview?
)

fun parseCachedResult(symbol: String, rawJson: String): ParsedCommitteeFields? {
    try {
        val root = org.json.JSONObject(rawJson)
        val result = root.optJSONObject("result")
        val byAsset = result?.optJSONObject("by_asset") ?: root.optJSONObject("by_asset") ?: return null
        val cleanSym = symbol.uppercase()
        val symbolObj = byAsset.optJSONObject(cleanSym) ?: byAsset.optJSONObject(cleanSym.split(".")[0]) ?: return null

        val eePoints = symbolObj.optJSONObject("entry_exit_points")
        val exitPoints = com.f1993yan.openInvest.network.ExitPoints(
            stop_loss_price = eePoints?.optDouble("stop_loss_price")?.takeIf { !it.isNaN() && it > 0.0 },
            take_profit_price = eePoints?.optDouble("take_profit_price")?.takeIf { !it.isNaN() && it > 0.0 },
            trim_price = eePoints?.optDouble("trim_price")?.takeIf { !it.isNaN() && it > 0.0 }
        )

        val buyCriteria = com.f1993yan.openInvest.network.BuyCriteria(
            pullback_price = eePoints?.optDouble("buy_pullback_price")?.takeIf { !it.isNaN() && it > 0.0 },
            breakout_price = eePoints?.optDouble("buy_breakout_price")?.takeIf { !it.isNaN() && it > 0.0 },
            reentry_price = eePoints?.optDouble("reentry_price")?.takeIf { !it.isNaN() && it > 0.0 },
            reward_risk_ratio = eePoints?.optDouble("reward_risk_ratio")?.takeIf { !it.isNaN() && it > 0.0 },
            reason = eePoints?.optString("reason")?.takeIf { it.isNotEmpty() }
        )

        val verdict = symbolObj.optString("verdict", "HOLD")
        val suggestedAlloc = symbolObj.optDouble("suggested_alloc_cny", 0.0)

        val triggersList = mutableListOf<com.f1993yan.openInvest.network.TriggerItem>()
        val triggersArr = symbolObj.optJSONArray("triggers")
        if (triggersArr != null) {
            for (i in 0 until triggersArr.length()) {
                val t = triggersArr.getJSONObject(i)
                triggersList.add(
                    com.f1993yan.openInvest.network.TriggerItem(
                        side = t.optString("side"),
                        kind = t.optString("kind"),
                        level = t.optDouble("level"),
                        price = t.optDouble("price")
                    )
                )
            }
        }

        val operation = com.f1993yan.openInvest.network.Operation(
            verdict = verdict,
            suggested_alloc_cny = suggestedAlloc,
            status = "monitoring",
            triggers = triggersList
        )

        val fundamental = com.f1993yan.openInvest.network.Fundamental(
            model = symbolObj.optString("fundamental_model"),
            score = symbolObj.optDouble("fundamental_score", 50.0),
            coverage = symbolObj.optDouble("fundamental_coverage", 0.0),
            anchor_multiplier = symbolObj.optDouble("fundamental_anchor_multiplier", 1.0)
        )

        val reviewObj = symbolObj.optJSONObject("llm_review")
        val llmReview = com.f1993yan.openInvest.network.LlmReview(
            conclusion = reviewObj?.optString("conclusion"),
            one_line = reviewObj?.optString("one_line"),
            risk_note = reviewObj?.optString("risk_note"),
            execution_plan = reviewObj?.optString("execution_plan"),
            raw_excerpt = reviewObj?.optString("raw_excerpt")
        )

        return ParsedCommitteeFields(exitPoints, buyCriteria, operation, fundamental, llmReview)
    } catch (e: Exception) {
        android.util.Log.e("MainActivity", "Failed to parse cached committee result", e)
    }
    return null
}



// --- Settings Dialog ---
