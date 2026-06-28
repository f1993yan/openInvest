package com.f1993yan.openInvest

import android.Manifest
import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Context
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.util.Log
import android.content.Intent
import androidx.compose.animation.core.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.graphicsLayer
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat

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

fun getDisplayUpdateTime(roundTime: String?, generatedAt: String?): String {
    if (roundTime != null && roundTime != "config") {
        return roundTime
    }
    if (!generatedAt.isNullOrEmpty()) {
        try {
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

private fun parseSymbolFromText(text: String): String? {
    val codeRegex = "\\((\\d{6})\\)".toRegex()
    val matchResult = codeRegex.find(text) ?: return null
    val code = matchResult.groupValues[1]
    return when {
        code.startsWith("6") -> "$code.SH"
        code.startsWith("0") || code.startsWith("3") -> "$code.SZ"
        code.startsWith("4") || code.startsWith("8") -> "$code.BJ"
        else -> "$code.SZ"
    }
}

fun showNotification(context: Context, title: String, content: String) {
    val prefs = context.getSharedPreferences("open_invest_prefs", Context.MODE_PRIVATE)
    val notificationsEnabled = prefs.getBoolean("notifications_enabled", true)
    if (!notificationsEnabled) return

    val isTradeSignal = title.contains("触发") || title.contains("执行") || title.contains("信号") || title.contains("交易") ||
                        content.contains("触发") || content.contains("执行") || content.contains("交易") ||
                        content.contains("买") || content.contains("卖") ||
                        content.contains("BUY") || content.contains("SELL")

    val islandEnabled = prefs.getBoolean("dynamic_island_enabled", true)
    if (islandEnabled && android.provider.Settings.canDrawOverlays(context)) {
        DynamicIslandManager.addTrigger(context, title, content)
        if (!isTradeSignal) {
            // Non-critical background updates are handled entirely by the floating island overlay
            return
        }
    }

    val channelId = if (isTradeSignal) "open_invest_critical" else "open_invest_regular"
    val channelName = if (isTradeSignal) "交易执行信号" else "日常行情更新"
    val channelImportance = if (isTradeSignal) NotificationManager.IMPORTANCE_HIGH else NotificationManager.IMPORTANCE_LOW
    val channelVisibility = if (isTradeSignal) android.app.Notification.VISIBILITY_PUBLIC else android.app.Notification.VISIBILITY_SECRET
    
    val notificationManager = context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager

    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
        val channel = NotificationChannel(
            channelId,
            channelName,
            channelImportance
        ).apply {
            description = if (isTradeSignal) "触发的交易执行提醒信号" else "常规行情后台更新"
            lockscreenVisibility = channelVisibility
        }
        notificationManager.createNotificationChannel(channel)
    }

    // Parse symbol, verdict from content to route to the interactive trade flow
    val symbol = parseSymbolFromText(content)
    val verdict = if (content.contains("买") || content.contains("BUY")) "BUY" else "SELL"

    val intent = Intent(context, MainActivity::class.java).apply {
        action = "com.f1993yan.openInvest.ACTION_EXECUTE_TRADE"
        putExtra("symbol", symbol)
        putExtra("verdict", verdict)
        addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP)
    }

    val pendingIntent = android.app.PendingIntent.getActivity(
        context,
        System.currentTimeMillis().toInt(),
        intent,
        android.app.PendingIntent.FLAG_UPDATE_CURRENT or android.app.PendingIntent.FLAG_IMMUTABLE
    )

    val builder = NotificationCompat.Builder(context, channelId)
        .setSmallIcon(R.drawable.deepseek_whale)
        .setContentTitle(title)
        .setContentText(content)
        .setPriority(if (isTradeSignal) NotificationCompat.PRIORITY_HIGH else NotificationCompat.PRIORITY_LOW)
        .setVisibility(if (isTradeSignal) NotificationCompat.VISIBILITY_PUBLIC else NotificationCompat.VISIBILITY_SECRET)
        .setContentIntent(pendingIntent)
        .setAutoCancel(true)
        .setCategory(if (isTradeSignal) NotificationCompat.CATEGORY_ALARM else NotificationCompat.CATEGORY_STATUS)

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
            Log.d("MainActivity", "Started AutoRefreshService with interval=$interval, news=$refreshNews, selection=$refreshSelection, prices=$refreshPrices")
        } catch (e: Exception) {
            Log.e("MainActivity", "Failed to start AutoRefreshService", e)
        }
    } else {
        try {
            context.stopService(intent)
            Log.d("MainActivity", "Stopped AutoRefreshService")
        } catch (e: Exception) {
            Log.e("MainActivity", "Failed to stop AutoRefreshService", e)
        }
    }
}
