package com.f1993yan.openInvest

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.window.Dialog
import com.f1993yan.openInvest.network.HoldingRow
import com.f1993yan.openInvest.network.NetworkClient

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
