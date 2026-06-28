package com.f1993yan.openInvest

import android.content.Context
import android.content.Intent
import android.graphics.PixelFormat
import android.net.Uri
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.util.Log
import android.util.TypedValue
import android.view.Gravity
import android.view.View
import android.view.WindowManager

object DynamicIslandManager {
    private const val TAG = "DynamicIslandManager"
    private var windowManager: WindowManager? = null
    private var islandView: DynamicIslandView? = null
    private var isVisible = false
    private var context: Context? = null
    private var windowParams: WindowManager.LayoutParams? = null

    // State cache
    private var isAppInForeground = true
    private var currentType: IslandType = IslandType.MONITORING
    private var currentTitle = ""
    private var currentText = ""
    private val currentItems = mutableListOf<String>()

    // Queued triggers to persist across transitions
    private val activeTriggers = mutableListOf<String>()
    private val activeStockNames = mutableListOf<String>()

    fun init(ctx: Context) {
        this.context = ctx.applicationContext
        this.windowManager = ctx.applicationContext.getSystemService(Context.WINDOW_SERVICE) as WindowManager
        Log.d(TAG, "Initialized DynamicIslandManager with custom window overlay")
    }

    fun onAppForeground(ctx: Context) {
        isAppInForeground = true
        Log.d(TAG, "App came to foreground - hiding overlay")
        hide()
    }

    fun onAppBackground(ctx: Context) {
        isAppInForeground = false
        Log.d(TAG, "App went to background - restoring overlay")
        if (AutoRefreshService.isRunning) {
            showOverlay(ctx)
        }
    }

    fun showMonitoring(ctx: Context) {
        if (currentType == IslandType.ANALYZING) return // Don't interrupt analysis progress

        currentType = IslandType.MONITORING
        currentTitle = "后台策略监控中"
        currentText = "自动更新与评估已激活"
        currentItems.clear()
        currentItems.add("• 策略匹配: 开启自动信号捕捉")
        currentItems.add("• 数据流速: 每 30s 评估 A 股持仓")
        currentItems.add("• 系统核心: Chaquopy 引擎正常工作")

        if (!isAppInForeground) {
            showOverlay(ctx)
        }
    }

    fun startAnalysis(ctx: Context, symbol: String, total: Int) {
        currentType = IslandType.ANALYZING
        currentTitle = "评估 $symbol 中"
        currentText = "分析进度: 0 / $total"
        currentItems.clear()
        currentItems.add("• 正在启动多智能体评估...$symbol")

        showOverlay(ctx)
    }

    fun updateAnalysisProgress(ctx: Context, symbol: String, completed: Int, total: Int) {
        currentType = IslandType.ANALYZING
        currentTitle = if (completed < total) "评估 $symbol 中" else "分析评估已完成"
        currentText = "分析进度: $completed / $total"

        currentItems.clear()
        if (completed < total) {
            currentItems.add("• 正在分析标的: $symbol")
            currentItems.add("• 当前进度: 已分析 $completed 项, 共计 $total 项")
        } else {
            currentItems.add("• 委员会本轮分析评估全部完成")
            currentItems.add("• 结果已保存至 resolved_snapshot.json")
        }

        showOverlay(ctx)
    }

    fun finishAnalysis() {
        if (currentType == IslandType.ANALYZING) {
            if (activeTriggers.isNotEmpty()) {
                currentType = IslandType.TRIGGER
                currentTitle = "触发建议 (${activeTriggers.size}笔)"
                currentText = activeTriggers.joinToString("; ").replace("• ", "")
                currentItems.clear()
                currentItems.addAll(activeTriggers)
                context?.let { showOverlay(it) }
            } else {
                if (!isAppInForeground && AutoRefreshService.isRunning) {
                    context?.let { showMonitoring(it) }
                } else {
                    hide()
                }
            }
        }
    }

    fun addTrigger(ctx: Context, title: String, content: String) {
        val cleanTitle = title.replace("交易信号触发: ", "")

        val matches = "([买卖])\\s*([^\\s]+)\\s*(\\d+)\\s*手".toRegex().find(content)
        val shortTitle = if (matches != null) {
            val side = matches.groupValues[1]
            val count = matches.groupValues[3]
            "$cleanTitle: ${side}出 ${count}手"
        } else {
            val sideText = if (content.contains("买") || content.contains("BUY")) "买入" else "卖出"
            val countText = "10"
            "$cleanTitle: 建议${sideText} ${countText}手"
        }

        val item = "• $cleanTitle: $content"

        synchronized(activeTriggers) {
            if (!activeTriggers.contains(item)) {
                activeTriggers.add(item)
            }
        }

        synchronized(activeStockNames) {
            if (!activeStockNames.contains(cleanTitle)) {
                activeStockNames.add(cleanTitle)
            }
        }

        currentType = IslandType.TRIGGER
        currentTitle = activeStockNames.joinToString(", ")
        currentText = activeTriggers.joinToString("; ").replace("• ", "")
        currentItems.clear()
        currentItems.addAll(activeTriggers)

        showOverlay(ctx)
    }

    fun clearTriggers() {
        synchronized(activeTriggers) {
            activeTriggers.clear()
        }
        synchronized(activeStockNames) {
            activeStockNames.clear()
        }
        if (!isAppInForeground && AutoRefreshService.isRunning) {
            context?.let { showMonitoring(it) }
        } else {
            hide()
        }
    }

