"""Deciding target weights: a rules baseline, optionally refined by Claude."""
import json

SYSTEM = """You manage a small, long-only Canadian ETF portfolio (about $1,000 CAD) for a retail investor.
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


def baseline(snap: dict, cfg: dict) -> dict:
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


def sanitize(weights: dict, cfg: dict) -> dict:
    """Hard guardrails applied to any proposed weights, whoever proposed them."""
    allowed = {u["ticker"] for u in cfg["universe"]}
    cash_t = cfg["cash_ticker"]
    cap = cfg["risk"]["max_position_weight"]
    out = {}
    for t, w in weights.items():
        if t not in allowed or not isinstance(w, (int, float)) or w != w or w <= 0:
            continue
        out[t] = min(float(w), 1.0 if t == cash_t else cap)
    total = sum(out.values())
    if total > 1:
        out = {t: w / total for t, w in out.items()}
    return out


def claude_weights(snap: dict, current: dict, base: dict, history: list, cfg: dict):
    """Ask Claude for target weights. Returns (weights, rationale) or raises."""
    import anthropic
    from pydantic import BaseModel

    class Weight(BaseModel):
        ticker: str
        weight: float

    class Decision(BaseModel):
        weights: list[Weight]
        rationale: str

    names = {u["ticker"]: u["name"] for u in cfg["universe"]}
    payload = {
        "universe": {
            t: {"name": names[t], "price_cad": d["price"], **{k: round(v, 4) if isinstance(v, float) else v for k, v in d["signals"].items()}}
            for t, d in snap.items()
        },
        "cash_etf": cfg["cash_ticker"],
        "per_position_cap": cfg["risk"]["max_position_weight"],
        "current_weights": {t: round(w, 4) for t, w in current.items()},
        "baseline_weights": {t: round(w, 4) for t, w in base.items()},
        "your_recent_decisions": history,
    }
    client = anthropic.Anthropic()
    resp = client.beta.messages.parse(
        model=cfg["claude"]["model"],
        max_tokens=16000,
        thinking={"type": "adaptive"},
        output_config={"effort": cfg["claude"]["effort"]},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=SYSTEM,
        messages=[{"role": "user", "content": json.dumps(payload, indent=1)}],
        output_format=Decision,
    )
    if resp.stop_reason == "refusal" or resp.parsed_output is None:
        raise ValueError(f"no usable decision from Claude (stop_reason={resp.stop_reason})")
    d = resp.parsed_output
    return {w.ticker: w.weight for w in d.weights}, d.rationale
