package com.f1993yan.openInvest

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.window.Dialog
import com.f1993yan.openInvest.network.NetworkClient
import com.f1993yan.openInvest.network.SelectionStock

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
