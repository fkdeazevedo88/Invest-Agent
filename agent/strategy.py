"""Deciding target weights: a rules baseline, optionally refined by Claude.

Picked by `strategy:` in the config file:
  etf_trend  (default)  diversified ETFs, trend filter + inverse volatility
  HIGH RISK (individual stocks / leveraged ETFs), described in STRATEGIES below:
  momentum, breakout, pullback, sector_rotation, leveraged_trend, combo
"""
import json

ETF_SYSTEM = """You manage a small, long-only Canadian ETF portfolio (about $1,000 CAD) for a retail investor.
You run once a week. Each run you receive market signals, the current holdings, a rules-based
baseline allocation, and your own recent decisions. Reply with target portfolio weights.

How to think about it:
- The goal is steady long-term growth with controlled drawdowns, not short-term trading. You have
  no news feed and no edge on short-term moves, so do not pretend to one.
- The baseline (trend filter + inverse-volatility weights) is a sensible default. Deviate from it
  only with a concrete reason grounded in the data you were given, and keep deviations modest.
- Trading costs matter at this size and positions are whole shares. Prefer leaving the portfolio
  alone over small reshuffles; stay consistent with your recent decisions unless the data changed.
- Stay diversified. Anything not allocated to a risk asset should go to the cash ETF.

Rules enforced by code after you answer (violations are clipped, so respect them): only tickers
from the universe, weights between 0 and the per-position cap (the cash ETF is exempt), and the
weights must sum to at most 1."""

MOMENTUM_SYSTEM = """You manage a small, deliberately HIGH-RISK, long-only sleeve of TSX (CAD) and NASDAQ (USD) stocks for
a Canadian retail investor who chose high risk for a chance at higher returns and accepts large
losses. You receive the candidates that passed the configured strategy's filters (described in
strategy_rules; all liquid, above $2 and affordable), the current holdings, the rules-based baseline
(the strategy's top picks, equal weight), your recent decisions, and news research covering
both the big picture (economy, trade and tariffs, central banks, tensions and politics between
countries, commodities, CAD/USD) and each candidate. Reply with target weights.

How to think about it:
- Follow the strategy's logic (strategy_rules). The baseline is a sound default; deviate
  only for a concrete reason, mainly news: drop a candidate facing a known negative event (fraud
  allegations, a failed deal, a dilutive financing, a guidance cut, a takeover already priced in),
  or one directly exposed to a specific, reported big-picture threat (for example new tariffs on
  its sector or its main export market, sanctions, a ban affecting its business).
- Use only facts from the research that come with a named source. Ignore anything unsourced,
  speculative or vague. Do not invent catalysts or risks. Markets already price known news, so
  big-picture worries justify at most a modest step back (fewer positions, more cash), not a
  bet on a prediction. If the research found nothing material, follow the baseline.
- NASDAQ stocks are bought only with the investor's US-dollar cash; the code handles that.
- Few positions, but not one: concentration is the point, a single bet is not.
- Stay consistent with recent decisions unless prices or news changed; churn costs money.
- Unallocated weight simply stays in cash. Holding cash when few candidates qualify is correct.

Rules enforced by code after you answer (violations are dropped or clipped): only tickers from the
candidate list, weights between 0 and the per-position cap, at most the maximum number of
positions, and weights summing to at most 1."""

RESEARCH_PROMPT = """You are gathering news for a stock-trading decision. Search for REAL, recent news (about the last two
weeks) from established news organizations, official sources and company filings.

Part 1 - the big picture, as it affects Canadian and US stocks:
- trade between countries: tariffs, trade deals and disputes (especially Canada-US and US-China),
  sanctions, export controls
- the economy and central banks: Bank of Canada, US Federal Reserve, interest rates, inflation,
  jobs, recession signs
- geopolitics: wars and conflicts, tensions between countries, elections and major policy changes
- what moves these stocks: oil, natural gas, gold, copper, uranium, chips and AI spending, and the
  Canadian dollar against the US dollar

Part 2 - each of these stocks: {names}
Anything material for the next few weeks: earnings or guidance, financings or share issuance,
mergers or takeovers, legal or regulatory trouble, or how the big-picture news above hits it.

Rules:
- Report only facts you found in a source during this search, each with the source name, the date
  and the link.
- No rumours, opinions presented as facts, social-media posts, or unsourced claims. If you could
  not confirm something in a reputable source, leave it out.
- If nothing material turned up for a stock, write "nothing material found".
- Be brief: one or two lines per item."""

