"""Chart indicators for the research agent, from daily prices. Pure math, no network."""
import statistics


def sma(xs: list[float], n: int) -> float:
    return sum(xs[-n:]) / n


def ema_series(xs: list[float], n: int) -> list[float]:
    k = 2 / (n + 1)
    out = [xs[0]]
    for x in xs[1:]:
        out.append(out[-1] + k * (x - out[-1]))
    return out


def rsi(closes: list[float], n: int = 14) -> float:
    """Wilder's RSI, 0-100. Under 30 = oversold, over 70 = overbought."""
    moves = [b - a for a, b in zip(closes, closes[1:])]
    gain = sum(max(m, 0) for m in moves[:n]) / n
    loss = sum(max(-m, 0) for m in moves[:n]) / n
    for m in moves[n:]:
        gain = (gain * (n - 1) + max(m, 0)) / n
        loss = (loss * (n - 1) + max(-m, 0)) / n
    if loss == 0:
        return 100.0
    return 100 - 100 / (1 + gain / loss)


def macd(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9, recent: int = 3) -> dict:
    """MACD line, signal line, and whether the line crossed above the signal in the last `recent` days."""
    line = [a - b for a, b in zip(ema_series(closes, fast), ema_series(closes, slow))]
    sig = ema_series(line, signal)
    crossed = any(line[i - 1] <= sig[i - 1] and line[i] > sig[i] for i in range(len(line) - recent, len(line)))
    return {"line": line[-1], "signal": sig[-1], "above": line[-1] > sig[-1], "crossed_up": crossed}


def atr_pct(highs: list[float], lows: list[float], closes: list[float], n: int = 14) -> float:
    """Average true range as a share of the price: how much it typically moves in a day."""
    trs = [max(h - lo, abs(h - pc), abs(lo - pc)) for h, lo, pc in zip(highs[1:], lows[1:], closes[:-1])]
    atr = sum(trs[:n]) / n
    for tr in trs[n:]:
        atr = (atr * (n - 1) + tr) / n
    return atr / closes[-1]


def bollinger(closes: list[float], n: int = 20, k: float = 2.0) -> tuple[float, float, float]:
    window = closes[-n:]
    mid = sum(window) / n
    sd = statistics.pstdev(window)
    return mid - k * sd, mid, mid + k * sd


def squeeze_breakout(closes: list[float], n: int = 20, lookback: int = 120, recent: int = 10) -> dict:
    """A 'squeeze' is when the bands are unusually narrow (a quiet stock); a breakout is the
    price closing above the upper band soon after. Also reports how far above the band it is."""
    widths = []
    for i in range(len(closes) - lookback, len(closes) + 1):
        lo, mid, up = bollinger(closes[:i], n)
        widths.append((up - lo) / mid)
    cutoff = sorted(widths)[len(widths) // 5]                 # the narrowest 20% of the last ~6 months
    squeeze = min(widths[-recent:]) <= cutoff
    lo, mid, up = bollinger(closes, n)
    broke_out = any(closes[i - 1] > bollinger(closes[:i], n)[2] for i in range(len(closes) - 2, len(closes) + 1))
    return {"squeeze": squeeze, "breakout": squeeze and broke_out, "upper": up,
            "above_upper": closes[-1] / up - 1}


def resistance_room(highs: list[float], price: float, lookback: int = 252, pivot: int = 5, skip: int = 5) -> float | None:
    """Room (as a share of the price) up to the nearest old high above today's price, where
    an old high is a peak the stock turned down from in the past year. None = no old high
    above (the stock is at new highs, so nothing overhead)."""
    start = max(pivot, len(highs) - lookback)
    peaks = [highs[i] for i in range(start, len(highs) - max(skip, pivot))
             if highs[i] == max(highs[i - pivot: i + pivot + 1])]
    above = [p for p in peaks if p > price]
    return min(above) / price - 1 if above else None
