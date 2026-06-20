---
type: Concept
title: openInvest Multi-Agent System
description: Explains the roles, prompts, and debate consensus mechanisms of the 4 investment agents.
resource: file:///d:/Documents/Code/OpenInvest/docs/okf/agents.md
tags: [agents, llm, consensus]
timestamp: 2026-06-20T17:40:00Z
---

# openInvest Multi-Agent System

Our decision-making process is run by a committee of four independent LLM sessions with strict information isolation.

## Agent Roles
- **Macro Strategist**: Monitors global macro indicators (interest rates, yield curves, currency spot rates).
- **Quant Analyst**: Runs technical pattern recognition (RSI, moving average alignment, price percentiles).
- **Risk Officer**: Evaluates portfolio concentration, paper trade insights, and drawdown buffer.
- **CIO (Chief Investment Officer)**: Reviews transcripts from the debate and issues the final verdict (BUY, ACCUMULATE, HOLD, TRIM, SELL) with confidence scores.
