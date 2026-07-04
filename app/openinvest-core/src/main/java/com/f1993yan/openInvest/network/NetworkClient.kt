package com.f1993yan.openInvest.network

import android.os.Handler
import android.os.Looper
import android.util.Log
import com.google.gson.Gson
import com.google.gson.reflect.TypeToken
import okhttp3.*
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.toRequestBody
import java.io.IOException
import java.util.concurrent.TimeUnit

object NetworkClient {
    private const val TAG = "OpenInvestNet"

    private val client = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .writeTimeout(30, TimeUnit.SECONDS)   // Upload timeout for large config payloads
        .readTimeout(10, TimeUnit.MINUTES)     // Long timeout for SSE stream
        .build()

    private val gson = Gson()
    private val JSON_MEDIA_TYPE = "application/json; charset=utf-8".toMediaType()
    private val mainHandler = Handler(Looper.getMainLooper())

    private var baseUrl: String = "http://10.0.2.2:8765"

    fun setBaseUrl(url: String) {
        var cleanUrl = url.trim()
        if (cleanUrl.endsWith("/")) {
            cleanUrl = cleanUrl.substring(0, cleanUrl.length - 1)
        }
        baseUrl = cleanUrl
        Log.d(TAG, "setBaseUrl: baseUrl configured to $baseUrl")
    }

    fun getBaseUrl(): String = baseUrl

    private fun <T> runOnMain(callback: (Result<T>) -> Unit, result: Result<T>) {
        mainHandler.post { callback(result) }
    }

    fun testConnection(onResult: (Result<Boolean>) -> Unit) {
        val request = Request.Builder()
            .url("$baseUrl/api/health")
            .get()
            .build()

        Log.d(TAG, "testConnection: GET $baseUrl/api/health")
        client.newCall(request).enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                Log.e(TAG, "testConnection failed for $baseUrl/api/health", e)
                runOnMain(onResult, Result.failure(e))
            }

