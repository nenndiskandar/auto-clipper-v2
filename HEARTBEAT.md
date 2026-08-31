<!-- Heartbeat: periodic checks for bot health & daily maintenance -->

## Bot Health Check (every 30 min)
- Check if Telegram bot process is alive: `Get-Process python*`
- If bot is DOWN → send alert to Iskan via Telegram (use `message` tool)
- If bot is UP → no action needed (don't spam)

## Bot Error Scan (every 2 hours)
- Read last 30 lines of `D:\clip-prog\yt-short-clipper\telegram-bot.log`
- If ERROR/Traceback found → send summary to Iskan via Telegram
- Ignore "INFO - HTTP Request" lines (normal polling noise)
