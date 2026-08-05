package com.f1993yan.openInvest

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Close
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
import androidx.compose.ui.window.DialogProperties
import com.f1993yan.openInvest.network.NewsLeader
import com.f1993yan.openInvest.network.SnapshotResponse
import com.f1993yan.openInvest.network.WeekendNewsResponse

@Composable
@Suppress("UNUSED_PARAMETER")
fun NewsDialog(
    news: WeekendNewsResponse?,
    snapshot: SnapshotResponse?,
    onClose: () -> Unit,
    onViewDetails: (NewsLeader) -> Unit
) {
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
                        items(news.cards ?: emptyList()) { card ->
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
                                                                    onViewDetails(leader)
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
}
