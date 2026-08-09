# DEAC Bot — Stage 3 (Combined)

Stage 1 (dispatch: `/BOL`, `/t`, `/POD`, `/PU`, `/DEL`) + Stage 2
(maintenance/PTI: `/admin`, `/pti`, `/ptidone`, `/history`, `/incident`)
merged into **one bot, one process, one deployment.**

⚠️ **Trade-off to know:** because it's now one process, a bug in the
PTI code could in theory affect the dispatch side too (they share the
same bot and event loop). If you want the "one breaks, other keeps
running" safety net back, that means going back to two separate bots —
just say so and I'll re-split it.

## Setup (Render.com — free tier)

1. Create **one** bot via [@BotFather](https://t.me/BotFather).
2. Add it to every driver group, your main service group, and your
   maintenance group.
3. Push this folder to a GitHub repo.
4. On [render.com](https://render.com): **New → Web Service** → connect the repo.
   - Build command: `pip install -r requirements.txt`
   - Start command: `python bot.py`
   - Plan: **Free**
5. Environment variables:
   - `BOT_TOKEN` = your bot token
   - `SERVICE_GROUP_ID` = main dispatch/service group chat_id (negative number)
   - `REMINDER_MINUTES` = `5` (optional)
   - `DAILY_REPORT_HOUR_UTC` = `18` (optional)
6. Set up a free [UptimeRobot](https://uptimerobot.com) monitor pinging
   the Render URL every 5 minutes so the free tier doesn't sleep.

## One-time setup

In each driver group:
```
/register John Doe
```

In your maintenance group:
```
/admin
```

## All commands

**Dispatch (driver groups):** `/BOL`, `/t`, `/POD`, `/PU`, `/DEL`
**Dispatch (service group):** `/done`, `/pending`, `/export`
**Maintenance (maintenance group):** `/admin`, `/pti`, `/history DriverName`
**Driver groups (PTI):** `/ptidone`, `/incident <details>`

Full behavior details are the same as documented in the separate
Stage 1 / Stage 2 READMEs previously provided — only the deployment is
now merged.

## Notes

- Written fresh from your spec (originals not yet shared) — send your
  real `dispatchbot.py`/`bot.py` and I'll reconcile differences.
- Data in a local JSON file — resets on Render free-tier redeploy.
  Upgrade to a paid plan with a disk, or use a VPS, before real daily use.
