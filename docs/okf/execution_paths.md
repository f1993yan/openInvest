---
type: Guide
title: openInvest Execution Paths
description: Explains the dual paths of execution in openInvest.
tags: [execution, paths, skill, cron]
timestamp: 2026-06-21T20:46:00Z
---

# openInvest Execution Paths

The openInvest system maintains two functional paths of execution to serve different cost, performance, and automation profiles.

## The Dual Paths
- **Skill Path (Claude Agent Mode)**: Invoked by a local agent runner, leveraging state-of-the-art models for interactive and cost-effective investigations.
- **Web/Cron Path (DeepSeek Multi-threaded Mode)**: Invoked automatically via daily/weekly schedules or REST calls, sweeping multiple symbols in parallel to produce regular dashboard reports.
