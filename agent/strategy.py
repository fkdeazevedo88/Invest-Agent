"""Deciding target weights: a rules baseline, optionally refined by Claude.

Two strategies, picked by `strategy:` in the config file:
  etf_trend  (default)  diversified ETFs, trend filter + inverse volatility
  momentum              HIGH RISK: a few individual stocks with the strongest
                        recent momentum, with liquidity and stop-loss filters
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
losses. You receive momentum candidates that already passed the code's filters (liquid, above $2,
in an uptrend, not in a sharp recent drop, affordable), the current holdings, a rules-based baseline
(the strongest momentum names, equal weight), your recent decisions, and news research covering
both the big picture (economy, trade and tariffs, central banks, tensions and politics between
countries, commodities, CAD/USD) and each candidate. Reply with target weights.

How to think about it:
- This sleeve rides strong price trends in a few stocks. The baseline is a sound default; deviate
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


def baseline(snap: dict, cfg: dict, buyable: set | None = None) -> dict:
    if cfg.get("strategy", "etf_trend") == "momentum":
        return momentum_baseline(snap, cfg, buyable)
    return etf_baseline(snap, cfg)


def etf_baseline(snap: dict, cfg: dict) -> dict:
    """Hold assets in an uptrend with positive momentum, weighted by inverse
    volatility and capped; everything else sits in the cash ETF."""
    cash_t = cfg["cash_ticker"]
    cap = cfg["risk"]["max_position_weight"]
    picks = {}
    for t, d in snap.items():
        s = d["signals"]
        momentum = (s["ret_3m"] + s["ret_6m"] + s["ret_12m"]) / 3
        if t != cash_t and s["above_200d_avg"] and momentum > 0:
            picks[t] = 1 / max(s["vol_60d_annual"], 0.03)
    total = sum(picks.values())
    weights = {t: min(v / total, cap) for t, v in picks.items()} if total else {}
    weights[cash_t] = max(0.0, 1 - sum(weights.values()))
    return weights


def momentum_score(s: dict) -> float:
    return 0.5 * s["ret_3m"] + 0.3 * s["ret_6m"] + 0.2 * s["ret_1m"]


def momentum_candidates(snap: dict, cfg: dict, buyable: set | None = None) -> list[str]:
    """Stocks that pass every filter, strongest momentum first. Prices and traded
    value are compared in CAD. `buyable`, if given, limits the list to stocks the
    agent can afford at least one share of."""
    h = cfg["momentum"]
    out = []
    for t, d in snap.items():
        s = d["signals"]
        if buyable is not None and t not in buyable:
            continue
        if (
            d.get("price_cad", d["price"]) >= h["min_price"]
            and d.get("dollar_volume_cad", d["dollar_volume_20d"]) >= h["min_dollar_volume"]
            and s["above_50d_avg"]
            and s["above_200d_avg"]
            and s["ret_6m"] > 0
            and s["drawdown_from_20d_high"] > -h["stop_loss"]
        ):
            out.append(t)
    return sorted(out, key=lambda t: momentum_score(snap[t]["signals"]), reverse=True)


def momentum_baseline(snap: dict, cfg: dict, buyable: set | None = None) -> dict:
    """Equal weight in the top N candidates; whatever isn't invested stays cash.
    A held stock drops out (and is sold) once it fails a filter, e.g. falls more
    than stop_loss below its 20-day high."""
    n = cfg["momentum"]["max_positions"]
    picks = momentum_candidates(snap, cfg, buyable)[:n]
    w = min(1 / n, cfg["risk"]["max_position_weight"])
    return {t: w for t in picks}


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
    max_n = cfg.get("momentum", {}).get("max_positions")
    if max_n and len(out) > max_n:
        out = dict(sorted(out.items(), key=lambda x: -x[1])[:max_n])
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

    momentum = cfg.get("strategy") == "momentum"
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
        payload["max_positions"] = cfg["momentum"]["max_positions"]
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
