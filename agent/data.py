"""Market data from Yahoo Finance's chart endpoint (daily bars, CAD)."""
import math
import statistics

import requests

URL = "https://query1.finance.yahoo.com/v8/finance/chart/{t}?range=2y&interval=1d"
HEADERS = {"User-Agent": "Mozilla/5.0"}


class DataError(RuntimeError):
    pass


def fetch(ticker: str, currency: str = "CAD") -> dict:
    """Return {"price": last traded price, "closes": adjusted daily closes,
    "dollar_volume_20d": average daily traded value over the last 20 days}."""
    r = requests.get(URL.format(t=ticker), headers=HEADERS, timeout=30)
    r.raise_for_status()
    chart = r.json()["chart"]
    if chart.get("error") or not chart.get("result"):
        raise DataError(f"{ticker}: {chart.get('error')}")
    res = chart["result"][0]
    if res["meta"].get("currency") != currency:
        raise DataError(f"{ticker}: expected {currency}, got {res['meta'].get('currency')}")
    try:
        closes = [c for c in res["indicators"]["adjclose"][0]["adjclose"] if c]
        quote = res["indicators"]["quote"][0]
        traded = [c * v for c, v in zip(quote["close"][-20:], quote["volume"][-20:]) if c and v]
    except (KeyError, IndexError, TypeError):
        raise DataError(f"{ticker}: incomplete data")
    price = res["meta"].get("regularMarketPrice")
    if not price or len(closes) < 260:
        raise DataError(f"{ticker}: not enough data ({len(closes)} bars, price={price})")
    return {
        "price": float(price),
        "closes": closes,
        "dollar_volume_20d": sum(traded) / len(traded) if traded else 0.0,
    }


def signals(closes: list[float]) -> dict:
    """Trend / momentum / risk numbers the strategy and Claude both see."""
    last = closes[-1]

    def ret(days):
        return last / closes[-1 - days] - 1

    daily = [closes[i] / closes[i - 1] - 1 for i in range(len(closes) - 60, len(closes))]
    moves = daily[-14:]
    gain = sum(m for m in moves if m > 0) / 14
    loss = -sum(m for m in moves if m < 0) / 14
    return {
        "ret_5d": ret(5),
        "ret_1m": ret(21),
        "ret_3m": ret(63),
        "ret_6m": ret(126),
        "ret_12m": ret(252),
        "above_50d_avg": last > statistics.fmean(closes[-50:]),
        "above_200d_avg": last > statistics.fmean(closes[-200:]),
        "vol_60d_annual": statistics.stdev(daily) * math.sqrt(252),
        "drawdown_from_20d_high": last / max(closes[-20:]) - 1,
        "drawdown_from_1y_high": last / max(closes[-252:]) - 1,
        "rsi_14": 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss),  # 0-100; under 30 = oversold
    }


def fx_rates(currencies) -> dict:
    """CAD value of one unit of each currency (CAD is 1)."""
    out = {"CAD": 1.0}
    if "USD" in set(currencies):
        out["USD"] = fetch("CAD=X")["price"]  # Yahoo's USD/CAD rate, priced in CAD
    return out


def snapshot(tickers: list[str], skip_missing: bool = False, currencies: dict | None = None) -> dict:
    """{ticker: {"price", "dollar_volume_20d", "signals"}} for the universe.
    With skip_missing, tickers whose data can't be loaded are left out (with a
    warning) instead of stopping the run."""
    out = {}
    for t in tickers:
        try:
            d = fetch(t, (currencies or {}).get(t, "CAD"))
        except (DataError, requests.RequestException) as e:
            if not skip_missing:
                raise DataError(str(e)) from e
            print(f"  (skipping {t}: {e})")
            continue
        out[t] = {"price": d["price"], "dollar_volume_20d": d["dollar_volume_20d"], "signals": signals(d["closes"])}
    return out
