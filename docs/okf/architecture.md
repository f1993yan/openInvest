---
type: Architecture
title: openInvest Architecture Overview
description: High-level overview of the 4-layer pipeline of openInvest.
tags: [architecture, design, pipeline]
timestamp: 2026-06-21T20:46:00Z
---

# openInvest Architecture Overview

The openInvest system is built as a 4-layer pipeline, ensuring clean separation of concerns and high interoperability.

## The 4-Layer Pipeline

1. **Connectors (Trigger/Ingestion Layer)**: Converts external protocols (HTTP REST, SSE, Desktop GUI, CLI) into core requests. Examples include `connectors/web_api.py` and `scripts/monitor_desktop_window.py`.
2. **Agents (Decision-Making Layer)**: Encapsulates LLM prompts and roles for independent multi-agent debates (Macro, Quant, Risk, CIO).
3. **Core (Business & Validation Layer)**: Implements database transactions, portfolio operations, and schema validation.
4. **Memory (Persistence Layer)**: Uses YAML frontmatter Markdown files (`portfolio.md`, `strategy.md`, and daily reports) for clean, Git-friendly persistence.