    private fun updateWindowSize(widthPx: Int, heightPx: Int) {
        Handler(Looper.getMainLooper()).post {
            try {
                if (isVisible && islandView != null && windowParams != null && windowManager != null) {
                    windowParams?.width = widthPx
                    windowParams?.height = heightPx
                    windowManager?.updateViewLayout(islandView, windowParams)
                }
            } catch (e: Exception) {
                Log.e(TAG, "Error updating window size", e)
            }
        }
    }

    private fun showOverlay(ctx: Context) {
        if (isAppInForeground) {
            Log.d(TAG, "App is in foreground - ignoring showOverlay")
            return
        }
        val prefs = ctx.getSharedPreferences("open_invest_prefs", Context.MODE_PRIVATE)
        val islandEnabled = prefs.getBoolean("dynamic_island_enabled", true)
        if (!islandEnabled) return

        if (!Settings.canDrawOverlays(ctx)) {
            Log.w(TAG, "Cannot draw overlays: Permission not granted")
            return
        }

        Handler(Looper.getMainLooper()).post {
            try {
                if (windowManager == null) {
                    windowManager = ctx.getSystemService(Context.WINDOW_SERVICE) as WindowManager
                }

                if (islandView == null) {
                    islandView = DynamicIslandView(ctx).apply {
                        setOnOpenAppClickListener {
                            val launchIntent = Intent(ctx, MainActivity::class.java).apply {
                                flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_REORDER_TO_FRONT
                            }
                            ctx.startActivity(launchIntent)
                            collapse()
                        }
                        setOnSizeChangedListener { w, h ->
                            updateWindowSize(w, h)
                        }
                    }
                }

                islandView?.setData(currentType, currentTitle, currentText, currentItems)

                if (!isVisible && islandView != null) {
                    val initW = if (islandView?.isExpanded == true) dpToPx(ctx, 320) else dpToPx(ctx, 240)
                    val initH = if (islandView?.isExpanded == true) dpToPx(ctx, 176) else dpToPx(ctx, 38)

                    windowParams = WindowManager.LayoutParams(
                        initW,
                        initH,
                        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                            WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
                        } else {
                            @Suppress("DEPRECATION")
                            WindowManager.LayoutParams.TYPE_PHONE
                        },
                        WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or
                                WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN or
                                WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS or
                                WindowManager.LayoutParams.FLAG_WATCH_OUTSIDE_TOUCH,
                        PixelFormat.TRANSLUCENT
                    ).apply {
                        gravity = Gravity.TOP or Gravity.CENTER_HORIZONTAL
                        y = dpToPx(ctx, 42) // Placed perfectly below the system status bar, preventing overlaps with clock/icons
                    }
                    if (islandView?.parent != null) {
                        islandView?.animate()?.cancel()
                        islandView?.scaleX = 1.0f
                        islandView?.scaleY = 1.0f
                    } else {
                        islandView?.scaleX = 0f
                        islandView?.scaleY = 0f
                        islandView?.post {
                            islandView?.pivotX = (islandView?.width ?: 0) / 2f
                            islandView?.pivotY = -dpToPx(ctx, 27).toFloat() // Align pivot vertical center with camera hole at y=15dp
                        }
                        windowManager?.addView(islandView, windowParams)
                        islandView?.animate()
                            ?.scaleX(1.0f)
                            ?.scaleY(1.0f)
                            ?.setDuration(500)
                            ?.setInterpolator(android.view.animation.OvershootInterpolator(1.8f))
                            ?.start()
                    }
                    isVisible = true
                    Log.d(TAG, "Displayed Dynamic Island window overlay with jelly bounce animation from camera hole")
                }
            } catch (e: Exception) {
                Log.e(TAG, "Error displaying Dynamic Island window", e)
            }
        }
    }

    fun hide() {
        Handler(Looper.getMainLooper()).post {
            try {
                if (isVisible && islandView != null && windowManager != null) {
                    isVisible = false
                    islandView?.post {
                        islandView?.pivotX = (islandView?.width ?: 0) / 2f
                        val viewContext = islandView?.context
                        if (viewContext != null) {
                            islandView?.pivotY = -dpToPx(viewContext, 27).toFloat()
                        }
                    }
                    islandView?.animate()
                        ?.scaleX(0f)
                        ?.scaleY(0f)
                        ?.setDuration(300)
                        ?.setInterpolator(android.view.animation.AnticipateInterpolator(1.4f))
                        ?.withEndAction {
                            try {
                                if (!isVisible && islandView?.parent != null) {
                                    windowManager?.removeView(islandView)
                                    Log.d(TAG, "Removed Dynamic Island window overlay after exit animation")
                                }
                            } catch (e: Exception) {
                                // Ignore
                            }
                        }
                        ?.start()
                }
            } catch (e: Exception) {
                Log.e(TAG, "Error removing Dynamic Island window", e)
            }
        }
    }

    private fun dpToPx(ctx: Context, dp: Int): Int {
        return TypedValue.applyDimension(
            TypedValue.COMPLEX_UNIT_DIP,
            dp.toFloat(),
            ctx.resources.displayMetrics
        ).toInt()
    }
}

enum class IslandType {
    MONITORING,
    ANALYZING,
    TRIGGER
}
