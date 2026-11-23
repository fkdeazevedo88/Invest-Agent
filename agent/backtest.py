"""Replay the high-risk strategies on past prices, one rebalance per week.

    python run.py backtest --config config-highrisk.yaml --strategy all

Uses the same rules as the live agent (strategy.baseline), with these simplifications:
fractional shares, trades at the weekly closing price minus `limit_slippage`, USD stocks
valued in CAD at each day's exchange rate, no Claude news check. The stock list was chosen
today, from companies that still exist, so results look better than reality would have been.
"""
import datetime as dt
import json
import time
from pathlib import Path

import numpy as np
import requests

from agent import data, strategy
from agent.brokers import currency_of

URL = "https://query1.finance.yahoo.com/v8/finance/chart/{t}?range=5y&interval=1d"
ALL = ["momentum", "breakout", "pullback", "sector_rotation", "leveraged_trend", "combo"]
STEP, WARMUP = 5, 260


def download(ticker: str) -> dict:
    r = requests.get(URL.format(t=ticker), headers=data.HEADERS, timeout=30)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    adj = res["indicators"]["adjclose"][0]["adjclose"]
    out = {}
    for ts, a, c, v in zip(res["timestamp"], adj, q["close"], q["volume"]):
        if a:
            out[dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%d")] = (a, (c or 0) * (v or 0))
    return out


def load_all(cfg: dict, cache: Path | None) -> dict:
    """Daily history for the universe plus benchmarks and USD/CAD, cached for a day."""
    if cache and cache.exists() and time.time() - cache.stat().st_mtime < 86400:
        return json.loads(cache.read_text())
    tickers = [u.get("signal") or u["ticker"] for u in cfg["universe"]] + ["XIU.TO", "QQQ", "CAD=X"]
    raw = {}
    for t in dict.fromkeys(tickers):
        try:
            raw[t] = download(t)
        except Exception as e:  # a missing ticker shouldn't stop the comparison
            print(f"  (skipping {t}: {e})")
    if cache:
        cache.write_text(json.dumps(raw))
    return raw


def align(raw: dict, cfg: dict) -> tuple[list, dict, np.ndarray]:
    """Put every series on the TSX trading calendar, carrying the last price forward."""
    dates = sorted(raw["XIU.TO"])

    def series(t):
        a, dv, last = np.full(len(dates), np.nan), np.zeros(len(dates)), (np.nan, 0.0)
        src = raw.get(t, {})
        for i, d in enumerate(dates):
            last = src.get(d, (last[0], 0.0))
            a[i], dv[i] = last
        return a, dv

    fx = series("CAD=X")[0]
    fx = np.where(np.isnan(fx), np.nanmean(fx), fx)
    info = {}
    for u in cfg["universe"]:
        src = u.get("signal") or u["ticker"]          # a CDR replays its US stock (currency-hedged)
        if src in raw:
            a, dv = series(src)
            info[u["ticker"]] = {"adj": a, "dv": dv, "currency": currency_of(u), "signal": u.get("signal"),
                                 "sector": u.get("sector"), "kind": u.get("kind", "stock")}
    for t, c in (("XIU.TO", "CAD"), ("QQQ", "USD")):
        a, dv = series(t)
        info["__" + t] = {"adj": a, "dv": dv, "currency": c}
    return dates, info, fx


def snapshot_at(i: int, info: dict, fx: np.ndarray) -> dict:
    snap = {}
    for t, s in info.items():
        if t.startswith("__"):
            continue
        hist = s["adj"][: i + 1]
        hist = hist[~np.isnan(hist)]
        if len(hist) < WARMUP or np.isnan(s["adj"][i]):
            continue
        rate = fx[i] if s["currency"] == "USD" else 1.0
        snap[t] = {
            "price": float(s["adj"][i]), "price_cad": float(s["adj"][i] * rate),
            "dollar_volume_20d": float(s["dv"][max(0, i - 19): i + 1].mean()),
            "dollar_volume_cad": float(s["dv"][max(0, i - 19): i + 1].mean() * rate),
            "signals": data.signals(list(hist)), "currency": s["currency"],
            "sector": s["sector"], "kind": s["kind"], "signal": s.get("signal"),
        }
    return snap


def value_cad(s: dict, i: int, fx: np.ndarray) -> float:
    return s["adj"][i] * (fx[i] if s["currency"] == "USD" else 1.0)


def simulate(cfg: dict, dates: list, info: dict, fx: np.ndarray, start: int = WARMUP, end: int | None = None,
             step: int = STEP) -> dict:
    """Trade every `step` trading days (5 = weekly, 1 = daily) from day index `start` to `end`
    (history before `start` feeds the signals)."""
    cost = cfg["risk"]["limit_slippage"]
    end = len(dates) if end is None else end
    weights, equity, curve, weekly, invested, turnover_sum = {}, 1.0, [1.0], [], [], 0.0
    first = start
    for i in range(first, end - step, step):
        snap = snapshot_at(i, info, fx)
        new = strategy.sanitize(strategy.baseline(snap, cfg, held=set(weights)), cfg, set(snap))
        turnover = sum(abs(new.get(t, 0) - weights.get(t, 0)) for t in set(new) | set(weights))
        turnover_sum += turnover
        equity *= 1 - turnover * cost
        weights = new
        j = i + step
        r = sum(w * (value_cad(info[t], j, fx) / value_cad(info[t], i, fx) - 1) for t, w in weights.items())
        equity *= 1 + r
        weekly.append(r)
        curve.append(equity)
        invested.append(sum(w for t, w in weights.items() if t != cfg.get("cash_ticker")))   # cash ETF isn't "invested"
    return metrics(curve, weekly, dates[first], dates[min(len(dates) - 1, first + step * len(weekly))],
                   extra={"invested": float(np.mean(invested)) if invested else 0.0,
                          "turnover_per_year": turnover_sum / max(1e-9, len(weekly) * step / 252)}, step=step)


def buy_and_hold(info: dict, key: str, dates: list, fx: np.ndarray, start: int = WARMUP, end: int | None = None) -> dict:
    s = info["__" + key]
    end = len(dates) if end is None else end
    curve, weekly = [1.0], []
    for i in range(start, end - STEP, STEP):
        r = value_cad(s, i + STEP, fx) / value_cad(s, i, fx) - 1
        weekly.append(r)
        curve.append(curve[-1] * (1 + r))
    return metrics(curve, weekly, dates[start], dates[min(len(dates) - 1, start + STEP * len(weekly))],
                   extra={"invested": 1.0, "turnover_per_year": 0.0})


def metrics(curve, weekly, start, end, extra, step: int = STEP) -> dict:
    years = len(weekly) * step / 252 or 1
    peak, worst = 0.0, 0.0
    for v in curve:
        peak = max(peak, v)
        worst = min(worst, v / peak - 1)
    total = curve[-1]
    return {"start": start, "end": end, "years": years, "total": total - 1,
            "per_year": total ** (1 / years) - 1 if total > 0 else -1.0, "max_drop": worst,
            "best_week": max(weekly, default=0), "worst_week": min(weekly, default=0),
            "weeks_up": sum(r > 0 for r in weekly) / max(1, len(weekly)), **extra}


def table(rows: list, capital: float) -> str:
    """rows: (name, whole-period metrics, first-half metrics, second-half metrics)."""
    out = [f"{'strategy':<19}{'total':>8}{'per year':>10}{'worst drop':>12}{'worst week':>12}{'invested':>10}"
           f"{'1st half':>10}{'2nd half':>10}   ${capital:,.0f} became"]
    for name, m, h1, h2 in rows:
        out.append(f"{name:<19}{m['total']*100:>+7.0f}%{m['per_year']*100:>+9.1f}%{m['max_drop']*100:>11.0f}%"
                   f"{m['worst_week']*100:>+11.1f}%{m['invested']*100:>9.0f}%{h1['total']*100:>+9.0f}%{h2['total']*100:>+9.0f}%"
                   f"   ${capital * (1 + m['total']):,.0f}")
    return "\n".join(out)


ETF_ALL = ["etf_trend", "pullback"]


def main(cfg: dict, which: str | None, cache_dir: Path | None = None):
    every = ETF_ALL if cfg.get("cash_ticker") else ALL
    names = every if which in (None, "all") else [which]
    for n in names:
        if n not in strategy.STRATEGIES:
            raise SystemExit(f"unknown strategy {n!r}; choose from {', '.join(every)} or all")
    cache_dir = cache_dir or Path(__file__).resolve().parent.parent / cfg.get("state_dir", "state")
    cache_dir.mkdir(exist_ok=True)
    print("Loading 5 years of prices (cached for a day)...")
    dates, info, fx = align(load_all(cfg, cache_dir / "backtest_prices.json"), cfg)
    mid = WARMUP + (len(dates) - WARMUP) // 2
    capital = 1000.0
    # A weekly strategy's result can depend a lot on which weekday it trades. Replay each one
    # starting on all 5 weekdays and report the median, the range, and the worst drop.
    print("  (each strategy is replayed 5 times, once per weekday; this takes a few minutes)")
    rows = []
    for n in names:
        print(f"  replaying {n}...")
        c = {**cfg, "strategy": n}
        runs = [simulate(c, dates, info, fx, start=WARMUP + off) for off in range(STEP)]
        second = [simulate(c, dates, info, fx, start=mid + off) for off in range(STEP)]
        rows.append((n, runs, second))
    for label, key in (("hold XIU (TSX)", "XIU.TO"), ("hold QQQ (NASDAQ)", "QQQ")):
        rows.append((label, [buy_and_hold(info, key, dates, fx)], [buy_and_hold(info, key, dates, fx, start=mid)]))
    first = rows[0][1][0]
    print(f"\nWeekly rebalancing, {first['start']} to {first['end']} ({first['years']:.1f} years), in CAD, after trading costs.")
    print(f"'last 2 yrs' = {dates[mid]} to {dates[-1]}.\n")
    med = lambda ms: float(np.median([m["total"] for m in ms]))  # noqa: E731
    print(f"{'strategy':<19}{'total':>8}{'range over weekdays':>24}{'last 2 yrs':>12}{'worst drop':>12}{'invested':>10}   ${capital:,.0f} became")
    for name, runs, second in rows:
        lo, hi = min(m["total"] for m in runs), max(m["total"] for m in runs)
        rng = f"{lo*100:+.0f}% to {hi*100:+.0f}%" if len(runs) > 1 else ""
        print(f"{name:<19}{med(runs)*100:>+7.0f}%{rng:>24}{med(second)*100:>+11.0f}%{min(m['max_drop'] for m in runs)*100:>11.0f}%"
              f"{np.median([m['invested'] for m in runs])*100:>9.0f}%   ${capital * (1 + med(runs)):,.0f}")
    print("\n'total' and 'last 2 yrs' are medians over the 5 weekdays; 'worst drop' is the worst of them.")
    print("A strategy worth trusting has a narrow range and does well in the last 2 years too.")
    print("'invested' = average share of the money in stocks (the rest waited in cash).")
    print("Caution: the stock list was picked today from companies that survived and, for many, soared,")
    print("so these results are flattered. Past results don't predict future ones.")
    print("Fractional shares and no news check assumed.")
    for n in names:
        print(f"\n{n}: {strategy.STRATEGIES[n]}")
    return rows

