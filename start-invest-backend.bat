@echo off
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d D:\Documents\Code\OpenInvest
echo openInvest backend starting on http://127.0.0.1:8766 ...
start "openInvest Backend" /B uv run uvicorn backend.server:app --host 127.0.0.1 --port 8766 > backend.log 2>&1
echo Backend started.
echo Starting market monitor (9:30-15:00, every 30min)...
start "openInvest Monitor" /B uv run python -m jobs.market_monitor > data\market_monitor\monitor_stdout.log 2>&1
echo Monitor started. You can close this window.
