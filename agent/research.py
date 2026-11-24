"""The research agent's free signal checker: price data from Yahoo, all signals worked out
in code, and the buy rule. Claude does the news and company research; this decides whether
the chart and the market agree."""
import datetime as dt
from zoneinfo import ZoneInfo

import requests

from agent import data
from agent.indicators import atr_pct, bollinger, macd, resistance_room, rsi, sma, squeeze_breakout

ET = ZoneInfo("America/New_York")
CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{t}?range=2y&interval=1d"
INTRADAY = "https://query1.finance.yahoo.com/v8/finance/chart/{t}?range=1d&interval=1m&includePrePost=true"

POSITIVES = ["rsi_in_range", "above_20d", "above_50d", "macd", "volume", "beats_sp500", "squeeze_breakout", "analyst"]
LABELS = {
    "rsi_in_range": "RSI between 40 and 65", "above_20d": "price above 20-day average",
    "above_50d": "price above 50-day average", "macd": "MACD above or crossing its signal line",
    "volume": "more volume than usual", "beats_sp500": "doing better than the S&P 500",
    "squeeze_breakout": "breaking out of a Bollinger squeeze", "analyst": "recent analyst upgrade or higher target",
}


def history(ticker: str) -> dict:
    """Daily prices (raw, not split-adjusted) for about two years."""
    r = requests.get(CHART.format(t=ticker), headers=data.HEADERS, timeout=30)
    r.raise_for_status()
    chart = r.json()["chart"]
    if chart.get("error") or not chart.get("result"):
        raise data.DataError(f"{ticker}: {chart.get('error')}")
    res = chart["result"][0]
    if res["meta"].get("currency") != "USD":
        raise data.DataError(f"{ticker}: not a US-dollar stock")
    q = res["indicators"]["quote"][0]
    rows = [(t, o, h, lo, c, v) for t, o, h, lo, c, v in
            zip(res["timestamp"], q["open"], q["high"], q["low"], q["close"], q["volume"]) if None not in (o, h, lo, c, v)]
    if len(rows) < 200:
        raise data.DataError(f"{ticker}: not enough price history ({len(rows)} days)")
    days, _, highs, lows, closes, vols = (list(x) for x in zip(*rows))
    return {"days": [dt.datetime.fromtimestamp(t, ET).date() for t in days], "highs": highs, "lows": lows,
            "closes": closes, "volumes": vols, "price": float(res["meta"].get("regularMarketPrice") or closes[-1])}


def latest_price(ticker: str) -> float:
    """The newest trade price, including pre-market and after-hours when available."""
    try:
        r = requests.get(INTRADAY.format(t=ticker), headers=data.HEADERS, timeout=30)
        r.raise_for_status()
        res = r.json()["chart"]["result"][0]
        closes = [c for c in res["indicators"]["quote"][0]["close"] if c]
        if closes:
            return float(closes[-1])
        return float(res["meta"]["regularMarketPrice"])
    except (requests.RequestException, KeyError, IndexError, TypeError, ValueError) as e:
        raise data.DataError(f"{ticker}: no current price ({e})")


def completed(h: dict, now: dt.datetime | None = None) -> dict:
    """Drop today's bar while the US market is still open (its volume is only partial)."""
    now = (now or dt.datetime.now(ET)).astimezone(ET)
    if h["days"][-1] == now.date() and now.time() < dt.time(16, 0):
        return {k: (v[:-1] if isinstance(v, list) else v) for k, v in h.items()}
    return h


def market_check(spy: dict) -> dict:
    closes = completed(spy)["closes"]
    avg50 = sma(closes, 50)
    return {"ok": closes[-1] > avg50, "sp500": closes[-1], "sp500_50d": avg50}