# Web search is limited to these sites so the research uses established news organizations,
# official statistics and central banks, regulators, and company press releases.
NEWS_SOURCES = [
    "reuters.com", "apnews.com", "bloomberg.com", "bnnbloomberg.ca", "cbc.ca", "ctvnews.ca",
    "globalnews.ca", "theglobeandmail.com", "financialpost.com", "cnbc.com", "wsj.com", "ft.com",
    "marketwatch.com", "barrons.com", "economist.com", "bbc.com", "nytimes.com", "axios.com",
    "politico.com", "bankofcanada.ca", "federalreserve.gov", "statcan.gc.ca", "bls.gov", "bea.gov",
    "canada.ca", "ustr.gov", "whitehouse.gov", "eia.gov", "iea.org", "imf.org", "worldbank.org",
    "oecd.org", "sec.gov", "sedarplus.ca", "nasdaq.com", "tsx.com", "globenewswire.com",
    "newswire.ca", "businesswire.com", "prnewswire.com", "mining.com", "kitco.com",
    "world-nuclear-news.org",
]


STRATEGIES = {
    "etf_trend": "Spread the money over the ETFs that are in an uptrend (above the 200-day average, positive "
                 "momentum), giving steadier ones more weight (inverse volatility, max 40% each); the rest "
                 "goes to the cash ETF.",
    "momentum": "Ride the strongest recent winners: hold the stocks with the biggest 1/3/6-month gains "
                "that are above their 50- and 200-day averages; sell one that falls stop_loss below its 20-day high.",
    "breakout": "Buy stocks breaking out to new highs: within near_high of their 52-week high, above the "
                "50-day average and up over 3 months; keep them while they stay within hold_band of the high.",
    "pullback": "Buy the dip in strong stocks: up at least min_6m over 6 months and above the 200-day average, "
                "but recently down dip_min to dip_max from the 20-day high with RSI under max_rsi; sell once "
                "they recover to within `recovered` of the high, or if the dip deepens past dip_max.",
    "sector_rotation": "Bet on the hottest sectors: rank sectors (gold, uranium, chips, software...) by the "
                       "median momentum of their stocks, then hold the strongest per_sector stocks in each of "
                       "the top `sectors` sectors.",
    "leveraged_trend": "Hold leveraged NASDAQ-100 ETFs (QQU 2x, TQQQ 3x) only while they are in an uptrend "
                       "(above 50- and 200-day averages, up over 3 months); otherwise stay in cash.",
    "combo": "Split the money evenly across several of the strategies above (combo.parts).",
}

DEFAULTS = {
    "breakout": {"near_high": 0.03, "hold_band": 0.12},
    "pullback": {"min_6m": 0.10, "dip_min": 0.07, "dip_max": 0.20, "max_rsi": 40, "recovered": 0.02},
    "sector_rotation": {"sectors": 2, "per_sector": 2},
    "leveraged_trend": {"max_positions": 2},
    "combo": {"parts": ["momentum", "pullback"]},
}


def params(cfg: dict, name: str) -> dict:
    return {**DEFAULTS.get(name, {}), **(cfg.get(name) or {})}


FILTER_DEFAULTS = {"min_price": 2.0, "min_dollar_volume": 2_000_000, "stop_loss": 0.15, "max_positions": 4,
                   "research_top": 8}


def filters(cfg: dict) -> dict:
    """Shared rules for the stock-picking strategies. The high-risk config keeps them under
    `momentum:`; any config can set them under `filters:`."""
    return {**FILTER_DEFAULTS, **(cfg.get("momentum") or {}), **(cfg.get("filters") or {})}


def is_stock_strategy(cfg: dict) -> bool:
    return cfg.get("strategy", "etf_trend") != "etf_trend"


def describe(cfg: dict) -> str:
    name = cfg.get("strategy", "etf_trend")
    if name == "combo":
        return "combo of " + ", ".join(params(cfg, "combo")["parts"])
    return name