            override fun onResponse(call: Call, response: Response) {
                if (response.isSuccessful) {
                    Log.d(TAG, "testConnection success: HTTP ${response.code}")
                    runOnMain(onResult, Result.success(true))
                } else {
                    Log.w(TAG, "testConnection failure: HTTP ${response.code}")
                    runOnMain(onResult, Result.failure(IOException("Server error: ${response.code}")))
                }
                response.close()
            }
        })
    }

    fun fetchSnapshot(onResult: (Result<SnapshotResponse>) -> Unit) {
        val request = Request.Builder()
            .url("$baseUrl/api/monitor/snapshot")
            .get()
            .build()

        Log.d(TAG, "fetchSnapshot: GET $baseUrl/api/monitor/snapshot")
        client.newCall(request).enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                Log.e(TAG, "fetchSnapshot failed", e)
                runOnMain(onResult, Result.failure(e))
            }

            override fun onResponse(call: Call, response: Response) {
                try {
                    if (response.isSuccessful) {
                        val bodyString = response.body?.string() ?: ""
                        Log.d(TAG, "fetchSnapshot success: length = ${bodyString.length}")
                        val snapshot = gson.fromJson(bodyString, SnapshotResponse::class.java)
                        
                        // Client-side fix: calculate/infer units if missing/0.0 on server response
                        val processedRows = snapshot.rows?.map { row ->
                            if (row.units <= 0.0 && kotlin.math.abs(row.position_pct) > 0.0 && kotlin.math.abs(row.cost) > 0.0 && snapshot.total_assets_cny > 0.0) {
                                val calcUnits = (snapshot.total_assets_cny * kotlin.math.abs(row.position_pct) / 100.0) / kotlin.math.abs(row.cost)
                                row.copy(units = calcUnits)
                            } else {
                                row
                            }
                        }
                        val processedSnapshot = snapshot.copy(rows = processedRows)
                        runOnMain(onResult, Result.success(processedSnapshot))
                    } else {
                        Log.w(TAG, "fetchSnapshot failure: HTTP ${response.code}")
                        runOnMain(onResult, Result.failure(IOException("HTTP error: ${response.code}")))
                    }
                } catch (e: Exception) {
                    Log.e(TAG, "fetchSnapshot exception during parsing", e)
                    runOnMain(onResult, Result.failure(e))
                } finally {
                    response.close()
                }
            }
        })
    }

    fun fetchSelection(onResult: (Result<DailySelectionResponse>) -> Unit) {
        val request = Request.Builder()
            .url("$baseUrl/api/monitor/selection")
            .get()
            .build()

        Log.d(TAG, "fetchSelection: GET $baseUrl/api/monitor/selection")
        client.newCall(request).enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                Log.e(TAG, "fetchSelection failed", e)
                runOnMain(onResult, Result.failure(e))
            }

            override fun onResponse(call: Call, response: Response) {
                try {
                    if (response.isSuccessful) {
                        val bodyString = response.body?.string() ?: ""
                        Log.d(TAG, "fetchSelection success: length = ${bodyString.length}")
                        val selection = gson.fromJson(bodyString, DailySelectionResponse::class.java)
                        runOnMain(onResult, Result.success(selection))
                    } else {
                        Log.w(TAG, "fetchSelection failure: HTTP ${response.code}")
                        runOnMain(onResult, Result.failure(IOException("HTTP error: ${response.code}")))
                    }
                } catch (e: Exception) {
                    Log.e(TAG, "fetchSelection exception during parsing", e)
                    runOnMain(onResult, Result.failure(e))
                } finally {
                    response.close()
                }
            }
        })
    }

    fun fetchNews(onResult: (Result<WeekendNewsResponse>) -> Unit) {
        val request = Request.Builder()
            .url("$baseUrl/api/monitor/news")
            .get()
            .build()

        Log.d(TAG, "fetchNews: GET $baseUrl/api/monitor/news")
        client.newCall(request).enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                Log.e(TAG, "fetchNews failed", e)
                runOnMain(onResult, Result.failure(e))
            }

            override fun onResponse(call: Call, response: Response) {
                try {
                    if (response.isSuccessful) {
                        val bodyString = response.body?.string() ?: ""
                        Log.d(TAG, "fetchNews success: length = ${bodyString.length}")
                        val news = gson.fromJson(bodyString, WeekendNewsResponse::class.java)
                        runOnMain(onResult, Result.success(news))
                    } else {
                        Log.w(TAG, "fetchNews failure: HTTP ${response.code}")
                        runOnMain(onResult, Result.failure(IOException("HTTP error: ${response.code}")))
                    }
                } catch (e: Exception) {
                    Log.e(TAG, "fetchNews exception during parsing", e)
                    runOnMain(onResult, Result.failure(e))
                } finally {
                    response.close()
                }
            }
        })
    }

    fun addToWatchlist(symbol: String, name: String, sector: String, onResult: (Result<String>) -> Unit) {
        val bodyMap = mapOf(
            "symbol" to symbol,
            "kind" to "other",
            "units" to 0.0,
            "unit_label" to "股",
            "avg_cost" to 0.0,
            "cost_currency" to "CNY",
            "display_name" to name,
            "is_tracking_only" to true,
            "channel" to sector
        )
        val requestBody = gson.toJson(bodyMap).toRequestBody(JSON_MEDIA_TYPE)
        val request = Request.Builder()
            .url("$baseUrl/api/holdings")
            .post(requestBody)
            .build()

        Log.d(TAG, "addToWatchlist: POST $baseUrl/api/holdings for $symbol ($name)")
        client.newCall(request).enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                Log.e(TAG, "addToWatchlist failed", e)
                runOnMain(onResult, Result.failure(e))
            }

            override fun onResponse(call: Call, response: Response) {
                try {
                    if (response.isSuccessful || response.code == 409) {
                        val msg = if (response.code == 409) "已在持仓或关注列表" else "已加入关注列表: $name"
                        Log.d(TAG, "addToWatchlist response: HTTP ${response.code} -> $msg")
                        runOnMain(onResult, Result.success(msg))
                    } else {
                        val errorMsg = response.body?.string() ?: ""
                        Log.w(TAG, "addToWatchlist failure: HTTP ${response.code} -> $errorMsg")
                        runOnMain(onResult, Result.failure(IOException("HTTP ${response.code}: $errorMsg")))
                    }
                } catch (e: Exception) {
                    Log.e(TAG, "addToWatchlist exception", e)
                    runOnMain(onResult, Result.failure(e))
                } finally {
                    response.close()
                }
            }
        })
    }

    fun executeTrade(symbol: String, direction: String, units: Double, price: Double, note: String, onResult: (Result<String>) -> Unit) {
        val bodyMap = mapOf(
            "symbol" to symbol,
            "direction" to direction,
            "units" to units,
            "price" to price,
            "note" to note
        )
        val requestBody = gson.toJson(bodyMap).toRequestBody(JSON_MEDIA_TYPE)
        val request = Request.Builder()
            .url("$baseUrl/api/accounts/real/trades")
            .post(requestBody)
            .build()

        Log.d(TAG, "executeTrade: POST $baseUrl/api/accounts/real/trades: $direction $units shares of $symbol at $price")
        client.newCall(request).enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                Log.e(TAG, "executeTrade failed", e)
                runOnMain(onResult, Result.failure(e))
            }

            override fun onResponse(call: Call, response: Response) {
                try {
                    if (response.isSuccessful) {
                        Log.d(TAG, "executeTrade success")
                        runOnMain(onResult, Result.success("记账成功"))
                    } else {
                        val errorMsg = response.body?.string() ?: ""
                        Log.w(TAG, "executeTrade failure: HTTP ${response.code} -> $errorMsg")
                        runOnMain(onResult, Result.failure(IOException("记账失败: HTTP ${response.code}: $errorMsg")))
                    }
                } catch (e: Exception) {
                    Log.e(TAG, "executeTrade exception", e)
                    runOnMain(onResult, Result.failure(e))
                } finally {
                    response.close()
                }
            }
        })
    }

    fun deleteFromWatchlist(symbol: String, onResult: (Result<String>) -> Unit) {
        val request = Request.Builder()
            .url("$baseUrl/api/holdings/$symbol")
            .delete()
            .build()

        Log.d(TAG, "deleteFromWatchlist: DELETE $baseUrl/api/holdings/$symbol")
        client.newCall(request).enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                Log.e(TAG, "deleteFromWatchlist failed", e)
                runOnMain(onResult, Result.failure(e))
            }

            override fun onResponse(call: Call, response: Response) {
                try {
                    if (response.isSuccessful) {
                        Log.d(TAG, "deleteFromWatchlist success")
                        runOnMain(onResult, Result.success("已删除关注"))
                    } else {
                        val errorMsg = response.body?.string() ?: ""
                        Log.w(TAG, "deleteFromWatchlist failure: HTTP ${response.code} -> $errorMsg")
                        runOnMain(onResult, Result.failure(IOException("删除失败: $errorMsg")))
                    }
                } catch (e: Exception) {
                    Log.e(TAG, "deleteFromWatchlist exception", e)
                    runOnMain(onResult, Result.failure(e))
                } finally {
                    response.close()
                }
            }
        })
    }

    fun updateHolding(symbol: String, units: Double, avgCost: Double, isTrackingOnly: Boolean, onResult: (Result<String>) -> Unit) {
        val bodyMap = mapOf(
            "units" to units,
            "avg_cost" to avgCost,
            "is_tracking_only" to isTrackingOnly
        )
        val requestBody = gson.toJson(bodyMap).toRequestBody(JSON_MEDIA_TYPE)
        val request = Request.Builder()
            .url("$baseUrl/api/holdings/$symbol")
            .put(requestBody)
            .build()

        Log.d(TAG, "updateHolding: PUT $baseUrl/api/holdings/$symbol: units=$units, avgCost=$avgCost, isTrackingOnly=$isTrackingOnly")
        client.newCall(request).enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                Log.e(TAG, "updateHolding failed", e)
                runOnMain(onResult, Result.failure(e))
            }

            override fun onResponse(call: Call, response: Response) {
                try {
                    if (response.isSuccessful) {
                        Log.d(TAG, "updateHolding success")
                        runOnMain(onResult, Result.success("持仓数据更新成功"))
                    } else {
                        val errorMsg = response.body?.string() ?: ""
                        Log.w(TAG, "updateHolding failure: HTTP ${response.code} -> $errorMsg")
                        runOnMain(onResult, Result.failure(IOException("更新失败: $errorMsg")))
                    }
                } catch (e: Exception) {
                    Log.e(TAG, "updateHolding exception", e)
                    runOnMain(onResult, Result.failure(e))
                } finally {
                    response.close()
                }
            }
        })
    }

    fun runCommittee(symbol: String, onResult: (Result<CommitteeRunResponse>) -> Unit) {
        val body = CommitteeRunRequest(symbols = listOf(symbol))
        val requestBody = gson.toJson(body).toRequestBody(JSON_MEDIA_TYPE)
        val request = Request.Builder()
            .url("$baseUrl/api/committee/run")
            .post(requestBody)
            .build()

        Log.d(TAG, "runCommittee: POST $baseUrl/api/committee/run for $symbol")
        client.newCall(request).enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                Log.e(TAG, "runCommittee failed", e)
                runOnMain(onResult, Result.failure(e))
            }

            override fun onResponse(call: Call, response: Response) {
                try {
                    if (response.isSuccessful) {
                        val bodyString = response.body?.string() ?: ""
                        Log.d(TAG, "runCommittee success: $bodyString")
                        val runResp = gson.fromJson(bodyString, CommitteeRunResponse::class.java)
                        runOnMain(onResult, Result.success(runResp))
                    } else {
                        Log.w(TAG, "runCommittee failure: HTTP ${response.code}")
                        runOnMain(onResult, Result.failure(IOException("HTTP error: ${response.code}")))
                    }
                } catch (e: Exception) {
                    Log.e(TAG, "runCommittee exception during parsing", e)
                    runOnMain(onResult, Result.failure(e))
                } finally {
                    response.close()
                }
            }
        })
    }

    fun streamCommitteeLive(
        taskId: String,
        onProgress: (CommitteeStatusResponse) -> Unit,
        onDone: (CommitteeStatusResponse) -> Unit,
        onError: (String) -> Unit
    ): Call {
        val request = Request.Builder()
            .url("$baseUrl/api/committee/live/$taskId")
            .build()

        Log.d(TAG, "streamCommitteeLive: GET $baseUrl/api/committee/live/$taskId")
        val call = client.newCall(request)
        call.enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                Log.e(TAG, "streamCommitteeLive failed for $taskId", e)
                mainHandler.post { onError(e.message ?: "Network error") }
            }

            override fun onResponse(call: Call, response: Response) {
                if (!response.isSuccessful) {
                    Log.w(TAG, "streamCommitteeLive non-successful: HTTP ${response.code}")
                    mainHandler.post { onError("HTTP error: ${response.code}") }
                    response.close()
                    return
                }
                val body = response.body
                if (body == null) {
                    Log.w(TAG, "streamCommitteeLive null body")
                    mainHandler.post { onError("Empty stream body") }
                    response.close()
                    return
                }
                try {
                    Log.d(TAG, "streamCommitteeLive started reading stream")
                    val reader = body.charStream().buffered()
                    var line: String?
                    var currentEvent = ""
                    while (reader.readLine().also { line = it } != null) {
                        val trimmed = line!!.trim()
                        if (trimmed.isEmpty()) continue
                        if (trimmed.startsWith("event:")) {
                            currentEvent = trimmed.substring(6).trim()
                        } else if (trimmed.startsWith("data:")) {
                            val dataStr = trimmed.substring(5).trim()
                            val status = gson.fromJson(dataStr, CommitteeStatusResponse::class.java)
                            Log.v(TAG, "streamCommitteeLive event $currentEvent: progress = ${status.progress}, events size = ${status.events?.size}")
                            mainHandler.post {
                                when (currentEvent) {
                                    "progress" -> onProgress(status)
                                    "done" -> onDone(status)
                                    "error" -> onError(status.error ?: "Debate error")
                                    else -> onProgress(status)
                                }
                            }
                            if (currentEvent == "done" || currentEvent == "error") {
                                Log.d(TAG, "streamCommitteeLive end event received: $currentEvent")
                                break
                            }
                        }
                    }
                } catch (e: Exception) {
                    Log.e(TAG, "streamCommitteeLive exception during read/parse", e)
                    mainHandler.post { onError(e.message ?: "SSE parsing error") }
                } finally {
                    Log.d(TAG, "streamCommitteeLive stream closed")
                    response.close()
                }
            }
        })
        return call
    }

    fun clearCache(retentionMonths: Int, onResult: (Result<ConfigResponse>) -> Unit) {
        val requestBody = "".toRequestBody(JSON_MEDIA_TYPE)
        val request = Request.Builder()
            .url("$baseUrl/api/config/clear_cache?retention_months=$retentionMonths")
            .post(requestBody)
            .build()

        Log.d(TAG, "clearCache: POST $baseUrl/api/config/clear_cache?retention_months=$retentionMonths")
        client.newCall(request).enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                Log.e(TAG, "clearCache failed", e)
                runOnMain(onResult, Result.failure(e))
            }

            override fun onResponse(call: Call, response: Response) {
                try {
                    if (response.isSuccessful) {
                        val bodyString = response.body?.string() ?: ""
                        val res = gson.fromJson(bodyString, ConfigResponse::class.java)
                        Log.d(TAG, "clearCache success: $bodyString")
                        runOnMain(onResult, Result.success(res))
                    } else {
                        Log.w(TAG, "clearCache failure: HTTP ${response.code}")
                        runOnMain(onResult, Result.failure(IOException("HTTP error: ${response.code}")))
                    }
                } catch (e: Exception) {
                    Log.e(TAG, "clearCache exception during parsing", e)
                    runOnMain(onResult, Result.failure(e))
                } finally {
                    response.close()
                }
            }
        })
    }

    fun fetchRegime(symbol: String, onResult: (Result<Map<String, Any>>) -> Unit) {
        val request = Request.Builder()
            .url("$baseUrl/api/regime/$symbol")
            .build()

        Log.d(TAG, "fetchRegime: GET $baseUrl/api/regime/$symbol")
        client.newCall(request).enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                Log.e(TAG, "fetchRegime failed", e)
                runOnMain(onResult, Result.failure(e))
            }

            override fun onResponse(call: Call, response: Response) {
                try {
                    if (response.isSuccessful) {
                        val bodyString = response.body?.string() ?: ""
                        val res = gson.fromJson<Map<String, Any>>(bodyString, object : TypeToken<Map<String, Any>>() {}.type)
                        Log.d(TAG, "fetchRegime success: $bodyString")
                        runOnMain(onResult, Result.success(res))
                    } else {
                        Log.w(TAG, "fetchRegime failure: HTTP ${response.code}")
                        runOnMain(onResult, Result.failure(IOException("HTTP error: ${response.code}")))
                    }
                } catch (e: Exception) {
                    Log.e(TAG, "fetchRegime exception during parsing", e)
                    runOnMain(onResult, Result.failure(e))
                } finally {
                    response.close()
                }
            }
        })
    }

    fun getCrawlerSettings(onResult: (Result<CrawlerSettings>) -> Unit) {
        val request = Request.Builder()
            .url("$baseUrl/api/config/crawler")
            .get()
            .build()
        Log.d(TAG, "getCrawlerSettings: GET $baseUrl/api/config/crawler")
        client.newCall(request).enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                Log.e(TAG, "getCrawlerSettings failed", e)
                runOnMain(onResult, Result.failure(e))
            }
            override fun onResponse(call: Call, response: Response) {
                try {
                    if (response.isSuccessful) {
                        val bodyString = response.body?.string() ?: ""
                        Log.d(TAG, "getCrawlerSettings success: $bodyString")
                        val settings = gson.fromJson(bodyString, CrawlerSettings::class.java)
                        runOnMain(onResult, Result.success(settings))
                    } else {
                        Log.w(TAG, "getCrawlerSettings failure: HTTP ${response.code}")
                        runOnMain(onResult, Result.failure(IOException("HTTP error: ${response.code}")))
                    }
                } catch (e: Exception) {
                    Log.e(TAG, "getCrawlerSettings exception during parsing", e)
                    runOnMain(onResult, Result.failure(e))
                } finally {
                    response.close()
                }
            }
        })
    }

    fun updateCrawlerSettings(settings: CrawlerSettings, onResult: (Result<Boolean>) -> Unit) {
        val requestBody = gson.toJson(settings).toRequestBody(JSON_MEDIA_TYPE)
        val request = Request.Builder()
            .url("$baseUrl/api/config/crawler")
            .post(requestBody)
            .build()
        Log.d(TAG, "updateCrawlerSettings: POST $baseUrl/api/config/crawler: $settings")
        client.newCall(request).enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                Log.e(TAG, "updateCrawlerSettings failed", e)
                runOnMain(onResult, Result.failure(e))
            }
            override fun onResponse(call: Call, response: Response) {
                try {
                    if (response.isSuccessful) {
                        Log.d(TAG, "updateCrawlerSettings success")
                        runOnMain(onResult, Result.success(true))
                    } else {
                        val errorMsg = response.body?.string() ?: ""
                        Log.w(TAG, "updateCrawlerSettings failure: HTTP ${response.code} -> $errorMsg")
                        runOnMain(onResult, Result.failure(IOException("HTTP ${response.code}: $errorMsg")))
                    }
                } catch (e: Exception) {
                    Log.e(TAG, "updateCrawlerSettings exception", e)
                    runOnMain(onResult, Result.failure(e))
                } finally {
                    response.close()
                }
            }
        })
    }

    fun uploadMonitorConfig(jsonContent: String, onResult: (Result<Boolean>) -> Unit) {
        val body = jsonContent.toRequestBody(JSON_MEDIA_TYPE)
        val request = Request.Builder()
            .url("$baseUrl/api/config/monitor_config")
            .post(body)
            .build()
        client.newCall(request).enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                runOnMain(onResult, Result.failure(e))
            }
            override fun onResponse(call: Call, response: Response) {
                if (response.isSuccessful) {
                    runOnMain(onResult, Result.success(true))
                } else {
                    runOnMain(onResult, Result.failure(IOException("Server error: ${response.code}")))
                }
                response.close()
            }
        })
    }

    fun uploadExitParams(jsonContent: String, onResult: (Result<Boolean>) -> Unit) {
        val body = jsonContent.toRequestBody(JSON_MEDIA_TYPE)
        val request = Request.Builder()
            .url("$baseUrl/api/config/exit_params")
            .post(body)
            .build()
        client.newCall(request).enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                runOnMain(onResult, Result.failure(e))
            }
            override fun onResponse(call: Call, response: Response) {
                if (response.isSuccessful) {
                    runOnMain(onResult, Result.success(true))
                } else {
                    runOnMain(onResult, Result.failure(IOException("Server error: ${response.code}")))
                }
                response.close()
            }
        })
    }

    fun downloadMonitorConfig(onResult: (Result<String>) -> Unit) {
        val request = Request.Builder()
            .url("$baseUrl/api/config/monitor_config")
            .get()
            .build()
        client.newCall(request).enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                runOnMain(onResult, Result.failure(e))
            }
            override fun onResponse(call: Call, response: Response) {
                try {
                    if (response.isSuccessful) {
                        val bodyString = response.body?.string() ?: ""
                        runOnMain(onResult, Result.success(bodyString))
                    } else {
                        runOnMain(onResult, Result.failure(IOException("Server error: ${response.code}")))
                    }
                } catch (e: Exception) {
                    runOnMain(onResult, Result.failure(e))
                } finally {
                    response.close()
                }
            }
        })
    }

    fun downloadExitParams(onResult: (Result<String>) -> Unit) {
        val request = Request.Builder()
            .url("$baseUrl/api/config/exit_params")
            .get()
            .build()
        client.newCall(request).enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                runOnMain(onResult, Result.failure(e))
            }
            override fun onResponse(call: Call, response: Response) {
                try {
                    if (response.isSuccessful) {
                        val bodyString = response.body?.string() ?: ""
                        runOnMain(onResult, Result.success(bodyString))
                    } else {
                        runOnMain(onResult, Result.failure(IOException("Server error: ${response.code}")))
                    }
                } catch (e: Exception) {
                    runOnMain(onResult, Result.failure(e))
                } finally {
                    response.close()
                }
            }
        })
    }

    fun uploadSectorCache(jsonContent: String, onResult: (Result<Boolean>) -> Unit) {
        val body = jsonContent.toRequestBody(JSON_MEDIA_TYPE)
        val request = Request.Builder()
            .url("$baseUrl/api/config/sector_cache")
            .post(body)
            .build()
        client.newCall(request).enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                runOnMain(onResult, Result.failure(e))
            }
            override fun onResponse(call: Call, response: Response) {
                if (response.isSuccessful) {
                    runOnMain(onResult, Result.success(true))
                } else {
                    runOnMain(onResult, Result.failure(IOException("Server error: ${response.code}")))
                }
                response.close()
            }
        })
    }

    fun downloadSectorCache(onResult: (Result<String>) -> Unit) {
        val request = Request.Builder()
            .url("$baseUrl/api/config/sector_cache")
            .get()
            .build()
        client.newCall(request).enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                runOnMain(onResult, Result.failure(e))
            }
            override fun onResponse(call: Call, response: Response) {
                try {
                    if (response.isSuccessful) {
                        val bodyString = response.body?.string() ?: ""
                        runOnMain(onResult, Result.success(bodyString))
                    } else {
                        runOnMain(onResult, Result.failure(IOException("Server error: ${response.code}")))
                    }
                } catch (e: Exception) {
                    runOnMain(onResult, Result.failure(e))
                } finally {
                    response.close()
                }
            }
        })
    }

    fun uploadEnvPolicies(policies: String, onResult: (Result<Boolean>) -> Unit) {
        val requestBody = gson.toJson(mapOf("policies" to policies)).toRequestBody(JSON_MEDIA_TYPE)
        val request = Request.Builder()
            .url("$baseUrl/api/config/env_policies")
            .post(requestBody)
            .build()
        client.newCall(request).enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                runOnMain(onResult, Result.failure(e))
            }
            override fun onResponse(call: Call, response: Response) {
                if (response.isSuccessful) {
                    runOnMain(onResult, Result.success(true))
                } else {
                    runOnMain(onResult, Result.failure(IOException("Server error: ${response.code}")))
                }
                response.close()
            }
        })
    }

    fun downloadEnvPolicies(onResult: (Result<String>) -> Unit) {
        val request = Request.Builder()
            .url("$baseUrl/api/config/env_policies")
            .get()
            .build()
        client.newCall(request).enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                runOnMain(onResult, Result.failure(e))
            }
            override fun onResponse(call: Call, response: Response) {
                try {
                    if (response.isSuccessful) {
                        val bodyString = response.body?.string() ?: ""
                        runOnMain(onResult, Result.success(bodyString))
                    } else {
                        runOnMain(onResult, Result.failure(IOException("Server error: ${response.code}")))
                    }
                } catch (e: Exception) {
                    runOnMain(onResult, Result.failure(e))
                } finally {
                    response.close()
                }
            }
        })
    }

    fun uploadAccountLedger(fileBytes: ByteArray, onResult: (Result<Boolean>) -> Unit) {
        val requestBody = MultipartBody.Builder()
            .setType(MultipartBody.FORM)
            .addFormDataPart(
                "file",
                "account_ledger.sqlite",
                fileBytes.toRequestBody("application/octet-stream".toMediaType())
            )
            .build()

        val request = Request.Builder()
            .url("$baseUrl/api/config/account_ledger")
            .post(requestBody)
            .build()

        client.newCall(request).enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                runOnMain(onResult, Result.failure(e))
            }
            override fun onResponse(call: Call, response: Response) {
                if (response.isSuccessful) {
                    runOnMain(onResult, Result.success(true))
                } else {
                    runOnMain(onResult, Result.failure(IOException("Server error: ${response.code}")))
                }
                response.close()
            }
        })
    }

    fun downloadAccountLedger(onResult: (Result<ByteArray>) -> Unit) {
        val request = Request.Builder()
            .url("$baseUrl/api/config/account_ledger")
            .get()
            .build()

        client.newCall(request).enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                runOnMain(onResult, Result.failure(e))
            }
            override fun onResponse(call: Call, response: Response) {
                try {
                    if (response.isSuccessful) {
                        val bytes = response.body?.bytes()
                        if (bytes != null) {
                            runOnMain(onResult, Result.success(bytes))
                        } else {
                            runOnMain(onResult, Result.failure(IOException("Empty response body")))
                        }
                    } else {
                        runOnMain(onResult, Result.failure(IOException("Server error: ${response.code}")))
                    }
                } catch (e: Exception) {
                    runOnMain(onResult, Result.failure(e))
                } finally {
                    response.close()
                }
            }
        })
    }

    fun correctCash(cash: Double, t2PendingCash: Double?, onResult: (Result<String>) -> Unit) {
        val bodyMap = mutableMapOf<String, Any>(
            "cash" to cash
        )
        if (t2PendingCash != null) {
            bodyMap["t2_pending_cash"] = t2PendingCash
        }
        val requestBody = gson.toJson(bodyMap).toRequestBody(JSON_MEDIA_TYPE)
        val request = Request.Builder()
            .url("$baseUrl/api/accounts/real/cash")
            .post(requestBody)
            .build()

        Log.d(TAG, "correctCash: POST $baseUrl/api/accounts/real/cash: cash=$cash, t2PendingCash=$t2PendingCash")
        client.newCall(request).enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                Log.e(TAG, "correctCash failed", e)
                runOnMain(onResult, Result.failure(e))
            }

            override fun onResponse(call: Call, response: Response) {
                try {
                    if (response.isSuccessful) {
                        Log.d(TAG, "correctCash success")
                        runOnMain(onResult, Result.success("可用现金/待交收现金修正成功"))
                    } else {
                        val errorMsg = response.body?.string() ?: ""
                        Log.w(TAG, "correctCash failure: HTTP ${response.code} -> $errorMsg")
                        runOnMain(onResult, Result.failure(IOException("修正现金失败: HTTP ${response.code}: $errorMsg")))
                    }
                } catch (e: Exception) {
                    Log.e(TAG, "correctCash exception", e)
                    runOnMain(onResult, Result.failure(e))
                } finally {
                    response.close()
                }
            }
        })
    }
}