def checklist(ticker: str, h: dict, spy: dict, cfg: dict, analyst_upgrade: bool = False) -> dict:
    """Every signal for one stock, which 'never buy' rules it breaks, and whether it passes."""
    s = cfg["signals"]
    hc, sc = completed(h), completed(spy)
    closes, highs, lows, vols = hc["closes"], hc["highs"], hc["lows"], hc["volumes"]
    price = closes[-1]
    r = rsi(closes)
    m = macd(closes)
    bb = squeeze_breakout(closes)
    atr = atr_pct(highs, lows, closes)
    vol_ratio = vols[-1] / (sum(vols[-21:-1]) / 20)
    stock_1m, stock_3m = price / closes[-22] - 1, price / closes[-64] - 1
    spx_1m, spx_3m = sc["closes"][-1] / sc["closes"][-22] - 1, sc["closes"][-1] / sc["closes"][-64] - 1
    room = resistance_room(highs, price)
    market = market_check(spy)
    positives = {
        "rsi_in_range": s["rsi_min"] <= r <= s["rsi_max"],
        "above_20d": price > sma(closes, 20),
        "above_50d": price > sma(closes, 50),
        "macd": m["above"] or m["crossed_up"],
        "volume": vol_ratio >= s["min_volume_ratio"],
        "beats_sp500": stock_1m > spx_1m and stock_3m > spx_3m,
        "squeeze_breakout": bb["breakout"],
        "analyst": bool(analyst_upgrade),
    }
    blockers = []
    if not market["ok"]:
        blockers.append("the whole market is falling (S&P 500 below its 50-day average)")
    if r > s["rsi_block"]:
        blockers.append(f"RSI {r:.0f} is over {s['rsi_block']}: already went up too much")
    if bb["above_upper"] > s["max_above_upper_band"]:
        blockers.append(f"{bb['above_upper']*100:.1f}% above the top Bollinger band: too stretched")
    if room is not None and room < cfg["take_profit"]:
        blockers.append(f"only {room*100:.1f}% of room before an old high")
    if atr < s["min_atr_pct"]:
        blockers.append(f"moves only {atr*100:.1f}% a day: too slow to reach {cfg['take_profit']*100:.0f}% in 2 weeks")
    if price < cfg["min_price"]:
        blockers.append(f"price ${price:.2f} is under ${cfg['min_price']}")
    score = sum(positives.values())
    return {
        "ticker": ticker, "price": round(price, 2), "rsi": round(r, 1), "macd_crossed_up": m["crossed_up"],
        "volume_ratio": round(vol_ratio, 2), "atr_pct": round(atr * 100, 2),
        "vs_sp500_1m": round((stock_1m - spx_1m) * 100, 1), "vs_sp500_3m": round((stock_3m - spx_3m) * 100, 1),
        "squeeze": bb["squeeze"], "above_upper_band_pct": round(bb["above_upper"] * 100, 1),
        "room_to_old_high_pct": None if room is None else round(room * 100, 1),
        "market_ok": market["ok"], "positives": positives, "score": score, "blockers": blockers,
        "passes": not blockers and score >= s["min_positive"],
        "upper_band": round(bollinger(closes)[2], 2), "as_of": str(hc["days"][-1]),
    }


def explain(c: dict) -> str:
    """The checklist in plain English, one line per signal."""
    lines = [f"{c['ticker']}  ${c['price']} (close of {c['as_of']})  score {c['score']}/{len(POSITIVES)}  "
             + ("PASSES" if c["passes"] else "does not pass")]
    for k in POSITIVES:
        lines.append(f"  {'✅' if c['positives'][k] else '⬜'} {LABELS[k]}")
    for b in c["blockers"]:
        lines.append(f"  ⛔ {b}")
    room = "no old high above" if c["room_to_old_high_pct"] is None else f"{c['room_to_old_high_pct']}% room to an old high"
    lines.append(f"  RSI {c['rsi']} · volume {c['volume_ratio']}x normal · moves {c['atr_pct']}%/day · "
                 f"vs S&P 500: {c['vs_sp500_1m']:+}% (1 mo), {c['vs_sp500_3m']:+}% (3 mo) · {room}")
    return "\n".join(lines)
