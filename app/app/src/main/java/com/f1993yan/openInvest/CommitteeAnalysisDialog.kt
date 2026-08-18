package com.f1993yan.openInvest

import android.content.Context
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Close
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.window.Dialog
import androidx.compose.ui.window.DialogProperties
import com.f1993yan.openInvest.network.*
import com.google.gson.Gson
import com.google.gson.reflect.TypeToken
import okhttp3.*
import java.io.IOException

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

    var statusLog by remember(symbol) { mutableStateOf("正在获取最新价格并运行委员会分析...") }
    var taskStatus by remember(symbol) { mutableStateOf<CommitteeStatusResponse?>(null) }
    var streamCall by remember(symbol) { mutableStateOf<Call?>(null) }
    var isResolving by remember(symbol) { mutableStateOf(row._is_resolving) }

    var resolvedRow by remember(symbol) { mutableStateOf<HoldingRow?>(if (row._is_resolving) null else row) }

    var addingWatchlist by remember(symbol) { mutableStateOf(false) }

    // Execute Trade Dialogue State
    var showTradeDialog by remember(symbol) { mutableStateOf(false) }
    var showUpdateHoldingDialog by remember(symbol) { mutableStateOf(false) }
    var tradeVerdict by remember(symbol) { mutableStateOf("") }

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
            changePct = targetRow.price.change_pct,
            ma20 = targetRow.technical?.ma20,
            ma120 = targetRow.technical?.ma120,
            atrPct = targetRow.technical?.atr_pct
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

                                val behavioralMap = symbolDetails?.get("behavioral_factor") as? Map<*, *>
                                val behavioral = resRow.behavioral_factor
                                val factorScore = (behavioralMap?.get("score") as? Number)?.toDouble() ?: behavioral?.score
                                val factorTarget = (behavioralMap?.get("target_weight_pct") as? Number)?.toDouble() ?: behavioral?.target_weight_pct
                                val factorWeight = (behavioralMap?.get("optimizer_weight") as? Number)?.toDouble() ?: behavioral?.optimizer_weight
                                val factorReturn = (behavioralMap?.get("trailing_3m_factor_return_pct") as? Number)?.toDouble() ?: behavioral?.trailing_3m_factor_return_pct
                                val factorHit = (behavioralMap?.get("trailing_3m_hit_rate") as? Number)?.toDouble() ?: behavioral?.trailing_3m_hit_rate
                                val factorN = (behavioralMap?.get("trailing_3m_sample_size") as? Number)?.toInt() ?: behavioral?.trailing_3m_sample_size
                                val factorSelected = behavioralMap?.get("selected") as? Boolean ?: behavioral?.selected
                                val factorLowConfidence = behavioralMap?.get("low_confidence") as? Boolean ?: behavioral?.low_confidence

                                if (factorScore != null) {
                                    Spacer(modifier = Modifier.height(10.dp))
                                    HorizontalDivider(color = Color(0xFFEEF2F6))
                                    Spacer(modifier = Modifier.height(10.dp))
                                    Text("A股行为因子", fontSize = 11.sp, fontWeight = FontWeight.Bold, color = Color(0xFF667085))
                                    Spacer(modifier = Modifier.height(6.dp))
                                    Row(modifier = Modifier.fillMaxWidth()) {
                                        Column(modifier = Modifier.weight(1f)) {
                                            Text("因子分 / 状态", fontSize = 9.sp, color = Color(0xFF8896AB))
                                            Text(
                                                "${String.format("%.1f", factorScore)} · ${if (factorSelected == true) "前四" else "未入选"}",
                                                fontSize = 11.sp,
                                                fontWeight = FontWeight.SemiBold,
                                                color = if (factorSelected == true) Color(0xFF067647) else Color(0xFF475467)
                                            )
                                        }
                                        Column(modifier = Modifier.weight(1f)) {
                                            Text("目标仓位 / 权重", fontSize = 9.sp, color = Color(0xFF8896AB))
                                            Text(
                                                "${String.format("%.1f", factorTarget ?: 0.0)}% · ${String.format("%.2f", factorWeight ?: 0.0)}",
                                                fontSize = 11.sp,
                                                fontWeight = FontWeight.SemiBold,
                                                color = Color(0xFF18202B)
                                            )
                                        }
                                        Column(modifier = Modifier.weight(1f)) {
                                            Text("近3月收益 / 命中", fontSize = 9.sp, color = Color(0xFF8896AB))
                                            Text(
                                                "${String.format("%+.1f", factorReturn ?: 0.0)}% · ${String.format("%.0f", (factorHit ?: 0.0) * 100)}%",
                                                fontSize = 11.sp,
                                                fontWeight = FontWeight.SemiBold,
                                                color = if ((factorReturn ?: 0.0) >= 0) Color(0xFF067647) else Color(0xFFB42318)
                                            )
                                        }
                                    }
                                    Text(
                                        "三个月样本 n=${factorN ?: 0}${if (factorLowConfidence == true) " · 低置信度" else ""}",
                                        fontSize = 9.sp,
                                        color = if (factorLowConfidence == true) Color(0xFFB54708) else Color(0xFF667085),
                                        modifier = Modifier.padding(top = 4.dp)
                                    )
                                }

                                val hkSpatioMap = symbolDetails?.get("hk_spatio_factor") as? Map<*, *>
                                val hkSpatio = resRow.hk_spatio_factor
                                val hkScore = (hkSpatioMap?.get("score") as? Number)?.toDouble() ?: hkSpatio?.score
                                val hkTarget = (hkSpatioMap?.get("target_weight_pct") as? Number)?.toDouble() ?: hkSpatio?.target_weight_pct
                                val hkExpected = (hkSpatioMap?.get("expected_return_pct") as? Number)?.toDouble() ?: hkSpatio?.expected_return_pct
                                val hkSample = (hkSpatioMap?.get("sample_size") as? Number)?.toInt() ?: hkSpatio?.sample_size
                                val hkSelected = hkSpatioMap?.get("selected") as? Boolean ?: hkSpatio?.selected
                                val hkLowConfidence = hkSpatioMap?.get("low_confidence") as? Boolean ?: hkSpatio?.low_confidence

                                if (hkScore != null) {
                                    Spacer(modifier = Modifier.height(10.dp))
                                    HorizontalDivider(color = Color(0xFFEEF2F6))
                                    Spacer(modifier = Modifier.height(10.dp))
                                    Text("港股时空动量", fontSize = 11.sp, fontWeight = FontWeight.Bold, color = Color(0xFF667085))
                                    Spacer(modifier = Modifier.height(6.dp))
                                    Row(modifier = Modifier.fillMaxWidth()) {
                                        Column(modifier = Modifier.weight(1f)) {
                                            Text("因子分 / 状态", fontSize = 9.sp, color = Color(0xFF8896AB))
                                            Text(
                                                "${String.format("%.1f", hkScore)} · ${if (hkSelected == true) "前四" else "未入选"}",
                                                fontSize = 11.sp,
                                                fontWeight = FontWeight.SemiBold,
                                                color = if (hkSelected == true) Color(0xFF067647) else Color(0xFF475467)
                                            )
                                        }
                                        Column(modifier = Modifier.weight(1f)) {
                                            Text("目标仓位", fontSize = 9.sp, color = Color(0xFF8896AB))
                                            Text(
                                                "${String.format("%.1f", hkTarget ?: 0.0)}%",
                                                fontSize = 11.sp,
                                                fontWeight = FontWeight.SemiBold,
                                                color = Color(0xFF18202B)
                                            )
                                        }
                                        Column(modifier = Modifier.weight(1f)) {
                                            Text("20日预期 / 样本", fontSize = 9.sp, color = Color(0xFF8896AB))
                                            Text(
                                                "${String.format("%+.1f", hkExpected ?: 0.0)}% · n=${hkSample ?: 0}",
                                                fontSize = 11.sp,
                                                fontWeight = FontWeight.SemiBold,
                                                color = if ((hkExpected ?: 0.0) >= 0) Color(0xFF067647) else Color(0xFFB42318)
                                            )
                                        }
                                    }
                                    if (hkLowConfidence == true) {
                                        Text(
                                            "横截面或历史样本不足，本轮暂停交易",
                                            fontSize = 9.sp,
                                            color = Color(0xFFB54708),
                                            modifier = Modifier.padding(top = 4.dp)
                                        )
                                    }
                                }

                                Spacer(modifier = Modifier.height(10.dp))
                                HorizontalDivider(color = Color(0xFFEEF2F6))
                                Spacer(modifier = Modifier.height(10.dp))

                                // Buy Criteria
                                Text("进场条件 (买点)", fontSize = 11.sp, fontWeight = FontWeight.Bold, color = Color(0xFF667085))
                                Spacer(modifier = Modifier.height(6.dp))
                                val buy = resRow.buy_criteria
                                val eePoints = symbolDetails?.get("entry_exit_points") as? Map<*, *>
                                Row(modifier = Modifier.fillMaxWidth()) {
                                    Column(modifier = Modifier.weight(1f)) {
                                        Text("回踩买入价", fontSize = 9.sp, color = Color(0xFF8896AB))
                                        val pullbackVal = (eePoints?.get("buy_pullback_price") as? Number)?.toDouble() ?: buy?.pullback_price ?: 0.0
                                        Text(if (pullbackVal > 0) "¥${String.format("%.2f", pullbackVal)}" else "-", fontSize = 11.sp, fontWeight = FontWeight.SemiBold, color = Color(0xFF18202B))
                                    }
                                    Column(modifier = Modifier.weight(1f)) {
                                        Text("突破买入价", fontSize = 9.sp, color = Color(0xFF8896AB))
                                        val breakoutVal = (eePoints?.get("buy_breakout_price") as? Number)?.toDouble() ?: buy?.breakout_price ?: 0.0
                                        Text(if (breakoutVal > 0) "¥${String.format("%.2f", breakoutVal)}" else "-", fontSize = 11.sp, fontWeight = FontWeight.SemiBold, color = Color(0xFF18202B))
                                    }
                                    Column(modifier = Modifier.weight(1f)) {
                                        Text("二次探底买入价", fontSize = 9.sp, color = Color(0xFF8896AB))
                                        val reentryVal = (eePoints?.get("reentry_price") as? Number)?.toDouble() ?: buy?.reentry_price ?: 0.0
                                        Text(if (reentryVal > 0) "¥${String.format("%.2f", reentryVal)}" else "-", fontSize = 11.sp, fontWeight = FontWeight.SemiBold, color = Color(0xFF18202B))
                                    }
                                }

                                val rrVal = (eePoints?.get("reward_risk_ratio") as? Number)?.toDouble() ?: buy?.reward_risk_ratio ?: 0.0
                                if (rrVal > 0) {
                                    Spacer(modifier = Modifier.height(4.dp))
                                    Text("预计盈亏比: ${String.format("%.1f", rrVal)}", fontSize = 10.sp, color = Color(0xFF475467))
                                }

                                val buyReason = eePoints?.get("reason") as? String ?: buy?.reason
                                if (!buyReason.isNullOrBlank()) {
                                    Spacer(modifier = Modifier.height(4.dp))
                                    val translatedReason = when (buyReason) {
                                        "conditional_quantile_volatility_scaled_levels" -> "基于历史分布(分位数)与波动率(ATR)自适应计算"
                                        "missing_current_price" -> "缺少当前最新价"
                                        "atr_regime_fallback" -> "基于波动率自适应估算(回退模型)"
                                        "regime_quantile_atr_cvar" -> "基于量化模型(分位数与条件在险价值CVaR)估算"
                                        else -> buyReason
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
                                        val stopLossVal = (eePoints?.get("stop_loss_price") as? Number)?.toDouble() ?: exit?.stop_loss_price ?: 0.0
                                        Text(if (stopLossVal > 0) "¥${String.format("%.2f", stopLossVal)}" else "-", fontSize = 11.sp, fontWeight = FontWeight.SemiBold, color = Color(0xFFB42318))
                                    }
                                    Column(modifier = Modifier.weight(1f)) {
                                        Text("止盈价", fontSize = 9.sp, color = Color(0xFF8896AB))
                                        val takeProfitVal = (eePoints?.get("take_profit_price") as? Number)?.toDouble() ?: exit?.take_profit_price ?: 0.0
                                        Text(if (takeProfitVal > 0) "¥${String.format("%.2f", takeProfitVal)}" else "-", fontSize = 11.sp, fontWeight = FontWeight.SemiBold, color = Color(0xFF067647))
                                    }
                                    Column(modifier = Modifier.weight(1f)) {
                                        Text("减仓触发价", fontSize = 9.sp, color = Color(0xFF8896AB))
                                        val trimVal = (eePoints?.get("trim_price") as? Number)?.toDouble() ?: exit?.trim_price ?: 0.0
                                        Text(if (trimVal > 0) "¥${String.format("%.2f", trimVal)}" else "-", fontSize = 11.sp, fontWeight = FontWeight.SemiBold, color = Color(0xFFB54708))
                                    }
                                }
                            }
                        }
                    }

                    if (isDebatingDone && debateLog.isNotEmpty()) {
                        Spacer(modifier = Modifier.height(12.dp))
                        FormattedReportCard(reportText = debateLog)
                    } else if (debateLog.isNotEmpty()) {
                        Spacer(modifier = Modifier.height(12.dp))
                        Text(
                            text = "流式分析与进度过程",
                            fontSize = 13.sp,
                            fontWeight = FontWeight.Bold,
                            color = Color(0xFF18202B),
                            modifier = Modifier.padding(bottom = 6.dp)
                        )
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

@Composable
fun FormattedReportCard(reportText: String) {
    Card(
        modifier = Modifier
            .fillMaxWidth()
            .padding(bottom = 12.dp),
        colors = CardDefaults.cardColors(containerColor = Color.White),
        border = BorderStroke(0.5.dp, Color(0xFFE6EBF2)),
        shape = RoundedCornerShape(10.dp)
    ) {
        Column(modifier = Modifier.padding(16.dp)) {
            Text(
                text = "📋 投资委员会决策报告",
                fontSize = 14.sp,
                fontWeight = FontWeight.Bold,
                color = Color(0xFF1B4E7A)
            )
            Spacer(modifier = Modifier.height(10.dp))

            val lines = reportText.split("\n")
            var skippedHeader = false
            var underNeedCare = false

            for (line in lines) {
                val trimmed = line.trim()
                if (trimmed.isEmpty()) continue

                // Skip the duplicate stock title and its decoration line at the top
                if (!skippedHeader && (trimmed.contains("(") || trimmed.contains(")") || trimmed.contains("=="))) {
                    if (trimmed.startsWith("==")) {
                        skippedHeader = true
                    }
                    continue
                }

                // Filter out debug logs, internal config parameters, and optimizer raw traces
                if (trimmed.startsWith("==") || trimmed.startsWith("--") || trimmed.startsWith("——") || trimmed.startsWith("—")) continue
                if (trimmed.contains("[") && trimmed.contains("]")) continue
                if (trimmed.startsWith("###")) continue
                if (trimmed.contains("交易约束")) continue
                if (trimmed.startsWith("- 🔴")) continue
                if (trimmed.contains("suggested_alloc_cny")) continue
                if (trimmed.contains("止盈止损纪律")) continue
                if (trimmed.contains("plan_type")) continue
                if (trimmed.contains("audit_text")) continue
                if (trimmed.contains("optimizer_review")) continue
                if (trimmed.startsWith("lots=") || trimmed.startsWith("side=") || trimmed.startsWith("reason=") || trimmed.startsWith("black_litterman")) continue

                // Track if we are under the "需要小心" warning section to color items red
                if (trimmed.contains("需要小心")) {
                    underNeedCare = true
                } else if (trimmed.startsWith("你最该先看") || trimmed.startsWith("为什么这么判断") || trimmed.startsWith("下一步")) {
                    underNeedCare = false
                }

                // Determine styling category
                val isHeader = trimmed.startsWith("你最该先看") || trimmed.startsWith("为什么这么判断") || trimmed.startsWith("下一步") || trimmed.contains("需要小心")
                val isVerdictLine = trimmed.startsWith("结论") || trimmed.contains("决策结论")
                val isGreen = isVerdictLine || trimmed.contains("理想买点") || trimmed.contains("技术面") || trimmed.contains("右侧趋势闸门")
                val isRed = underNeedCare || trimmed.contains("风险线") || trimmed.contains("止损") || trimmed.contains("波动率风险") || trimmed.contains("买点不匹配") || trimmed.contains("低置信度")
                val isGray = trimmed.startsWith("- 基本面") || trimmed.contains("决策证据") || trimmed.contains("口径冲突") || trimmed.contains("模型提醒")

                val textColor = when {
                    isHeader && trimmed.contains("需要小心") -> Color(0xFFC62828)
                    isHeader -> Color(0xFF18202B)
                    isRed -> Color(0xFFC62828) // Deep Crimson Red
                    isGreen -> Color(0xFF2E7D32) // Forest Green
                    isGray -> Color(0xFF757575) // Cool Gray
                    else -> Color(0xFF2E2E2E) // Soft Black
                }

                val fontWeight = if (isHeader || isVerdictLine) FontWeight.Bold else FontWeight.Normal
                val fontSize = if (isHeader) 13.sp else 12.sp

                Text(
                    text = line,
                    fontSize = fontSize,
                    fontWeight = fontWeight,
                    color = textColor,
                    lineHeight = 18.sp,
                    modifier = Modifier.padding(vertical = 3.dp)
                )
            }
        }
    }
}
