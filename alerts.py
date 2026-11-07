#!/usr/bin/env python3
"""
BTC alert bot -> Telegram.

One invocation = one check (made for GitHub Actions / cron every 5 min).
Use --loop SECONDS to run continuously on your own machine or a VPS.
Standard library only. Python 3.11+.

Alert types
  - static key levels (config.toml) and your own levels added with /add
  - dynamic levels: daily/weekly MAs, 200W SMA, yearly anchored VWAP (live, TradingView-style)
  - daily close + weekly close reports (what closed through what, RSI, golden/death cross)
  - weekly-close heads-up on Sunday night
  - fast-move alerts (1h / 24h)
  - Fear & Greed and perp funding extremes
  - event reminders (CPI, FOMC, NFP, ...) + "reaction" message after the release
  - high-impact news from RSS feeds, keyword-scored and de-duplicated
  - morning brief
  - Telegram commands: /status /levels /add /del /events /mute /unmute /help
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
UTC = timezone.utc
UA = "btc-alerts/1.0 (personal Telegram alert bot)"

DYNAMIC_LABELS = {
    "sma50d": "Daily 50 SMA",
    "ema50d": "Daily 50 EMA",
    "sma200d": "200-day SMA",
    "ema200d": "200-day EMA",
    "sma50w": "50-week SMA",
    "ema50w": "50-week EMA",
    "sma200w": "200-week SMA",
    "avwap_ytd": "Yearly anchored VWAP",
}


def log(*a):
    print(datetime.now(UTC).strftime("%H:%M:%S"), *a, flush=True)


def usd(x):
    return f"${x:,.0f}"


def pct(x):
    return f"{x:+.1f}%"


def esc(s):
    return html.escape(str(s), quote=False)


# --------------------------------------------------------------------------- HTTP

def http_get(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def get_json(url, timeout=15):
    return json.loads(http_get(url, timeout))


# --------------------------------------------------------------------------- Telegram

class Telegram:
    def __init__(self, token, chat_id, dry=False):
        self.token, self.chat_id, self.dry = token, str(chat_id or ""), dry or not token

    def _call(self, method, params, timeout=20):
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        data = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(url, data=data, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())

    def send(self, text, silent=False, chat_id=None):
        text = text[:4000]
        if self.dry:
            print(("[silent] " if silent else "") + "---- TELEGRAM ----\n" + text + "\n------------------")
            return True
        params = {
            "chat_id": chat_id or self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
            "disable_notification": "true" if silent else "false",
        }
        try:
            self._call("sendMessage", params)
            return True
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="ignore")
            log("telegram error", e.code, body[:300])
            if e.code == 400 and "parse" in body.lower():  # bad HTML -> retry as plain text
                params.pop("parse_mode")
                params["text"] = re.sub(r"<[^>]+>", "", text)
                try:
                    self._call("sendMessage", params)
                    return True
                except Exception as e2:
                    log("telegram retry failed", e2)
        except Exception as e:
            log("telegram error", e)
        return False

    def updates(self, offset):
        if self.dry:
            return []
        try:
            res = self._call("getUpdates", {"offset": offset, "timeout": 0, "allowed_updates": '["message"]'})
            return res.get("result", [])
        except Exception as e:
            log("getUpdates failed", e)
            return []


# --------------------------------------------------------------------------- market data

def spot_price():
    """Live BTC-USD price with fallbacks. Returns (price, source)."""
    try:
        return float(get_json("https://api.coinbase.com/v2/prices/BTC-USD/spot")["data"]["amount"]), "Coinbase"
    except Exception as e:
        log("coinbase spot failed", e)
    try:
        r = get_json("https://api.kraken.com/0/public/Ticker?pair=XBTUSD")["result"]
        return float(next(iter(r.values()))["c"][0]), "Kraken"
    except Exception as e:
        log("kraken spot failed", e)
    try:
        return float(get_json("https://www.bitstamp.net/api/v2/ticker/btcusd/")["last"]), "Bitstamp"
    except Exception as e:
        log("bitstamp spot failed", e)
    raise RuntimeError("all price sources failed")


def day_open_24h():
    try:
        return float(get_json("https://api.exchange.coinbase.com/products/BTC-USD/stats")["open"])
    except Exception:
        return None


def fetch_daily_history(days=1500, now=None):
    """Completed UTC daily candles, ascending: [date_iso, close, hlc3, volume]."""
    now = now or datetime.now(UTC)
    today0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    rows = {}
    try:
        end = today0
        while len(rows) < days:
            start = end - timedelta(days=299)
            url = ("https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=86400"
                   f"&start={start.strftime('%Y-%m-%dT%H:%M:%SZ')}&end={end.strftime('%Y-%m-%dT%H:%M:%SZ')}")
            batch = get_json(url)
            if not batch:
                break
            for t, lo, hi, op, cl, vol in batch:
                d = datetime.fromtimestamp(t, UTC)
                if d < today0:
                    rows[d.date().isoformat()] = [float(cl), (float(hi) + float(lo) + float(cl)) / 3, float(vol)]
            end = start
            time.sleep(0.35)
        if len(rows) >= 400:
            return [[k] + v for k, v in sorted(rows.items())]
        log("coinbase history short:", len(rows))
    except Exception as e:
        log("coinbase history failed", e)
    # Fallback: Kraken (max 720 days -> no 200W SMA)
    r = get_json("https://api.kraken.com/0/public/OHLC?pair=XBTUSD&interval=1440")["result"]
    data = next(v for k, v in r.items() if k != "last")
    out = []
    for t, o, h, l, c, vw, vol, n in data:
        d = datetime.fromtimestamp(int(t), UTC)
        if d < today0:
            out.append([d.date().isoformat(), float(c), (float(h) + float(l) + float(c)) / 3, float(vol)])
    return out


def fear_greed():
    d = get_json("https://api.alternative.me/fng/?limit=1")["data"][0]
    return int(d["value"]), d["value_classification"]


def perp_funding():
    """Deribit BTC perpetual: (8h funding as %, open interest USD)."""
    r = get_json("https://www.deribit.com/api/v2/public/ticker?instrument_name=BTC-PERPETUAL")["result"]
    return float(r["funding_8h"]) * 100, float(r.get("open_interest", 0))


# --------------------------------------------------------------------------- indicators

def sma(v, n):
    return sum(v[-n:]) / n if len(v) >= n else None


def ema(v, n):
    if len(v) < n:
        return None
    k = 2 / (n + 1)
    e = sum(v[:n]) / n
    for x in v[n:]:
        e = x * k + e * (1 - k)
    return e


def ema_series(v, n):
    if len(v) < n:
        return []
    k = 2 / (n + 1)
    e = sum(v[:n]) / n
    out = [e]
    for x in v[n:]:
        e = x * k + e * (1 - k)
        out.append(e)
    return out  # out[i] aligns with v[n - 1 + i]


def macd_hist(v, fast=12, slow=26, sig=9):
    if len(v) < slow + sig:
        return None
    ef, es = ema_series(v, fast), ema_series(v, slow)
    ef = ef[slow - fast:]  # align both to v[slow-1:]
    line = [a - b for a, b in zip(ef, es)]
    s = ema_series(line, sig)
    return line[-1] - s[-1] if s else None


def fmt_funding(f8_pct):
    """Funding per 8h given in percent -> basis points + annualised."""
    return f"{f8_pct * 100:+.2f}bp/8h (≈{f8_pct * 3 * 365:+.1f}% APR)"


def rsi(v, n=14):
    if len(v) < n + 1:
        return None
    g = [max(0.0, v[i] - v[i - 1]) for i in range(1, len(v))]
    l = [max(0.0, v[i - 1] - v[i]) for i in range(1, len(v))]
    ag, al = sum(g[:n]) / n, sum(l[:n]) / n
    for gi, li in zip(g[n:], l[n:]):
        ag = (ag * (n - 1) + gi) / n
        al = (al * (n - 1) + li) / n
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def weekly_completed(rows):
    """Completed Mon-Sun weeks: list of (sunday_iso, close)."""
    out = []
    for r in rows:
        d = date.fromisoformat(r[0])
        if d.weekday() == 6:
            out.append((r[0], r[1]))
    return out


def indicators(rows, live=None, avwap_anchor=None):
    """MAs on daily/weekly closes. With `live`, the current (incomplete) candle uses the live
    price, like TradingView shows intrabar. Without it, values are as of the last completed close."""
    closes = [r[1] for r in rows]
    d = closes + ([live] if live is not None else [])
    wk = [c for _, c in weekly_completed(rows)]
    w = wk + ([live] if live is not None else [])
    out = {
        "sma50d": sma(d, 50), "ema50d": ema(d, 50),
        "sma200d": sma(d, 200), "ema200d": ema(d, 200),
        "rsi14d": rsi(d, 14),
        "sma50w": sma(w, 50), "ema50w": ema(w, 50), "sma200w": sma(w, 200),
        "avwap_ytd": None,
        "macd_hist": macd_hist(d),
        "sma50w_slope": (sma(w, 50) - sma(w[:-4], 50)) if len(w) >= 54 else None,
        "ema50d_slope": (ema(d, 50) - ema(d[:-5], 50)) if len(d) >= 55 else None,
    }
    if rows:
        anchor = avwap_anchor or f"{rows[-1][0][:4]}-01-01"
        seg = [r for r in rows if r[0] >= anchor and r[3] > 0]
        vol = sum(r[3] for r in seg)
        if vol > 0:
            out["avwap_ytd"] = sum(r[2] * r[3] for r in seg) / vol
    return out


# --------------------------------------------------------------------------- news

def _strip_ns(tag):
    return tag.split("}", 1)[-1]


def parse_feed(raw):
    """RSS 2.0 / Atom -> list of dicts(title, link, summary, published: datetime|None)."""
    items = []
    root = ET.fromstring(raw)
    for el in root.iter():
        tag = _strip_ns(el.tag)
        if tag not in ("item", "entry"):
            continue
        d = {"title": "", "link": "", "summary": "", "published": None}
        for c in el:
            ct = _strip_ns(c.tag)
            txt = (c.text or "").strip()
            if ct == "title":
                d["title"] = txt
            elif ct == "link":
                d["link"] = c.attrib.get("href") or txt or d["link"]
            elif ct in ("description", "summary", "content") and not d["summary"]:
                d["summary"] = re.sub(r"<[^>]+>", " ", txt)[:600]
            elif ct in ("pubDate", "published", "updated", "date") and d["published"] is None:
                try:
                    d["published"] = parsedate_to_datetime(txt)
                except Exception:
                    try:
                        d["published"] = datetime.fromisoformat(txt.replace("Z", "+00:00"))
                    except Exception:
                        pass
                if d["published"] and d["published"].tzinfo is None:
                    d["published"] = d["published"].replace(tzinfo=UTC)
        if d["title"]:
            items.append(d)
    return items


def score_news(title, summary, keywords):
    """keywords: {regex: weight}. Title hits count fully, summary-only hits count half."""
    t, s = title.lower(), summary.lower()
    score, hits = 0.0, []
    for pat, w in keywords.items():
        rx = re.compile(pat, re.I)
        if rx.search(t):
            score += w
            hits.append(pat)
        elif rx.search(s):
            score += w / 2
            hits.append(pat)
    return score, hits


DEFAULT_BULLISH = [
    r"\binflows?\b", r"\b(buys|bought|adds|acquires|accumulat)", r"\bapprov", r"rate cuts?\b", r"\bcuts? rates?",
    r"\badopt", r"strategic (bitcoin )?reserve", r"\b(surge|soar|rall(y|ies)|jump|rebound|recover)",
    r"all-time high|record high", r"short squeeze|shorts? liquidat", r"\bpasses\b|\bsigned into law",
    r"\bdovish\b", r"\beases?\b|\bcooler\b|\bsoft(er)?\b",
]
DEFAULT_BEARISH = [
    r"\bhack|\bexploit|\bdrain|\bstolen", r"\boutflows?\b", r"rate (hike|increase|rise)|\bhikes?\b|raises? rates?",
    r"\bban(s|ned)?\b", r"\bsues?\b|lawsuit|\bcharge[sd]\b|\bindict", r"\b(crash|plunge|tumble|slump|sink|sell-?off|drop)",
    r"\binsolven|\bbankrupt|halts? withdrawals", r"long liquidat|\bliquidations?\b", r"\bhawkish\b",
    r"\bhot(ter)?\b|\bsticky\b", r"\bfails?\b|\bdelay|\bstall", r"\bdepeg", r"\bsell(s|ing)?\b|\bdump",
]


def news_tone(text, bullish=None, bearish=None):
    t = text.lower()
    b = sum(1 for p in (bullish or DEFAULT_BULLISH) if re.search(p, t, re.I))
    s = sum(1 for p in (bearish or DEFAULT_BEARISH) if re.search(p, t, re.I))
    return "bull" if b > s else "bear" if s > b else "neutral"


def title_words(t):
    return {w for w in re.findall(r"[a-z0-9$]+", t.lower()) if len(w) > 3}


def similar(a, b):
    if not a or not b:
        return False
    return len(a & b) / max(1, len(a | b)) >= 0.5


# --------------------------------------------------------------------------- state

DEFAULT_STATE = {
    "sides": {}, "level_last_alert": {}, "user_levels": [],
    "samples": [], "last_price": None, "price_failures": 0,
    "history": [], "history_date": None,
    "last_close_date": None, "move_last_alert": {},
    "sentiment": {}, "sentiment_ts": 0, "fng_zone": None, "funding_zone": None,
    "events_sent": {}, "weekly_warn_sent": None,
    "news_seen": {}, "news_init": {}, "news_recent": [], "news_ts": 0,
    "last_digest": None, "tg_offset": 0, "mute_until": 0,
}


def load_state(path):
    st = json.loads(json.dumps(DEFAULT_STATE))
    try:
        with open(path) as f:
            st.update(json.load(f))
    except FileNotFoundError:
        pass
    except Exception as e:
        log("state unreadable, starting fresh:", e)
    return st


def save_state(path, st):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, separators=(",", ":"))
    os.replace(tmp, path)


def parse_dt(v):
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    return datetime.fromisoformat(str(v).replace("Z", "+00:00"))


# --------------------------------------------------------------------------- the bot

class Bot:
    def __init__(self, cfg, st, tg, now=None, weekly_log=None):
        self.cfg, self.st, self.tg = cfg, st, tg
        self.now = now or datetime.now(UTC)
        self.ts = self.now.timestamp()
        self.tz = ZoneInfo(cfg.get("timezone", "Europe/London"))
        self.a = cfg.get("alerts", {})
        self.out = []  # (text, priority) priority: high | normal | low
        self.weekly_log = weekly_log
        self.price = None
        self.live = {}

    # -- helpers
    def local(self, dt=None):
        return (dt or self.now).astimezone(self.tz)

    def fmt_time(self, dt):
        return self.local(dt).strftime("%a %d %b %H:%M")

    def emit(self, text, priority="normal"):
        self.out.append((text, priority))

    def muted(self):
        return self.ts < self.st.get("mute_until", 0)

    def flush(self):
        for text, pr in self.out:
            if self.muted() and pr != "high":
                log("muted:", text.splitlines()[0])
                continue
            self.tg.send(text, silent=(pr == "low"))
        self.out = []

    def price_at(self, target_ts, tolerance=1200):
        best = None
        for t, p in self.st["samples"]:
            if t <= target_ts and target_ts - t <= tolerance:
                if best is None or t > best[0]:
                    best = (t, p)
        return best[1] if best else None

    # -- bullish / bearish read
    def bias(self):
        """Backdrop from live indicators.
        Trend (weekly): price vs 50W SMA, 50W EMA, 200W SMA + 50W SMA slope      -> -4..+4
        Momentum (daily): price vs 50D EMA/SMA, 200D SMA, yearly AVWAP, RSI, MACD
                          histogram, 50D EMA slope                                -> -7..+7"""
        ind, p = self.live, self.price
        if not ind or not p:
            return None
        sgn = lambda x: (x > 0) - (x < 0)
        t, m = [], []
        for k in ("sma50w", "ema50w", "sma200w"):
            if ind.get(k):
                t.append(1 if p >= ind[k] else -1)
        if ind.get("sma50w_slope") is not None:
            t.append(sgn(ind["sma50w_slope"]))
        for k in ("ema50d", "sma50d", "sma200d", "avwap_ytd"):
            if ind.get(k):
                m.append(1 if p >= ind[k] else -1)
        r = ind.get("rsi14d")
        if r is not None:
            m.append(1 if r > 55 else -1 if r < 45 else 0)
        for k in ("macd_hist", "ema50d_slope"):
            if ind.get(k) is not None:
                m.append(sgn(ind[k]))
        ts, ms = sum(t), sum(m)
        tl = "bull" if ts >= 2 else "bear" if ts <= -2 else "neutral"
        ml = "bull" if ms >= 3 else "bear" if ms <= -3 else "neutral"
        if tl == ml:
            ov = tl
        elif "neutral" in (tl, ml):
            ov = (tl if ml == "neutral" else ml) + "_lean"
        else:
            ov = "mixed"
        return {"trend": tl, "mom": ml, "overall": ov, "ts": ts, "ms": ms, "tn": len(t), "mn": len(m)}

    ICON = {"bull": "🟢", "bear": "🔴", "neutral": "⚪"}
    OVERALL = {"bull": "🟢 Bullish", "bear": "🔴 Bearish", "bull_lean": "🟢 Bullish lean",
               "bear_lean": "🔴 Bearish lean", "neutral": "⚪ Neutral", "mixed": "🟡 Mixed"}

    def read_line(self, signal=None, why=""):
        """One line: this alert's direction + the overall backdrop, and whether they agree."""
        b = self.bias()
        if not b:
            return ""
        back = f"backdrop {self.OVERALL[b['overall']]} (trend {self.ICON[b['trend']]} · momentum {self.ICON[b['mom']]})"
        if signal is None:
            return f"<i>Read: {back}</i>"
        sig = {"bull": "🟢 <b>Bullish</b>", "bear": "🔴 <b>Bearish</b>", "neutral": "⚪ <b>Neutral</b>"}[signal]
        if why:
            sig += f" ({esc(why)})"
        ctx = "bull" if b["overall"].startswith("bull") else "bear" if b["overall"].startswith("bear") else None
        tail = ""
        if signal != "neutral" and ctx:
            tail = " → with the trend" if signal == ctx else " → against the backdrop"
        return f"{sig} · {back}{tail}"

    def bias_summary(self):
        b = self.bias()
        if not b:
            return ""
        name = {"bull": "bullish", "bear": "bearish", "neutral": "neutral"}
        return (f"<b>Bias: {self.OVERALL[b['overall']]}</b>\n"
                f"Trend (weekly) {self.ICON[b['trend']]} {name[b['trend']]} {b['ts']:+d}/{b['tn']} · "
                f"Momentum (daily) {self.ICON[b['mom']]} {name[b['mom']]} {b['ms']:+d}/{b['mn']}")

    # -- levels
    def levels(self, ind=None):
        """All tracked levels as dicts: id, price, name, note, major."""
        ind = self.live if ind is None else ind
        out = []
        for lv in self.cfg.get("levels", []):
            out.append({"id": f"s:{lv['price']}", "price": float(lv["price"]), "name": lv["name"],
                        "note": lv.get("note", ""), "major": lv.get("importance", "major") == "major"})
        for lv in self.st["user_levels"]:
            out.append({"id": f"u:{lv['price']}", "price": float(lv["price"]), "name": lv.get("name") or "Your level",
                        "note": "", "major": True})
        for key in self.cfg.get("dynamic_levels", {}).get("track", []):
            v = ind.get(key)
            if v:
                out.append({"id": f"d:{key}", "price": v, "name": DYNAMIC_LABELS.get(key, key), "note": "",
                            "major": key in self.cfg.get("dynamic_levels", {}).get("major", []), "dynamic": True})
        return out

    def check_levels(self):
        buf = self.a.get("level_buffer_pct", 0.15) / 100
        cooldown = self.a.get("level_cooldown_min", 240) * 60
        p = self.price
        hits = []
        for lv in self.levels():
            lid, L = lv["id"], lv["price"]
            side = self.st["sides"].get(lid)
            new = "above" if p > L * (1 + buf) else "below" if p < L * (1 - buf) else side
            if side is None:
                self.st["sides"][lid] = new or ("above" if p >= L else "below")
                continue
            if new and new != side:
                self.st["sides"][lid] = new
                if self.ts - self.st["level_last_alert"].get(lid, 0) < cooldown:
                    continue
                self.st["level_last_alert"][lid] = self.ts
                hits.append((lv, new == "above"))
        if hits:
            hits.sort(key=lambda h: -h[0]["price"])
            major = any(lv["major"] for lv, _ in hits)
            up = hits[0][1]
            lines = []
            for lv, u in hits:
                icon = ("🟢" if u else "🔴") if lv["major"] else ("↗️" if u else "↘️")
                lines.append(f"{icon} <b>{'reclaimed' if u else 'lost'} {esc(lv['name'])}</b> ({usd(lv['price'])})")
                if lv["note"]:
                    lines.append(f"   <i>{esc(lv['note'])}</i>")
            head = f"{'🟢' if up else '🔴'} <b>BTC {usd(p)}</b>" if len(hits) > 1 else f"<b>BTC {usd(p)}</b>"
            msg = head + "\n" + "\n".join(lines)
            nxt = self.next_levels(p, exclude={lv["id"] for lv, _ in hits})
            if nxt:
                msg += "\n" + nxt
            key_hits = [h for h in hits if h[0]["major"]] or hits
            dirs = {u for _, u in key_hits}
            signal = "neutral" if len(dirs) > 1 else ("bull" if up else "bear")
            msg += "\n" + self.read_line(signal, "level reclaimed" if signal == "bull" else "level lost" if signal == "bear" else "")
            if major:
                msg += "\n<i>Intraday break: the close is what counts.</i>"
            self.emit(msg, "normal" if major else "low")
        # drop state for levels that no longer exist
        ids = {lv["id"] for lv in self.levels()}
        for k in list(self.st["sides"]):
            if k not in ids:
                self.st["sides"].pop(k, None)

    def next_levels(self, p, exclude=()):
        if isinstance(exclude, str):
            exclude = {exclude}
        lv = [l for l in self.levels() if l["id"] not in exclude]
        above = sorted([l for l in lv if l["price"] > p], key=lambda l: l["price"])
        below = sorted([l for l in lv if l["price"] <= p], key=lambda l: -l["price"])
        parts = []
        if above:
            l = above[0]
            parts.append(f"▲ next {usd(l['price'])} {esc(l['name'])} ({pct((l['price'] / p - 1) * 100)})")
        if below:
            l = below[0]
            parts.append(f"▼ next {usd(l['price'])} {esc(l['name'])} ({pct((l['price'] / p - 1) * 100)})")
        return "\n".join(parts)

    # -- fast moves
    def check_moves(self):
        for name, secs, thr in (("1h", 3600, self.a.get("move_1h_pct", 3.0)),
                                ("24h", 86400, self.a.get("move_24h_pct", 6.0))):
            ref = self.price_at(self.ts - secs, tolerance=max(900, secs * 0.05))
            if not ref:
                continue
            chg = (self.price / ref - 1) * 100
            if abs(chg) >= thr and self.ts - self.st["move_last_alert"].get(name, 0) >= self.a.get("move_cooldown_min", 120) * 60:
                self.st["move_last_alert"][name] = self.ts
                icon = "🚀" if chg > 0 else "⚠️"
                self.emit(f"{icon} <b>BTC {pct(chg)} in {name}</b>: {usd(ref)} → {usd(self.price)}\n"
                          + self.next_levels(self.price) + "\n"
                          + self.read_line("bull" if chg > 0 else "bear", "fast move"),
                          "high" if abs(chg) >= thr * 1.6 else "normal")

    # -- daily / weekly close
    def close_reports(self):
        rows = self.st["history"]
        if len(rows) < 3:
            return
        last = rows[-1][0]
        if self.st["last_close_date"] is None:
            self.st["last_close_date"] = last
            return
        if last <= self.st["last_close_date"]:
            return
        self.st["last_close_date"] = last
        anchor = self.cfg.get("dynamic_levels", {}).get("avwap_anchor")
        now_i, prev_i = indicators(rows, avwap_anchor=anchor), indicators(rows[:-1], avwap_anchor=anchor)
        c, pc = rows[-1][1], rows[-2][1]
        crossed = []
        score = 0
        for lv in self.levels(ind=now_i):
            L = lv["price"]
            if lv.get("dynamic"):
                key = lv["id"][2:]
                Lp = prev_i.get(key)
                if Lp is None:
                    continue
            else:
                Lp = L
            if (pc >= Lp) != (c >= L):
                crossed.append(("🟢 closed ABOVE" if c >= L else "🔴 closed BELOW") + f" {esc(lv['name'])} ({usd(L)})")
                score += (1 if c >= L else -1) * (2 if lv["major"] else 1)
        notable = bool(crossed)
        d = date.fromisoformat(last)
        lines = [f"📅 <b>Daily close {d.strftime('%a %d %b')}: {usd(c)}</b> ({pct((c / pc - 1) * 100)})"]
        lines += crossed
        r_now, r_prev = now_i["rsi14d"], prev_i["rsi14d"]
        if r_now is not None:
            lo, hi = self.a.get("rsi_low", 30), self.a.get("rsi_high", 70)
            tag = ""
            if r_prev is not None and r_now < lo <= r_prev:
                tag, notable = f" ⚠️ entered oversold (<{lo})", True
            elif r_prev is not None and r_now > hi >= r_prev:
                tag, notable = f" ⚠️ entered overbought (>{hi})", True
            elif r_prev is not None and r_now > lo >= r_prev:
                tag, notable = f" back above {lo}", True
            lines.append(f"Daily RSI {r_now:.1f}{tag}")
        a, b = now_i["sma50d"], now_i["sma200d"]
        ap, bp = prev_i["sma50d"], prev_i["sma200d"]
        if None not in (a, b, ap, bp) and (a > b) != (ap > bp):
            lines.append("✨ <b>Golden cross</b> (50D SMA over 200D SMA)" if a > b else "💀 <b>Death cross</b> (50D SMA under 200D SMA)")
            notable = True
            score += 3 if a > b else -3
        pos = []
        for key in ("sma50d", "sma200d", "sma50w"):
            v = now_i.get(key)
            if v:
                pos.append(f"{DYNAMIC_LABELS[key]} {usd(v)} {'▲' if c >= v else '▼'}")
        if pos:
            lines.append(" · ".join(pos))
        if score:
            lines.append(self.read_line("bull" if score > 0 else "bear", "close-based"))
        else:
            lines.append(self.read_line())
        self.emit("\n".join(lines), "normal" if notable else "low")
        if d.weekday() == 6:
            self.weekly_report(rows, now_i)

    def weekly_report(self, rows, ind):
        wk = weekly_completed(rows)
        if len(wk) < 2:
            return
        (wd, c), (_, pc) = wk[-1], wk[-2]
        chg = (c / pc - 1) * 100
        streak, sign = 0, (c >= pc)
        for i in range(len(wk) - 1, 0, -1):
            if (wk[i][1] >= wk[i - 1][1]) == sign:
                streak += 1
            else:
                break
        lines = [f"🗓 <b>WEEKLY CLOSE: {usd(c)}</b> ({pct(chg)}) · {streak} {'green' if sign else 'red'} week{'s' if streak > 1 else ''} in a row"]
        prev_ind = indicators([r for r in rows if r[0] <= wk[-2][0]])
        log_bits = []
        wscore = (1 if chg > 0 else -1) * (3 if abs(chg) >= 8 else 2 if abs(chg) >= 4 else 1)
        for key in ("sma50w", "ema50w", "sma200w"):
            v, vp = ind.get(key), prev_ind.get(key)
            if not v:
                continue
            side = "ABOVE" if c >= v else "BELOW"
            flip = vp is not None and ((pc >= vp) != (c >= v))
            wscore += (1 if c >= v else -1) * (3 if flip else 1)
            lines.append(f"{'🔁 ' if flip else ''}{side} {DYNAMIC_LABELS[key]} {usd(v)} ({pct((c / v - 1) * 100)})"
                         + (" ← flipped this week" if flip else ""))
            log_bits.append(f"{DYNAMIC_LABELS[key]} {usd(v)} {side.lower()}")
        nxt = self.next_levels(c)
        if nxt:
            lines.append(nxt)
        wsig = "bull" if wscore >= 2 else "bear" if wscore <= -2 else "neutral"
        lines.append(self.read_line(wsig, "weekly close"))
        self.emit("\n".join(lines), "high")
        if self.weekly_log:
            new = not os.path.exists(self.weekly_log)
            with open(self.weekly_log, "a") as f:
                if new:
                    f.write("| Week ending | Close | Change | Notes |\n|---|---|---|---|\n")
                f.write(f"| {wd} | {usd(c)} | {pct(chg)} | {'; '.join(log_bits)} |\n")

    def weekly_close_warning(self):
        hours = self.a.get("weekly_close_warning_hours", 2)
        if not hours:
            return
        days_ahead = (7 - self.now.weekday()) % 7 or 7
        close = (self.now + timedelta(days=days_ahead)).replace(hour=0, minute=0, second=0, microsecond=0)
        mins = (close - self.now).total_seconds() / 60
        key = close.date().isoformat()
        if 0 < mins <= hours * 60 and self.st["weekly_warn_sent"] != key:
            self.st["weekly_warn_sent"] = key
            lines = [f"⏳ <b>Weekly close in ~{mins / 60:.1f}h</b> ({self.fmt_time(close)}). BTC {usd(self.price)}"]
            for k in ("sma50w", "ema50w"):
                v = self.live.get(k)
                if v:
                    lines.append(f"{'▲' if self.price >= v else '▼'} {DYNAMIC_LABELS[k]} {usd(v)} ({pct((self.price / v - 1) * 100)})")
            lines.append(self.next_levels(self.price))
            lines.append(self.read_line())
            self.emit("\n".join(lines), "normal")

    # -- sentiment
    def check_sentiment(self):
        if self.ts - self.st["sentiment_ts"] < self.a.get("sentiment_interval_min", 60) * 60:
            return
        self.st["sentiment_ts"] = self.ts
        s = self.st["sentiment"]
        try:
            v, cls = fear_greed()
            s["fng"], s["fng_cls"] = v, cls
            zone = "extreme_fear" if v <= self.a.get("fng_extreme_fear", 20) else "extreme_greed" if v >= self.a.get("fng_extreme_greed", 80) else "neutral"
            if zone != self.st["fng_zone"] and self.st["fng_zone"] is not None and zone != "neutral":
                fear = zone == "extreme_fear"
                self.emit(f"{'😱' if fear else '🤑'} <b>Fear & Greed hit {v} ({esc(cls)})</b>. "
                          + ("Historically a contrarian buy zone, but not a timing signal on its own." if fear
                             else "Crowded optimism; watch for long squeezes.")
                          + "\n" + self.read_line("bull" if fear else "bear", "contrarian"), "normal")
            self.st["fng_zone"] = zone
        except Exception as e:
            log("fear&greed failed", e)
        try:
            f8, oi = perp_funding()
            s["funding_8h"], s["oi"] = f8, oi
            bps = f8 * 100
            hi = self.a.get("funding_high_bps", self.a.get("funding_high_pct", 0.05) * 100)
            lo = self.a.get("funding_low_bps", self.a.get("funding_low_pct", -0.02) * 100)
            zone = "high" if bps >= hi else "negative" if bps <= lo else "normal"
            if zone != self.st["funding_zone"] and self.st["funding_zone"] is not None and zone != "normal":
                high = zone == "high"
                self.emit(f"💸 <b>Perp funding {fmt_funding(f8)}</b>\n"
                          + ("Longs paying up: crowded long, squeeze risk down." if high
                             else "Shorts paying: crowded short, squeeze fuel up.")
                          + "\n" + self.read_line("bear" if high else "bull", "positioning"), "normal")
            self.st["funding_zone"] = zone
        except Exception as e:
            log("funding failed", e)

    # -- events
    def events(self):
        now = self.now
        for ev in self.cfg.get("events", []):
            at = parse_dt(ev["at"])
            key = f"{ev['name']}|{at.isoformat()}"
            leads = sorted(ev.get("remind_minutes", [1440, 60]), reverse=True)
            sent = self.st["events_sent"].setdefault(key, [])
            if now < at:
                due = [l for l in leads if now >= at - timedelta(minutes=l)]
                if due:
                    l = min(due)
                    if l not in sent:
                        sent.extend(x for x in leads if x >= l and x not in sent)
                        when = "tomorrow" if l >= 720 else f"in {round((at - now).total_seconds() / 60)} min" if l < 120 else f"in ~{round((at - now).total_seconds() / 3600)}h"
                        msg = f"⏰ <b>{esc(ev['name'])}</b> {when}: {self.fmt_time(at)} UK"
                        if ev.get("note"):
                            msg += f"\n<i>{esc(ev['note'])}</i>"
                        if self.price:
                            msg += f"\nBTC now {usd(self.price)}\n" + self.read_line()
                        self.emit(msg, "high" if ev.get("importance") == "high" and l <= 60 else "normal")
            react = ev.get("reaction_minutes", 30)
            if react and "reaction" not in sent and at + timedelta(minutes=react) <= now < at + timedelta(hours=3):
                sent.append("reaction")
                pre = self.price_at(at.timestamp(), tolerance=1500)
                if pre and self.price:
                    chg = (self.price / pre - 1) * 100
                    sig = "neutral" if abs(chg) < 0.5 else "bull" if chg > 0 else "bear"
                    self.emit(f"📊 <b>{esc(ev['name'])}: market reaction</b>\nBTC {usd(pre)} → {usd(self.price)} ({pct(chg)}) "
                              f"in the {react} min after\n" + self.next_levels(self.price) + "\n"
                              + self.read_line(sig, "muted reaction" if sig == "neutral" else "event reaction"), "normal")
        # prune old
        for k in list(self.st["events_sent"]):
            try:
                if parse_dt(k.split("|", 1)[1]) < now - timedelta(days=3):
                    self.st["events_sent"].pop(k)
            except Exception:
                self.st["events_sent"].pop(k, None)

    def upcoming(self, days=7):
        evs = []
        for ev in self.cfg.get("events", []):
            at = parse_dt(ev["at"])
            if self.now <= at <= self.now + timedelta(days=days):
                evs.append((at, ev))
        return [f"• {self.local(at).strftime('%a %d %b %H:%M')}: {esc(ev['name'])}" for at, ev in sorted(evs, key=lambda x: x[0])]

    # -- news
    def news(self):
        ncfg = self.cfg.get("news", {})
        if not ncfg.get("enabled", True):
            return
        if self.ts - self.st["news_ts"] < ncfg.get("interval_min", 10) * 60:
            return
        self.st["news_ts"] = self.ts
        kw = ncfg.get("keywords", {})
        relevance = [re.compile(p, re.I) for p in ncfg.get("relevance", [])]
        max_age = timedelta(hours=ncfg.get("max_age_hours", 8))
        picked = []
        recent = self.st["news_recent"]
        for feed in ncfg.get("feeds", []):
            name, url = feed["name"], feed["url"]
            try:
                items = parse_feed(http_get(url, timeout=12))
            except Exception as e:
                log("feed failed", name, e)
                continue
            first = not self.st["news_init"].get(name)
            for it in items:
                link = it["link"] or it["title"]
                if link in self.st["news_seen"]:
                    continue
                self.st["news_seen"][link] = self.ts
                if first:
                    continue
                if it["published"] and self.now - it["published"] > max_age:
                    continue
                text = it["title"] + " " + it["summary"]
                if feed.get("require_relevance", True) and relevance and not any(r.search(text) for r in relevance):
                    continue
                if feed.get("title_filter") and not re.search(feed["title_filter"], it["title"], re.I):
                    continue
                score, hits = score_news(it["title"], it["summary"], kw)
                score += feed.get("bonus", 0)
                if score < ncfg.get("min_score", 3):
                    continue
                tw = title_words(it["title"])
                if any(similar(tw, set(r[1])) for r in recent) or any(similar(tw, title_words(p["title"])) for p in picked):
                    continue
                it["score"], it["source"] = score, name
                picked.append(it)
            self.st["news_init"][name] = True
        # prune
        cutoff = self.ts - 7 * 86400
        self.st["news_seen"] = {k: v for k, v in self.st["news_seen"].items() if v > cutoff}
        if not picked:
            return
        picked.sort(key=lambda x: -x["score"])
        picked = picked[: ncfg.get("max_per_run", 4)]
        for it in picked:
            recent.append([self.ts, sorted(title_words(it["title"]))])
        self.st["news_recent"] = [r for r in recent if r[0] > self.ts - 48 * 3600][-60:]
        hi = ncfg.get("high_score", 6)
        tone_cfg = ncfg.get("tone", {})
        lines = ["📰 <b>BTC-relevant news</b>"]
        for it in picked:
            flag = "🔥" if it["score"] >= hi else "•"
            tone = news_tone(it["title"], tone_cfg.get("bullish"), tone_cfg.get("bearish"))
            lines.append(f"{flag}{self.ICON[tone]} <a href=\"{esc(it['link'])}\">{esc(it['title'])}</a> <i>({esc(it['source'])})</i>")
        if self.price:
            lines.append(f"BTC {usd(self.price)}")
            lines.append(self.read_line())
        self.emit("\n".join(lines), "normal" if max(i["score"] for i in picked) >= hi else "low")

    # -- status / digest
    def status_text(self, title="📊 <b>BTC status</b>"):
        p = self.price
        lines = [title, f"<b>{usd(p)}</b>"]
        o = day_open_24h() if not self.cfg.get("_offline") else None
        if o is None:
            o = self.price_at(self.ts - 86400, tolerance=3600)
        if o:
            lines[-1] += f" (24h {pct((p / o - 1) * 100)})"
        s = self.st["sentiment"]
        extra = []
        if "fng" in s:
            extra.append(f"F&G {s['fng']} {esc(s.get('fng_cls', ''))}")
        if "funding_8h" in s:
            extra.append(f"funding {fmt_funding(s['funding_8h'])}")
        if self.live.get("rsi14d"):
            extra.append(f"daily RSI {self.live['rsi14d']:.0f}")
        if extra:
            lines.append(" · ".join(extra))
        bs = self.bias_summary()
        if bs:
            lines += ["", bs]
        lines.append("")
        lines.append("<b>Moving averages</b>")
        for key in ("sma50d", "ema50d", "sma200d", "sma50w", "ema50w", "sma200w", "avwap_ytd"):
            v = self.live.get(key)
            if v:
                lines.append(f"{'▲' if p >= v else '▼'} {DYNAMIC_LABELS[key]} {usd(v)} ({pct((p / v - 1) * 100)})")
        lines.append("")
        lines.append("<b>Nearest levels</b>")
        lines.append(self.next_levels(p))
        up = self.upcoming(7)
        if up:
            lines += ["", "<b>Next 7 days</b>"] + up
        if self.muted():
            lines.append(f"\n🔕 muted until {self.fmt_time(datetime.fromtimestamp(self.st['mute_until'], UTC))}")
        return "\n".join(lines)

    def digest(self):
        dcfg = self.cfg.get("digest", {})
        if not dcfg.get("enabled", True):
            return
        loc = self.local()
        if loc.hour < dcfg.get("hour", 8) or self.st["last_digest"] == loc.date().isoformat():
            return
        if loc.hour > dcfg.get("hour", 8) + 3:  # don't send a stale brief late in the day
            self.st["last_digest"] = loc.date().isoformat()
            return
        self.st["last_digest"] = loc.date().isoformat()
        self.emit(self.status_text(f"☀️ <b>BTC morning brief</b> · {loc.strftime('%a %d %b')}"), "low")

    # -- commands
    def commands(self):
        chat = str(self.tg.chat_id)
        for u in self.tg.updates(self.st["tg_offset"] + 1 if self.st["tg_offset"] else 0):
            self.st["tg_offset"] = max(self.st["tg_offset"], u["update_id"])
            m = u.get("message") or {}
            if str(m.get("chat", {}).get("id")) != chat:
                continue
            text = (m.get("text") or "").strip()
            if text.startswith("/"):
                self.tg.send(self.handle_command(text))

    def handle_command(self, text):
        parts = text.split()
        cmd = parts[0].split("@")[0].lower()
        args = parts[1:]
        if cmd in ("/status", "/s", "/price"):
            return self.status_text()
        if cmd == "/levels":
            lv = sorted(self.levels(), key=lambda l: -l["price"])
            rows = []
            marked = False
            for l in lv:
                if not marked and l["price"] < self.price:
                    rows.append(f"<b>━━ {usd(self.price)} now ━━</b>")
                    marked = True
                tag = "⭐" if l["id"].startswith("u:") else "〰️" if l.get("dynamic") else ("●" if l["major"] else "○")
                rows.append(f"{tag} {usd(l['price'])} {esc(l['name'])} ({pct((l['price'] / self.price - 1) * 100)})")
            if not marked:
                rows.append(f"<b>━━ {usd(self.price)} now ━━</b>")
            return "<b>Tracked levels</b> (● major ○ minor 〰️ moving ⭐ yours)\n" + "\n".join(rows)
        if cmd == "/add":
            try:
                p = float(args[0].replace(",", "").replace("$", "").lower().replace("k", "000"))
            except Exception:
                return "Usage: /add 80000 optional note"
            name = " ".join(args[1:]) or "Your level"
            self.st["user_levels"] = [l for l in self.st["user_levels"] if float(l["price"]) != p]
            self.st["user_levels"].append({"price": p, "name": name})
            self.st["sides"][f"u:{p}"] = "above" if self.price >= p else "below"
            return f"⭐ Added {usd(p)}: {esc(name)} ({pct((p / self.price - 1) * 100)} from here)"
        if cmd in ("/del", "/remove"):
            try:
                p = float(args[0].replace(",", "").replace("$", "").lower().replace("k", "000"))
            except Exception:
                return "Usage: /del 80000 (only levels you added with /add)"
            before = len(self.st["user_levels"])
            self.st["user_levels"] = [l for l in self.st["user_levels"] if float(l["price"]) != p]
            return f"Removed {usd(p)}" if len(self.st["user_levels"]) < before else f"No level of yours at {usd(p)}. Config levels live in config.toml."
        if cmd == "/events":
            up = self.upcoming(45)
            return "<b>Upcoming events</b>\n" + ("\n".join(up) if up else "Nothing in the calendar. Add events in config.toml.")
        if cmd == "/mute":
            try:
                h = float(args[0]) if args else 8
            except ValueError:
                h = 8
            self.st["mute_until"] = self.ts + h * 3600
            return f"🔕 Muted for {h:g}h (only fast moves ≥ big threshold, weekly close and high-priority event alerts get through). /unmute to undo."
        if cmd == "/unmute":
            self.st["mute_until"] = 0
            return "🔔 Unmuted."
        return ("<b>Commands</b>\n/status: price, MAs, levels, next events\n/levels: every tracked level\n"
                "/add 80000 note: add your own level\n/del 80000: remove it\n/events: upcoming calendar\n"
                "/mute 6: quiet for 6h · /unmute")

    # -- main
    def refresh_history(self):
        today = self.now.date().isoformat()
        if self.st["history_date"] == today and self.st["history"]:
            return
        if self.now.hour == 0 and self.now.minute < 2:
            return  # give the exchange a minute to finalise yesterday's candle
        try:
            rows = fetch_daily_history(now=self.now)
            if rows:
                self.st["history"] = rows[-1500:]
                self.st["history_date"] = today
                log("history refreshed:", len(rows), "days, last", rows[-1][0])
        except Exception as e:
            log("history refresh failed", e)

    def run(self):
        try:
            self.price, src = spot_price()
        except Exception as e:
            self.st["price_failures"] += 1
            log("no price:", e)
            if self.st["price_failures"] == self.a.get("warn_after_failures", 6):
                self.tg.send("⚠️ BTC alert bot: price feeds failing for the last few runs. Will keep retrying.")
            return
        self.st["price_failures"] = 0
        log("price", self.price, src)
        samples = self.st["samples"]
        samples.append([self.ts, self.price])
        self.st["samples"] = [s for s in samples if s[0] > self.ts - 26 * 3600]

        self.refresh_history()
        self.live = indicators(self.st["history"], live=self.price,
                               avwap_anchor=self.cfg.get("dynamic_levels", {}).get("avwap_anchor")) if self.st["history"] else {}

        self.commands()
        self.check_levels()
        self.check_moves()
        self.close_reports()
        self.weekly_close_warning()
        self.check_sentiment()
        self.events()
        self.news()
        self.digest()
        self.st["last_price"] = self.price
        self.flush()


