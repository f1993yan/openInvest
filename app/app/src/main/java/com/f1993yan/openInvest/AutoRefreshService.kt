package com.f1993yan.openInvest

import android.app.Service
import android.content.Intent
import android.os.Binder
import android.os.IBinder
import android.util.Log
import com.f1993yan.openInvest.network.*
import kotlinx.coroutines.*

class AutoRefreshService : Service() {
    companion object {
        private const val TAG = "AutoRefreshService"
        var isRunning = false
            private set

        private val listeners = mutableListOf<RefreshListener>()

        fun registerListener(listener: RefreshListener) {
            synchronized(listeners) {
                listeners.add(listener)
            }
        }

        fun unregisterListener(listener: RefreshListener) {
            synchronized(listeners) {
                listeners.remove(listener)
            }
        }

        private fun notifySnapshotUpdated(snap: SnapshotResponse) {
            synchronized(listeners) {
                listeners.forEach { it.onSnapshotUpdated(snap) }
            }
        }

        private fun notifySelectionUpdated(sel: DailySelectionResponse) {
            synchronized(listeners) {
                listeners.forEach { it.onSelectionUpdated(sel) }
            }
        }

        private fun notifyNewsUpdated(news: WeekendNewsResponse) {
            synchronized(listeners) {
                listeners.forEach { it.onNewsUpdated(news) }
            }
        }
    }

    private val binder = LocalBinder()
    private val serviceJob = SupervisorJob()
    private val serviceScope = CoroutineScope(Dispatchers.Main + serviceJob)
    private var refreshJob: Job? = null

    inner class LocalBinder : Binder() {
        fun getService(): AutoRefreshService = this@AutoRefreshService
    }

    interface RefreshListener {
        fun onSnapshotUpdated(snap: SnapshotResponse)
        fun onSelectionUpdated(sel: DailySelectionResponse)
        fun onNewsUpdated(news: WeekendNewsResponse)
    }

    override fun onCreate() {
        super.onCreate()
        Log.d(TAG, "Service onCreate")
        isRunning = true
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        Log.d(TAG, "Service onStartCommand")
        val interval = intent?.getIntExtra("interval", 30) ?: 30
        val refreshNews = intent?.getBooleanExtra("refresh_news", true) ?: true
        val refreshSelection = intent?.getBooleanExtra("refresh_selection", true) ?: true
        val refreshPrices = intent?.getBooleanExtra("refresh_prices", true) ?: true
        startRefreshing(interval, refreshNews, refreshSelection, refreshPrices)
        return START_NOT_STICKY
    }

    override fun onBind(intent: Intent?): IBinder {
        Log.d(TAG, "Service onBind")
        return binder
    }

    override fun onDestroy() {
        super.onDestroy()
        Log.d(TAG, "Service onDestroy - stopping refreshes")
        serviceJob.cancel()
        isRunning = false
    }

    private fun startRefreshing(
        intervalSeconds: Int,
        refreshNews: Boolean,
        refreshSelection: Boolean,
        refreshPrices: Boolean
    ) {
        refreshJob?.cancel()
        if (intervalSeconds <= 0) {
            Log.d(TAG, "Interval <= 0, skipping refresh loop")
            return
        }

        refreshJob = serviceScope.launch {
            Log.d(TAG, "Starting refresh loop: interval=$intervalSeconds, news=$refreshNews, selection=$refreshSelection, prices=$refreshPrices")
            while (isActive) {
                delay(intervalSeconds * 1000L)
                if (!isActive) break

                Log.d(TAG, "Service executing background auto-refresh cycle...")
                
                if (refreshPrices) {
                    NetworkClient.fetchSnapshot { snapshotResult ->
                        snapshotResult.getOrNull()?.let {
                            Log.d(TAG, "Service background fetchSnapshot success")
                            notifySnapshotUpdated(it)
                        }
                    }
                }
                
                if (refreshSelection) {
                    NetworkClient.fetchSelection { selectionResult ->
                        selectionResult.getOrNull()?.let {
                            Log.d(TAG, "Service background fetchSelection success")
                            notifySelectionUpdated(it)
                        }
                    }
                }
                
                if (refreshNews) {
                    NetworkClient.fetchNews { newsResult ->
                        newsResult.getOrNull()?.let {
                            Log.d(TAG, "Service background fetchNews success")
                            notifyNewsUpdated(it)
                        }
                    }
                }
            }
        }
    }
}