def baseline(snap: dict, cfg: dict, buyable: set | None = None, held: set | None = None) -> dict:
    name = cfg.get("strategy", "etf_trend")
    if name == "etf_trend":
        return etf_baseline(snap, cfg, held or set())
    if name == "combo":
        parts = params(cfg, "combo")["parts"]
        out = {}
        for part in parts:
            for t, w in baseline(snap, {**cfg, "strategy": part, "cash_ticker": None}, buyable, held).items():
                out[t] = out.get(t, 0.0) + w / len(parts)
        cash_t = cfg.get("cash_ticker")
        if cash_t and cash_t in snap:
            out[cash_t] = out.get(cash_t, 0.0) + max(0.0, 1 - sum(out.values()))
        return out
    picks = candidates(snap, cfg, buyable, held)[: max_positions(cfg)]
    w = min(1 / max(1, max_positions(cfg)), cfg["risk"]["max_position_weight"])
    weights = {t: w for t in picks}
    cash_t = cfg.get("cash_ticker")
    if cash_t and cash_t in snap:                     # ETF agent: park the rest in the cash ETF
        weights[cash_t] = weights.get(cash_t, 0.0) + max(0.0, 1 - sum(weights.values()))
    return weights


def max_positions(cfg: dict) -> int:
    name = cfg.get("strategy", "etf_trend")
    if name == "combo":
        return sum(max_positions({**cfg, "strategy": part}) for part in params(cfg, "combo")["parts"])
    if name == "sector_rotation":
        p = params(cfg, name)
        return p["sectors"] * p["per_sector"]
    if name == "leveraged_trend":
        return params(cfg, name)["max_positions"]
    return filters(cfg)["max_positions"]


def candidates(snap: dict, cfg: dict, buyable: set | None = None, held: set | None = None) -> list[str]:
    """Every stock the configured strategy would consider right now, best first."""
    name = cfg.get("strategy", "etf_trend")
    if name == "combo":
        out = []
        for part in params(cfg, "combo")["parts"]:
            out += [t for t in candidates(snap, {**cfg, "strategy": part}, buyable, held) if t not in out]
        return out
    fn = {"momentum": momentum_candidates, "breakout": breakout_candidates, "pullback": pullback_candidates,
          "sector_rotation": sector_rotation_candidates, "leveraged_trend": leveraged_candidates}[name]
    return fn(snap, cfg, buyable, held or set())


def etf_baseline(snap: dict, cfg: dict, held: set = frozenset()) -> dict:
    """Hold assets in an uptrend with positive momentum, weighted by inverse
    volatility and capped; everything else sits in the cash ETF. An ETF already
    held is kept until it falls more than risk.trend_buffer below its 200-day
    average (0 = sell as soon as it drops below)."""
    cash_t = cfg["cash_ticker"]
    cap = cfg["risk"]["max_position_weight"]
    buffer = cfg["risk"].get("trend_buffer", 0.0)
    picks = {}
    for t, d in snap.items():
        s = d["signals"]
        momentum = (s["ret_3m"] + s["ret_6m"] + s["ret_12m"]) / 3
        if t == cash_t:
            continue
        new_buy = s["above_200d_avg"] and momentum > 0
        keep = t in held and buffer > 0 and s.get("vs_200d_avg", -1) >= -buffer
        if new_buy or keep:
            picks[t] = 1 / max(s["vol_60d_annual"], 0.03)
    total = sum(picks.values())
    weights = {t: min(v / total, cap) for t, v in picks.items()} if total else {}
    weights[cash_t] = max(0.0, 1 - sum(weights.values()))
    return weights


def momentum_score(s: dict) -> float:
    return 0.5 * s["ret_3m"] + 0.3 * s["ret_6m"] + 0.2 * s["ret_1m"]


def _pool(snap: dict, cfg: dict, buyable, kind: str = "stock"):
    """(ticker, data, signals) for tradable entries of this kind: liquid, not a
    penny stock (in CAD), and affordable if `buyable` is given."""
    h = filters(cfg)
    for t, d in snap.items():
        if d.get("kind", "stock") != kind or (buyable is not None and t not in buyable) or t == cfg.get("cash_ticker"):
            continue
        if (d.get("price_cad", d["price"]) >= h["min_price"]
                and d.get("dollar_volume_cad", d["dollar_volume_20d"]) >= h["min_dollar_volume"]):
            yield t, d, d["signals"]


