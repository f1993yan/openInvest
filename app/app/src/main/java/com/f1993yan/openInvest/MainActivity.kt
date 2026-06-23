package com.f1993yan.openInvest

import android.content.Context
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Scaffold
import androidx.compose.ui.Modifier
import com.f1993yan.openInvest.network.NetworkClient
import com.f1993yan.openInvest.ui.theme.OpenInvestTheme

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        // Initialize Python
        if (!com.chaquo.python.Python.isStarted()) {
            com.chaquo.python.Python.start(com.chaquo.python.android.AndroidPlatform(this))
        }

        // Load saved server URL
        val prefs = getSharedPreferences("open_invest_prefs", Context.MODE_PRIVATE)
        val savedUrl = prefs.getString("server_url", "http://10.0.2.2:8765") ?: "http://10.0.2.2:8765"
        NetworkClient.setBaseUrl(savedUrl)

        enableEdgeToEdge()
        setContent {
            OpenInvestTheme {
                Scaffold(modifier = Modifier.fillMaxSize()) { innerPadding ->
                    MainScreen(
                        modifier = Modifier.padding(innerPadding),
                        onSaveUrl = { newUrl ->
                            prefs.edit().putString("server_url", newUrl).apply()
                            NetworkClient.setBaseUrl(newUrl)
                        }
                    )
                }
            }
        }
    }
}
