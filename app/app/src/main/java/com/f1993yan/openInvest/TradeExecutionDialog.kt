package com.f1993yan.openInvest

import android.widget.Toast
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.window.Dialog
import com.f1993yan.openInvest.network.HoldingRow
import com.f1993yan.openInvest.network.NetworkClient
import com.f1993yan.openInvest.ui.theme.TextSecondary

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