def momentum_candidates(snap: dict, cfg: dict, buyable: set | None = None, held: set | None = None) -> list[str]:
    """Stocks in an uptrend that haven't hit the stop-loss, strongest momentum first."""
    stop = filters(cfg)["stop_loss"]
    out = [t for t, d, s in _pool(snap, cfg, buyable)
           if s["above_50d_avg"] and s["above_200d_avg"] and s["ret_6m"] > 0 and s["drawdown_from_20d_high"] > -stop]
    return sorted(out, key=lambda t: momentum_score(snap[t]["signals"]), reverse=True)


def momentum_baseline(snap: dict, cfg: dict, buyable: set | None = None) -> dict:
    return baseline(snap, {**cfg, "strategy": "momentum"}, buyable)


def breakout_candidates(snap, cfg, buyable=None, held=frozenset()):
    p, stop = params(cfg, "breakout"), filters(cfg)["stop_loss"]
    out = []
    for t, d, s in _pool(snap, cfg, buyable):
        band = p["hold_band"] if t in held else p["near_high"]
        if (s["drawdown_from_1y_high"] >= -band and s["above_50d_avg"] and s["ret_3m"] > 0
                and s["drawdown_from_20d_high"] > -stop):
            out.append(t)
    return sorted(out, key=lambda t: snap[t]["signals"]["ret_3m"], reverse=True)


def pullback_candidates(snap, cfg, buyable=None, held=frozenset()):
    p = params(cfg, "pullback")
    out = []
    for t, d, s in _pool(snap, cfg, buyable):
        dd = s["drawdown_from_20d_high"]
        strong = s["above_200d_avg"] and s["ret_6m"] >= p["min_6m"]
        if t in held:
            keep = strong and -p["dip_max"] <= dd <= -p["recovered"]     # still dipped, not broken
        else:
            keep = strong and -p["dip_max"] <= dd <= -p["dip_min"] and s["rsi_14"] <= p["max_rsi"]
        if keep:
            out.append(t)
    return sorted(out, key=lambda t: snap[t]["signals"]["ret_6m"], reverse=True)


