---
type: Reference
title: openInvest Data Model
description: Persistence schemas for cash, holdings, and transaction registers.
resource: file:///d:/Documents/Code/OpenInvest/docs/okf/data_model.md
tags: [data-model, schema, pydantic]
timestamp: 2026-06-20T17:40:00Z
---

# openInvest Data Model

We persist crucial operational state in YAML frontmatter Markdown files, enabling simultaneous machine read-write and clean human readability.

## Key Schemas
- **Cash Accounts**: Structured as `cash: dict[currency_code, amount]`, accommodating multiple currencies (CNY, AUD, USD).
- **Holdings**: A list of holding objects containing unique attributes (`symbol`, `kind`, `units`, `avg_cost`, `cost_currency`, `channel`).
- **RMW Transactions**: Managed via thread-safe file locks and atomic writes (`core/portfolio_manager.py`) to prevent concurrency race conditions.
