package com.f1993yan.openInvest

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.*
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.f1993yan.openInvest.network.HoldingRow
import com.f1993yan.openInvest.ui.theme.*

@Composable
fun BehavioralFactorTargetBar(rows: List<HoldingRow>, onSelect: (HoldingRow) -> Unit) {
    val targets = rows
        .filter { it.behavioral_factor?.selected == true }
        .sortedByDescending { it.behavioral_factor?.target_weight_pct ?: 0.0 }
        .take(4)
    if (targets.isEmpty()) return

    Column(
        modifier = Modifier
            .fillMaxWidth()
            .background(Color(0xFFF8FAFC))
            .padding(vertical = 8.dp)
    ) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = 2.dp),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically
        ) {
            Text(
                text = "行为因子目标前四",
                fontSize = 12.sp,
                fontWeight = FontWeight.Bold,
                color = TextPrimary
            )
            Text(
                text = "模型目标，不是持仓",
                fontSize = 10.sp,
                color = TextSecondary
            )
        }
        Spacer(modifier = Modifier.height(6.dp))
        LazyRow(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
            items(targets, key = { it.symbol }) { row ->
                val targetWeight = row.behavioral_factor?.target_weight_pct ?: 0.0
                Row(
                    modifier = Modifier
                        .clip(RoundedCornerShape(6.dp))
                        .background(Color(0xFFE7F6ED))
                        .clickable { onSelect(row) }
                        .padding(horizontal = 9.dp, vertical = 5.dp),
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Text(
                        text = row.name,
                        fontSize = 11.sp,
                        fontWeight = FontWeight.Bold,
                        color = Color(0xFF067647)
                    )
                    if (targetWeight > 0.0) {
                        Spacer(modifier = Modifier.width(5.dp))
                        Text(
                            text = String.format("%.1f%%", targetWeight),
                            fontSize = 10.sp,
                            color = Color(0xFF067647)
                        )
                    }
                }
            }
        }
    }
}

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
            val technical = row.technical
            val hasBuyPoints = buyCriteria?.pullback_price != null || buyCriteria?.breakout_price != null || buyCriteria?.reentry_price != null
            val hasExitPoints = exitPoints?.stop_loss_price != null || exitPoints?.take_profit_price != null
            val hasTechnical = technical?.ma20 != null || technical?.ma120 != null || technical?.atr_pct != null

            val behavioralFactor = row.behavioral_factor
            if (behavioralFactor?.selected == true) {
                val targetWeight = behavioralFactor.target_weight_pct ?: 0.0
                Spacer(modifier = Modifier.height(7.dp))
                Text(
                    text = if (targetWeight > 0.0) {
                        "行为因子目标前四 · 目标仓位 ${String.format("%.1f", targetWeight)}%"
                    } else {
                        "行为因子目标前四"
                    },
                    fontSize = 10.sp,
                    fontWeight = FontWeight.Bold,
                    color = Color(0xFF067647)
                )
            }
 
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
            } else if (hasBuyPoints || hasExitPoints || hasTechnical) {
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
                    if (hasTechnical) {
                        val techText = buildString {
                            technical?.ma20?.let { append("MA20:¥${String.format("%.2f", it)} ") }
                            technical?.ma120?.let { append("MA120:¥${String.format("%.2f", it)} ") }
                            technical?.atr_pct?.let { append("ATR:${String.format("%.2f", it)}% ") }
                        }.trim()
                        Text(
                            text = "技术: $techText",
                            fontSize = 11.sp,
                            color = TextSecondary
                        )
                    }
                }
            }
        }
    }
}