def sector_rotation_candidates(snap, cfg, buyable=None, held=frozenset()):
    p, stop = params(cfg, "sector_rotation"), filters(cfg)["stop_loss"]
    sectors = {}
    for t, d, s in _pool(snap, cfg, None):                 # rank sectors on all their stocks
        sectors.setdefault(d.get("sector") or "other", []).append(t)
    ranked = []
    for name, ts in sectors.items():
        if len(ts) < 2 or name == "other":
            continue
        scores = sorted(momentum_score(snap[t]["signals"]) for t in ts)
        median = scores[len(scores) // 2]
        uptrend = sum(snap[t]["signals"]["above_200d_avg"] for t in ts) / len(ts)
        if median > 0 and uptrend >= 0.5:
            ranked.append((median, name))
    out = []
    for _, name in sorted(ranked, reverse=True)[: p["sectors"]]:
        picks = [t for t in sectors[name] if (buyable is None or t in buyable)
                 and snap[t]["signals"]["above_50d_avg"] and snap[t]["signals"]["above_200d_avg"]
                 and snap[t]["signals"]["drawdown_from_20d_high"] > -stop]
        out += sorted(picks, key=lambda t: momentum_score(snap[t]["signals"]), reverse=True)[: p["per_sector"]]
    return out


def leveraged_candidates(snap, cfg, buyable=None, held=frozenset()):
    stop = filters(cfg)["stop_loss"]
    out = [t for t, d, s in _pool(snap, cfg, buyable, kind="leveraged_etf")
           if s["above_50d_avg"] and s["above_200d_avg"] and s["ret_3m"] > 0 and s["drawdown_from_20d_high"] > -stop]
    return sorted(out, key=lambda t: snap[t]["signals"]["ret_3m"], reverse=True)


def sanitize(weights: dict, cfg: dict, allowed: set | None = None) -> dict:
    """Hard guardrails applied to any proposed weights, whoever proposed them."""
    if allowed is None:
        allowed = {u["ticker"] for u in cfg["universe"]}
    cash_t = cfg.get("cash_ticker")
    cap = cfg["risk"]["max_position_weight"]
    out = {}
    for t, w in weights.items():
        if t not in allowed or not isinstance(w, (int, float)) or w != w or w <= 0:
            continue
        out[t] = min(float(w), 1.0 if t == cash_t else cap)
    max_n = max_positions(cfg) if is_stock_strategy(cfg) else None
    if max_n and len([t for t in out if t != cash_t]) > max_n:
        keep = sorted((t for t in out if t != cash_t), key=lambda t: -out[t])[:max_n]
        out = {t: w for t, w in out.items() if t in keep or t == cash_t}
    total = sum(out.values())
    if total > 1:
        out = {t: w / total for t, w in out.items()}
    return out


def research_news(tickers: list[str], cfg: dict) -> str:
    """Ask Claude to search trusted news sites for the big picture (economy, trade,
    politics between countries) and for recent news on the candidates.
    Returns plain-text notes ("" if nothing usable came back)."""
    import anthropic

    names = ", ".join(t[:-3] + " (TSX)" if t.endswith(".TO") else t + " (NASDAQ)" for t in tickers)
    client = anthropic.Anthropic()
    messages = [{"role": "user", "content": RESEARCH_PROMPT.format(names=names)}]
    tools = [{
        "type": "web_search_20260209", "name": "web_search",
        "max_uses": cfg["claude"].get("max_searches", 15),
        "allowed_domains": cfg["claude"].get("news_sources", NEWS_SOURCES),
    }]
    resp = None
    for _ in range(5):  # resume paused server-side search loops, with a limit
        resp = client.messages.create(
            model=cfg["claude"]["model"],
            max_tokens=16000,
            thinking={"type": "adaptive"},
            output_config={"effort": cfg["claude"]["effort"]},
            tools=tools,
            messages=messages,
        )
        if resp.stop_reason != "pause_turn":
            break
        messages = [messages[0], {"role": "assistant", "content": resp.content}]
    if resp is None or resp.stop_reason == "refusal":
        return ""
    return "\n".join(b.text for b in resp.content if b.type == "text").strip()


def claude_weights(snap: dict, current: dict, base: dict, history: list, cfg: dict,
                   candidates: list[str] | None = None, research: str = ""):
    """Ask Claude for target weights. Returns (weights, rationale) or raises."""
    import anthropic
    from pydantic import BaseModel

    class Weight(BaseModel):
        ticker: str
        weight: float

    class Decision(BaseModel):
        weights: list[Weight]
        rationale: str

    momentum = is_stock_strategy(cfg)
    names = {u["ticker"]: u.get("name", u["ticker"]) for u in cfg["universe"]}
    shown = candidates if momentum else list(snap)
    payload = {
        ("candidates" if momentum else "universe"): {
            t: {"name": names.get(t, t), "currency": snap[t].get("currency", "CAD"),
                "price": snap[t]["price"], "price_cad": round(snap[t].get("price_cad", snap[t]["price"]), 2),
                **{k: round(v, 4) if isinstance(v, float) else v for k, v in snap[t]["signals"].items()}}
            for t in shown
        },
        "per_position_cap": cfg["risk"]["max_position_weight"],
        "current_weights": {t: round(w, 4) for t, w in current.items()},
        "baseline_weights": {t: round(w, 4) for t, w in base.items()},
        "your_recent_decisions": history,
    }
    if momentum:
        payload["strategy"] = describe(cfg)
        payload["strategy_rules"] = STRATEGIES.get(cfg.get("strategy"), "")
        payload["max_positions"] = max_positions(cfg)
        payload["news_research"] = research or "no research available"
    else:
        payload["cash_etf"] = cfg["cash_ticker"]
    client = anthropic.Anthropic()
    resp = client.beta.messages.parse(
        model=cfg["claude"]["model"],
        max_tokens=16000,
        thinking={"type": "adaptive"},
        output_config={"effort": cfg["claude"]["effort"]},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=MOMENTUM_SYSTEM if momentum else ETF_SYSTEM,
        messages=[{"role": "user", "content": json.dumps(payload, indent=1)}],
        output_format=Decision,
    )
    if resp.stop_reason == "refusal" or resp.parsed_output is None:
        raise ValueError(f"no usable decision from Claude (stop_reason={resp.stop_reason})")
    d = resp.parsed_output
    return {w.ticker: w.weight for w in d.weights}, d.rationale
