# BTC alerts → Telegram

A small bot that checks Bitcoin every 5 minutes and messages you on Telegram. It runs free on GitHub Actions, so there is no server to manage and your computer doesn't need to stay on.

## What it sends

| Alert | When | Sound |
|---|---|---|
| Key level broken / reclaimed | Price clears a level by 0.15% (stops chop spam). Several levels in one move arrive as one message | 🔔 major · 🔕 minor |
| Moving levels | 50W SMA/EMA, 200W SMA, 50D SMA/EMA, 200D SMA, yearly anchored VWAP, all recalculated live | 🔔 |
| Daily close | Just after 00:00 UK: what closed through what, RSI, golden/death cross | 🔔 if something happened, otherwise 🔕 |
| Weekly close | Monday 00:05 UK: close vs 50W/200W, red/green streak, logged to `weekly_log.md` | 🔔 (gets through mute) |
| Weekly close heads-up | Sunday 22:00 UK, with distance to the 50W lines | 🔔 |
| Fast move | ±3% in 1h or ±6% in 24h | 🔔 |
| Sentiment extremes | Fear & Greed ≤20 / ≥80, perp funding extreme | 🔔 |
| Event reminders | Day before + 1h before CPI, FOMC, NFP, PCE, Nvidia, expiries... | 🔔 |
| Event reaction | 30–60 min after a release: how much BTC moved | 🔔 |
| News | High-impact headlines from CoinDesk, Cointelegraph, The Block, Decrypt, Bitcoin Magazine, the Fed and CNBC, keyword-scored and de-duplicated | 🔔 big stories (🔥) · 🔕 the rest |
| Morning brief | 08:00 UK: price, MAs, nearest levels, next 7 days | 🔕 |

### Commands (send these to your bot)

- `/status`: price, MAs, nearest levels, upcoming events
- `/levels`: every tracked level, with where price is now
- `/add 74k bid zone` and `/del 74000`: your own levels
- `/events`: calendar
- `/mute 6` and `/unmute`: quiet hours (weekly close, big moves and high-priority events still get through)
- `/help`

Commands are answered on the next check, so within about 5 minutes. Only your chat ID can use them.

## Setup (about 10 minutes)

**1. Telegram bot**

1. In Telegram, message **@BotFather** → `/newbot` → follow the prompts. Copy the **token**.
2. Send your new bot any message (e.g. "hi").
3. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and find `"chat":{"id":123456789`. That number is your **chat ID**.

Keep the token private. It goes into GitHub Secrets only, never into a file or a chat.

**2. GitHub repo**

1. Create a new repository on github.com. Make it **Public** (see "Cost" below for why). Nothing secret lives in the files.
2. Upload everything from this folder. The `.github/workflows/alerts.yml` file must keep that exact path. If drag-and-drop skips the hidden `.github` folder, use **Add file → Create new file**, type `.github/workflows/alerts.yml` as the name and paste the contents.
3. **Settings → Secrets and variables → Actions → New repository secret**, twice:
   - `TELEGRAM_BOT_TOKEN` = your token
   - `TELEGRAM_CHAT_ID` = your chat ID

**3. Start it**

**Actions** tab → enable workflows if asked → **btc-alerts** → **Run workflow**. The first run quietly records where price sits relative to each level and marks existing news as seen, so it won't flood you. Send `/status` and you'll get a reply within about 5 minutes. After that it runs on its own every 5 minutes.

## Changing things

Everything lives in `config.toml`. Edit it on GitHub (pencil icon) and the next run uses it.

- **Levels**: add or remove `[[levels]]` blocks. `importance = "minor"` makes them silent.
- **Events**: add `[[events]]` with a UTC time. UK = UTC until 29 March 2027; after that, subtract an hour.
- **Thresholds**: the `[alerts]` section.
- **News sensitivity**: raise `min_score` for fewer headlines, lower it for more. Keyword weights are in `[news.keywords]`.

## Good to know

- **Cost.** Public repos get unlimited free Actions minutes. A private repo would use ~8,600 minutes/month at a 5-minute cadence, against 2,000 free. If you want it private, change the cron to `*/30 * * * *`.
- **Timing.** GitHub sometimes delays scheduled runs by a few minutes when busy. Alerts are near-real-time, not tick-perfect. For instant alerts on one critical level, also set a TradingView alert.
- **Data.** Prices come from Coinbase (with Kraken and Bitstamp fallbacks). MAs may differ by a few dollars from TradingView's BTCUSD INDEX.
- **Keep-alive.** GitHub pauses schedules after 60 days without commits. The weekly close log commit each Monday prevents that.

## Run it on your own machine instead (optional)

Python 3.11+, no packages needed:

```bash
export TELEGRAM_BOT_TOKEN=...   TELEGRAM_CHAT_ID=...
python3 alerts.py --test          # sends a test message
python3 alerts.py --loop 60       # checks every 60 s until stopped
python3 alerts.py --dry-run       # prints messages instead of sending
```
