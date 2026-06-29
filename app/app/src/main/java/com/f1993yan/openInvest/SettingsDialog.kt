package com.f1993yan.openInvest

import android.Manifest
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.provider.Settings
import android.util.Log
import android.widget.Toast
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Info
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.window.Dialog
import androidx.core.content.ContextCompat
import com.f1993yan.openInvest.network.CrawlerSettings
import com.f1993yan.openInvest.network.NetworkClient
import com.f1993yan.openInvest.ui.theme.*

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
    var tradingMode by remember { mutableStateOf(prefs.getString("trading_mode", "active_profit") ?: "active_profit") }
    var autoRefresh by remember { mutableStateOf(prefs.getBoolean("auto_refresh_enabled", false)) }
    var interval by remember { mutableStateOf(prefs.getInt("auto_refresh_interval", 30).toString()) }
    var refreshNews by remember { mutableStateOf(prefs.getBoolean("auto_refresh_news_enabled", true)) }
    var refreshSelection by remember { mutableStateOf(prefs.getBoolean("auto_refresh_selection_enabled", true)) }
    var refreshPrices by remember { mutableStateOf(prefs.getBoolean("auto_refresh_prices_enabled", true)) }
    var notificationsEnabled by remember { mutableStateOf(prefs.getBoolean("notifications_enabled", true)) }
    var dynamicIslandEnabled by remember { mutableStateOf(prefs.getBoolean("dynamic_island_enabled", true)) }
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
    var cashStr by remember { mutableStateOf("") }
    var t2CashStr by remember { mutableStateOf("") }

    LaunchedEffect(Unit) {
        val configStr = readLocalFile(context, "market_monitor_config.json")
        if (configStr != null) {
            try {
                val json = org.json.JSONObject(configStr)
                val cash = json.optDouble("cash", 0.0)
                val t2 = json.optDouble("t2_pending_cash", 0.0)
                cashStr = cash.toString()
                t2CashStr = t2.toString()
                val tm = json.opt("trading_mode")
                if (tm is String) {
                    tradingMode = tm
                } else if (tm is org.json.JSONObject) {
                    tradingMode = tm.optString("mode", "active_profit")
                }
            } catch (e: Exception) {
                Log.e("SettingsDialog", "Failed to parse config values", e)
            }
        }
    }

    val permissionLauncher = rememberLauncherForActivityResult(
        contract = ActivityResultContracts.RequestPermission()
    ) { isGranted ->
        notificationsEnabled = isGranted
        if (!isGranted) {
            Toast.makeText(context, "未获得通知权限，已关闭通知", Toast.LENGTH_SHORT).show()
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

                Column(
                    modifier = Modifier
                        .weight(1f)
                        .fillMaxWidth()
                        .verticalScroll(rememberScrollState())
                ) {
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

                    Text("系统交易模式 (控制委员会策略)", fontSize = 13.sp, fontWeight = FontWeight.Bold, color = IndigoPrimary)
                    Spacer(modifier = Modifier.height(8.dp))
                    Row(
                        modifier = Modifier
                            .fillMaxWidth()
                            .background(Color(0xFF1E293B), RoundedCornerShape(10.dp))
                            .padding(4.dp),
                        horizontalArrangement = Arrangement.spacedBy(4.dp)
                    ) {
                        listOf(
                            "active_profit" to "主动盈利",
                            "cash_recovery" to "现金回收",
                            "risk_off" to "主动避险"
                        ).forEach { (mode, label) ->
                            val isSelected = tradingMode == mode
                            val activeBgColor = when(mode) {
                                "active_profit" -> Color(0xFF6366F1)
                                "cash_recovery" -> Color(0xFFD97706)
                                "risk_off" -> Color(0xFFEF4444)
                                else -> Color(0xFF6366F1)
                            }
                            Box(
                                modifier = Modifier
                                    .weight(1f)
                                    .height(36.dp)
                                    .background(
                                        if (isSelected) activeBgColor else Color.Transparent,
                                        RoundedCornerShape(8.dp)
                                    )
                                    .clickable { tradingMode = mode },
                                contentAlignment = Alignment.Center
                            ) {
                                Text(
                                    text = label,
                                    color = if (isSelected) Color.White else TextSecondary,
                                    fontSize = 12.sp,
                                    fontWeight = if (isSelected) FontWeight.Bold else FontWeight.Normal
                                )
                            }
                        }
                    }

                    Spacer(modifier = Modifier.height(12.dp))
                    HorizontalDivider(color = SlateBorder, thickness = 1.dp)
                    Spacer(modifier = Modifier.height(12.dp))

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
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.SpaceBetween
                    ) {
                        Text("启用灵动岛悬浮通知", fontSize = 14.sp, color = TextPrimary)
                        Switch(
                            checked = dynamicIslandEnabled,
                            colors = SwitchDefaults.colors(checkedTrackColor = IndigoPrimary),
                            onCheckedChange = { checked ->
                                if (checked) {
                                    if (!Settings.canDrawOverlays(context)) {
                                        Toast.makeText(context, "请授予悬浮窗权限以启用灵动岛", Toast.LENGTH_LONG).show()
                                        val intent = Intent(
                                            Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                                            Uri.parse("package:${context.packageName}")
                                        )
                                        context.startActivity(intent)
                                        dynamicIslandEnabled = false
                                    } else {
                                        dynamicIslandEnabled = true
                                    }
                                } else {
                                    dynamicIslandEnabled = false
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

                    Text("导入配置文件", fontSize = 13.sp, fontWeight = FontWeight.Bold, color = IndigoPrimary)
                    Spacer(modifier = Modifier.height(6.dp))
                    Button(
                        onClick = {
                            exitParamsPicker.launch("application/json")
                        },
                        colors = ButtonDefaults.buttonColors(containerColor = IndigoPrimary),
                        shape = RoundedCornerShape(10.dp),
                        modifier = Modifier
                            .fillMaxWidth()
                            .height(40.dp)
                    ) {
                        Text("导入每周止盈配置", color = Color.White, fontSize = 11.sp, fontWeight = FontWeight.Bold)
                    }

                    Spacer(modifier = Modifier.height(14.dp))
                    HorizontalDivider(color = SlateBorder, thickness = 1.dp)
                    Spacer(modifier = Modifier.height(14.dp))

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
                                val sectorContent = readLocalFile(context, "sector_cache.json")
                                val prefs = context.getSharedPreferences("open_invest_prefs", Context.MODE_PRIVATE)
                                val envPolicies = prefs.getString("invest_a_share_sector_exit_policies", "") ?: ""
                                val ledgerBytes = readLocalBinaryFile(context, "account_ledger.sqlite")

                                if (configContent == null && exitContent == null && sectorContent == null && envPolicies.isEmpty() && ledgerBytes == null) {
                                    Toast.makeText(context, "没有本地数据可上传，请先导入配置", Toast.LENGTH_LONG).show()
                                    return@Button
                                }

                                var completedCount = 0
                                var totalToUpload = 0
                                if (configContent != null) totalToUpload++
                                if (exitContent != null) totalToUpload++
                                if (sectorContent != null) totalToUpload++
                                if (envPolicies.isNotEmpty()) totalToUpload++
                                if (ledgerBytes != null) totalToUpload++

                                fun checkComplete() {
                                    completedCount++
                                    if (completedCount == totalToUpload) {
                                        Toast.makeText(context, "所有配置文件及账本数据库上传成功！", Toast.LENGTH_LONG).show()
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

                                sectorContent?.let {
                                    NetworkClient.uploadSectorCache(it) { res ->
                                        res.fold(
                                            onSuccess = { checkComplete() },
                                            onFailure = { err ->
                                                Toast.makeText(context, "上传板块缓存失败: ${err.message}", Toast.LENGTH_LONG).show()
                                            }
                                        )
                                    }
                                }

                                if (envPolicies.isNotEmpty()) {
                                    NetworkClient.uploadEnvPolicies(envPolicies) { res ->
                                        res.fold(
                                            onSuccess = { checkComplete() },
                                            onFailure = { err ->
                                                Toast.makeText(context, "上传环境变量失败: ${err.message}", Toast.LENGTH_LONG).show()
                                            }
                                        )
                                    }
                                }

                                ledgerBytes?.let {
                                    NetworkClient.uploadAccountLedger(it) { res ->
                                        res.fold(
                                            onSuccess = { checkComplete() },
                                            onFailure = { err ->
                                                Toast.makeText(context, "上传账本数据库失败: ${err.message}", Toast.LENGTH_LONG).show()
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
                                text = { Text("您确定要从云端服务器同步所有配置文件与账本数据库吗？这将会覆盖本地现有的配置与数据！") },
                                confirmButton = {
                                    TextButton(
                                        onClick = {
                                            showSyncConfirmDialog = false
                                            var syncCount = 0
                                            val totalToSync = 5
                                            fun onSyncComplete() {
                                                syncCount++
                                                if (syncCount == totalToSync) {
                                                    Toast.makeText(context, "所有配置文件及账本数据库同步成功！", Toast.LENGTH_LONG).show()
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
                                            NetworkClient.downloadSectorCache { res ->
                                                res.fold(
                                                    onSuccess = { content ->
                                                        saveLocalFile(context, "sector_cache.json", content)
                                                        onSyncComplete()
                                                    },
                                                    onFailure = { err ->
                                                        onSyncComplete()
                                                    }
                                                )
                                            }
                                            NetworkClient.downloadEnvPolicies { res ->
                                                res.fold(
                                                    onSuccess = { content ->
                                                        try {
                                                            val gson = com.google.gson.Gson()
                                                            val map = gson.fromJson(content, Map::class.java)
                                                            val policies = map["policies"] as? String ?: ""
                                                            val prefs = context.getSharedPreferences("open_invest_prefs", Context.MODE_PRIVATE)
                                                            prefs.edit().putString("invest_a_share_sector_exit_policies", policies).apply()
                                                        } catch (e: Exception) {
                                                            Log.e("Settings", "Failed to parse env policies", e)
                                                        }
                                                        onSyncComplete()
                                                    },
                                                    onFailure = { err ->
                                                        onSyncComplete()
                                                    }
                                                )
                                            }
                                            NetworkClient.downloadAccountLedger { res ->
                                                res.fold(
                                                    onSuccess = { bytes ->
                                                        saveLocalBinaryFile(context, "account_ledger.sqlite", bytes)
                                                        onSyncComplete()
                                                    },
                                                    onFailure = { err ->
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

                    Text("账户资金修正", fontSize = 13.sp, fontWeight = FontWeight.Bold, color = IndigoPrimary)
                    Spacer(modifier = Modifier.height(6.dp))
                    OutlinedTextField(
                        value = cashStr,
                        onValueChange = { cashStr = it },
                        modifier = Modifier.fillMaxWidth(),
                        label = { Text("可用现金 (CNY)") },
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
                        value = t2CashStr,
                        onValueChange = { t2CashStr = it },
                        modifier = Modifier.fillMaxWidth(),
                        label = { Text("T+2 待交收资金 (CNY)") },
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
                    var savingCash by remember { mutableStateOf(false) }
                    Button(
                        onClick = {
                            val cashVal = cashStr.toDoubleOrNull()
                            val t2Val = t2CashStr.toDoubleOrNull()
                            if (cashVal == null || t2Val == null) {
                                Toast.makeText(context, "请输入有效的数字金额", Toast.LENGTH_SHORT).show()
                                return@Button
                            }
                            savingCash = true
                            if (NetworkClient.getBaseUrl().isNotEmpty()) {
                                NetworkClient.correctCash(cashVal, t2Val) { res ->
                                    savingCash = false
                                    res.fold(
                                        onSuccess = {
                                            Toast.makeText(context, "账户资金已修正并同步到服务器！", Toast.LENGTH_SHORT).show()
                                            onRefresh()
                                        },
                                        onFailure = { err ->
                                            Toast.makeText(context, "同步服务器失败: ${err.message}", Toast.LENGTH_LONG).show()
                                        }
                                    )
                                }
                            } else {
                                // Local offline fallback
                                val configStr = readLocalFile(context, "market_monitor_config.json")
                                if (configStr != null) {
                                    try {
                                        val json = org.json.JSONObject(configStr)
                                        json.put("cash", cashVal)
                                        json.put("t2_pending_cash", t2Val)
                                        val newContent = json.toString(2)
                                        saveLocalFile(context, "market_monitor_config.json", newContent)
                                        onImportConfig(newContent)
                                        savingCash = false
                                        Toast.makeText(context, "本地账户资金修正成功！", Toast.LENGTH_SHORT).show()
                                        onRefresh()
                                    } catch (e: Exception) {
                                        savingCash = false
                                        Toast.makeText(context, "更新本地资金配置失败: ${e.message}", Toast.LENGTH_LONG).show()
                                    }
                                } else {
                                    savingCash = false
                                    Toast.makeText(context, "未找到本地配置文件，请先从云端同步", Toast.LENGTH_LONG).show()
                                }
                            }
                        },
                        enabled = !savingCash,
                        colors = ButtonDefaults.buttonColors(containerColor = IndigoPrimary),
                        shape = RoundedCornerShape(10.dp),
                        modifier = Modifier.fillMaxWidth()
                    ) {
                        Text(if (savingCash) "正在更新..." else "确认修正账户资金", color = Color.White, fontWeight = FontWeight.Bold)
                    }

                    Spacer(modifier = Modifier.height(14.dp))
                    HorizontalDivider(color = SlateBorder, thickness = 1.dp)
                    Spacer(modifier = Modifier.height(14.dp))

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
                        colors = ButtonDefaults.buttonColors(containerColor = Color(0xFFEF4444)),
                        shape = RoundedCornerShape(10.dp),
                        modifier = Modifier.fillMaxWidth()
                    ) {
                        Text(if (clearingCache) "正在清理缓存..." else "清理服务器缓存", color = Color.White, fontWeight = FontWeight.Bold)
                    }
                }

                Spacer(modifier = Modifier.height(16.dp))

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
                                .putString("trading_mode", tradingMode)
                                .putBoolean("auto_refresh_enabled", autoRefresh)
                                .putInt("auto_refresh_interval", intervalVal)
                                .putBoolean("auto_refresh_news_enabled", refreshNews)
                                .putBoolean("auto_refresh_selection_enabled", refreshSelection)
                                .putBoolean("auto_refresh_prices_enabled", refreshPrices)
                                .putBoolean("notifications_enabled", notificationsEnabled)
                                .putBoolean("dynamic_island_enabled", dynamicIslandEnabled)
                                .putBoolean("committee_analysis_enabled", committeeAnalysisEnabled)
                                .putInt("cache_retention_limit_months", limitMonths)
                                .putInt("remote_crawler_interval_minutes", remoteFreq)
                                .putBoolean("remote_target_refresh_enabled", remoteTargetRefresh)
                                .putBoolean("remote_news_refresh_enabled", remoteNewsRefresh)
                                .apply()

                            // Update market_monitor_config.json
                            val configStr = readLocalFile(context, "market_monitor_config.json")
                            if (configStr != null) {
                                try {
                                    val json = org.json.JSONObject(configStr)
                                    val modeObj = org.json.JSONObject().apply {
                                        put("mode", tradingMode)
                                        put("label", when(tradingMode) {
                                            "active_profit" -> "主动盈利"
                                            "cash_recovery" -> "现金回收"
                                            "risk_off" -> "主动避险"
                                            else -> "主动盈利"
                                        })
                                    }
                                    json.put("trading_mode", modeObj)
                                    saveLocalFile(context, "market_monitor_config.json", json.toString(2))
                                } catch (e: java.lang.Exception) {
                                    android.util.Log.e("SettingsDialog", "Failed to update config file with trading mode", e)
                                }
                            }

                            triggerRefreshService(context)

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
