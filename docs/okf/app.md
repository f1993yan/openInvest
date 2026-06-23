---
type: Reference
title: openInvest Android App Client
description: Overview of the openInvest Android Client architecture, functional components, and state synchronization.
resource: ../../app
tags: [android, mobile, kotlin, compose, ui]
timestamp: 2026-06-23T07:58:00+08:00
---

# openInvest Android Client

The openInvest Android client is a native Kotlin Jetpack Compose application designed to provide a real-time portfolio dashboard, configuration synchronization, quick trade accounting, and interactive multi-agent debate logs.

## Modular Component Structure
To improve codebase maintainability and readability, the app is split into functional components:
- **`MainActivity.kt`**: Lightweight launcher that initializes the Python runtime (Chaquopy), handles shared preferences, and embeds the main container inside the theme.
- **`MainScreen.kt`**: The dashboard controller that manages global states, connects with the refresh service, triggers background committee evaluations, and routes actions to appropriate dialog panels.
- **`Helpers.kt`**: Houses global utility functions, extension functions, file I/O caching, and system notification handlers.
- **`StockCard.kt`**: Contains the visual representations of holding and watchlist cards, complete with swipe-to-dismiss actions for trade execution and deletion.
- **`SettingsDialog.kt`**: Manages server connection settings, crawler schedules, auto-refresh triggers, and backup imports/exports.
- **`NewsDialog.kt`**: Renders weekend news summaries fetched from the backend server.
- **`SelectionStockDialog.kt`**: Lists daily screened candidates with options to track or buy.
- **`CommitteeAnalysisDialog.kt`**: Visualizes local or remote multi-agent debate tabs (Macro, Quant, Risk, CIO) and triggers live Server-Sent Events (SSE) updates.
- **`TradeExecutionDialog.kt`**: Quick accounting interface to log transactions to the ledger.
- **`UpdateHoldingDialog.kt`**: Popover to edit holding quantities, average cost, and watchlist statuses.

## Synchronization & Cache
- **Local JSON Cache**: Persists snapshots (`resolved_snapshot.json`), Daily Selection results (`selection_cache.json`), and debate logs (`committee_results_cache.json`) using secure app-private storage.
- **Auto-Refresh Service**: Implements a background Android `Service` (`AutoRefreshService`) polling the backend server at user-defined intervals to fetch pricing, news, and screening updates.