# --------------------------------------------------------------------------- CLI

def main():
    ap = argparse.ArgumentParser(description="BTC -> Telegram alert bot")
    ap.add_argument("--config", default=os.path.join(HERE, "config.toml"))
    ap.add_argument("--state", default=os.path.join(HERE, ".state", "state.json"))
    ap.add_argument("--weekly-log", default=os.path.join(HERE, "weekly_log.md"))
    ap.add_argument("--dry-run", action="store_true", help="print messages instead of sending")
    ap.add_argument("--loop", type=int, default=0, help="run forever, checking every N seconds")
    ap.add_argument("--test", action="store_true", help="send a test message and exit")
    ap.add_argument("--find-chat-id", action="store_true", help="print chat ids that messaged your bot")
    args = ap.parse_args()

    with open(args.config, "rb") as f:
        cfg = tomllib.load(f)
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    tg = Telegram(token, chat, dry=args.dry_run)

    if args.find_chat_id:
        if not token:
            sys.exit("Set TELEGRAM_BOT_TOKEN first, then send your bot any message.")
        res = tg._call("getUpdates", {"timeout": 0})
        ids = {(m["message"]["chat"]["id"], m["message"]["chat"].get("first_name", "")) for m in res.get("result", []) if "message" in m}
        print("\n".join(f"chat id: {i}  ({n})" for i, n in ids) or "No messages yet. Send your bot a message in Telegram, then rerun.")
        return
    if args.test:
        ok = tg.send("✅ BTC alert bot connected. Send /help for commands.")
        sys.exit(0 if ok else 1)
    if not args.dry_run and not (token and chat):
        sys.exit("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set (or use --dry-run)")

    while True:
        st = load_state(args.state)
        bot = Bot(cfg, st, tg, weekly_log=args.weekly_log)
        try:
            bot.run()
        except Exception as e:
            log("run error:", repr(e))
            bot.flush()
        save_state(args.state, st)
        if not args.loop:
            break
        time.sleep(args.loop)


if __name__ == "__main__":
    main()
