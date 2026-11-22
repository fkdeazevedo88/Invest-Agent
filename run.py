#!/usr/bin/env python3
"""Investing agent. Run once per rebalance (weekly is plenty).

  python run.py status          show the account
  python run.py run             one decide-and-trade cycle on the PAPER account
  python run.py run --dry-run   decide and show orders, place nothing
  python run.py run --live      one cycle with REAL money on moomoo
  python run.py accounts        list your moomoo accounts and which one the agent uses

Add --config config-highrisk.yaml to any command to use the HIGH-RISK stock agent
instead of the ETF agent. Each agent has its own money, records and state folder.
"""
import argparse
import datetime as dt
import json
import math
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from agent import data, strategy
from agent.brokers import BrokerError, MoomooBroker, PaperBroker, RunLock, list_accounts

ROOT = Path(__file__).parent
STATE = ROOT / "state"
JOURNAL = STATE / "journal.jsonl"
HALT = STATE / "HALT"


def market_open() -> bool:
    now = dt.datetime.now(ZoneInfo("America/Toronto"))
    return now.weekday() < 5 and dt.time(9, 35) <= now.time() <= dt.time(15, 55)


def recent_decisions(n=3) -> list:
    if not JOURNAL.exists():
        return []
    rows = [json.loads(line) for line in JOURNAL.read_text().splitlines() if line.strip()]
    return [{"date": r["time"][:10], "source": r["source"], "target_weights": r["target"], "rationale": r["rationale"]} for r in rows[-n:]]


def account(broker, prices, cfg):
    """The agent's own money, from its records: its cash, its shares, and what's held
    by open orders. `spendable` is the cash it can use right now (never borrowed)."""
    cash, pos, spendable = broker.own_cash(), broker.positions(), broker.cash()
    missing = [t for t in pos if t not in prices]
    if missing:
        raise data.DataError(f"no price for {', '.join(missing)}, which the agent holds; not trading blind")
    value = {t: q * prices[t] for t, q in pos.items()}
    held = broker.reserved()
    equity = cash + sum(value.values()) + held
    return cash, pos, value, equity, held, spendable


def show(cash, pos, value, equity, held, spendable, cfg):
    start = cfg["capital_cad"]
    print(f"\nEquity ${equity:,.2f} CAD  ({(equity / start - 1) * 100:+.2f}% vs ${start:,.0f} start)")
    for t in sorted(value, key=value.get, reverse=True):
        print(f"  {t:<9} {pos[t]:>4} sh  ${value[t]:>8,.2f}  {value[t] / equity * 100:5.1f}%")
    print(f"  {'cash':<9}          ${cash:>8,.2f}  {cash / equity * 100:5.1f}%")
    if held:
        print(f"  {'in orders':<9}          ${held:>8,.2f}  {held / equity * 100:5.1f}%  (waiting to fill)")
    if spendable < cash - 0.01:
        print(f"  Note: only ${max(spendable, 0):,.2f} of the agent's cash is in the account right now. "
              "It never borrows, so it can only spend that.")


def plan_orders(target, pos, prices, equity, cfg):
    """Turn target weights into whole-share orders, skipping trades too small to matter."""
    r = cfg["risk"]
    investable = min(equity, cfg["max_equity_cad"])
    threshold = max(r["min_trade_cad"], r["no_trade_band"] * equity)
    sells, buys = [], []
    for t in sorted(set(target) | set(pos)):
        want = math.floor(target.get(t, 0) * investable / prices[t])
        diff = want - pos.get(t, 0)
        full_exit = want == 0 and pos.get(t, 0) > 0
        if diff == 0 or (abs(diff) * prices[t] < threshold and not full_exit):
            continue
        (buys if diff > 0 else sells).append((t, abs(diff)))
    buys.sort(key=lambda o: -o[1] * prices[o[0]])  # biggest buys first if cash runs short
    return sells, buys


def cycle(args, cfg):
    STATE.mkdir(exist_ok=True)
    if HALT.exists():
        sys.exit(f"HALTED by kill switch: {HALT.read_text().strip()}\nReview, then delete {HALT} to resume.")
    if args.live and not args.dry_run and not market_open():
        sys.exit("TSX is closed (or within 5 min of open/close). Live orders only go out during market hours.")

    tickers = [u["ticker"] for u in cfg["universe"]]
    momentum = cfg.get("strategy") == "momentum"
    snap = data.snapshot(tickers, skip_missing=momentum)
    prices = {t: d["price"] for t, d in snap.items()}

    broker = MoomooBroker(cfg, STATE) if args.live else PaperBroker(cfg, STATE)
    try:
        if broker.live:
            print(f"Trading moomoo {broker.acc_type} account {broker.acc_id}")
        cash, pos, value, equity, held, spendable = account(broker, prices, cfg)
        show(cash, pos, value, equity, held, spendable, cfg)
        if broker.pending():
            print(f"\n{len(broker.pending())} earlier order(s) still waiting to fill. Not trading until they finish (DAY orders expire at market close).")
            return
        slip = cfg["risk"]["limit_slippage"]
        log = {"time": dt.datetime.now(dt.timezone.utc).isoformat(), "mode": "live" if args.live else "paper", "equity_before": round(equity, 2)}

        # Kill switch: deterministic, checked before any model is consulted.
        floor = cfg["capital_cad"] * (1 - cfg["risk"]["max_drawdown"])
        if equity < floor:
            source, rationale, target = "kill-switch", f"equity ${equity:.2f} below floor ${floor:.2f}; selling everything", {}
            if not args.dry_run:
                HALT.write_text(f"{log['time']}: {rationale}\n")
        else:
            candidates = strategy.momentum_candidates(snap, cfg) if momentum else None
            allowed = set(candidates) if momentum else None
            base = strategy.sanitize(strategy.baseline(snap, cfg), cfg, allowed)
            if momentum:
                rationale = (f"strongest momentum among {len(candidates)} stocks passing the filters"
                             if candidates else "no stock passes the filters; staying in cash")
            else:
                rationale = "trend filter + inverse-volatility baseline"
            source, target = "rules", base
            if cfg["claude"]["enabled"] and os.environ.get("ANTHROPIC_API_KEY") and (candidates or not momentum):
                try:
                    research = ""
                    if momentum and cfg["claude"].get("web_search"):
                        print("\nClaude is searching the news on the candidates...")
                        research = strategy.research_news(candidates[: cfg["momentum"]["research_top"]], cfg)
                    current = {t: v / equity for t, v in value.items()}
                    raw, why = strategy.claude_weights(snap, current, base, recent_decisions(), cfg, candidates, research)
                    target, source, rationale = strategy.sanitize(raw, cfg, allowed), "claude", why
                except Exception as e:  # Claude is optional: any failure falls back to the rules
                    print(f"\nClaude unavailable ({type(e).__name__}: {e}); using rules baseline.")

        print(f"\nDecision by: {source}\n{rationale}\n\nTarget:")
        for t, w in sorted(target.items(), key=lambda x: -x[1]):
            print(f"  {t:<9} {w * 100:5.1f}%")

        sells, buys = plan_orders(target, pos, prices, equity, cfg)
        executed = []
        print("\nOrders:" if sells or buys else "\nNo trades needed.")
        for t, q in sells:
            limit = prices[t] * (1 - slip)
            print(f"  SELL {q:>3} {t:<9} @ {limit:.2f}")
            if not args.dry_run:
                executed.append({"side": "SELL", "ticker": t, "qty": q, **broker.place(t, "SELL", q, limit)})
        if sells and buys and not args.dry_run:
            broker.settle()
        budget = (spendable + sum(q * prices[t] * (1 - slip) for t, q in sells)) if args.dry_run else broker.cash()
        budget = min(budget, max(0.0, cfg["max_equity_cad"] - sum(value.values())) + sum(q * prices[t] for t, q in sells))
        for t, q in buys:
            limit = prices[t] * (1 + slip)
            q = min(q, math.floor((budget - cfg["paper"]["commission_cad"]) / limit))
            if q <= 0:
                print(f"  skip BUY {t}: not enough cash")
                continue
            budget -= q * limit
            print(f"  BUY  {q:>3} {t:<9} @ {limit:.2f}")
            if not args.dry_run:
                executed.append({"side": "BUY", "ticker": t, "qty": q, **broker.place(t, "BUY", q, limit)})

        if buys and broker.live and not args.dry_run:
            broker.settle()  # give buy orders a moment to fill before showing the result
        if args.dry_run:
            print("\n(dry run: nothing placed, nothing recorded)")
            return
        cash, pos, value, equity, held, spendable = account(broker, prices, cfg)
        show(cash, pos, value, equity, held, spendable, cfg)
        log.update(source=source, rationale=rationale, target={t: round(w, 4) for t, w in target.items()}, orders=executed, equity_after=round(equity, 2))
        with JOURNAL.open("a") as f:
            f.write(json.dumps(log) + "\n")
    finally:
        broker.close()


def status(args, cfg):
    STATE.mkdir(exist_ok=True)
    broker = MoomooBroker(cfg, STATE) if args.live else PaperBroker(cfg, STATE)
    try:
        held = set(broker.positions()) | {o["ticker"] for o in broker.pending()}
        prices = {t: data.fetch(t)["price"] for t in sorted(held)}
        if broker.live:
            print(f"moomoo {broker.acc_type} account {broker.acc_id}")
        show(*account(broker, prices, cfg), cfg)
    finally:
        broker.close()
    if HALT.exists():
        print(f"\nHALTED: {HALT.read_text().strip()}")


def load_config(path: Path) -> dict:
    cfg = yaml.safe_load(path.read_text())
    exclude = {e.upper() for e in cfg.get("exclude", [])}
    cfg["universe"] = [u for u in cfg["universe"] if u["ticker"].upper() not in exclude]
    return cfg


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["run", "status", "accounts"])
    p.add_argument("--live", action="store_true", help="use the real moomoo account (real money)")
    p.add_argument("--dry-run", action="store_true", help="show the decision and orders without placing them")
    p.add_argument("--config", default="config.yaml", help="config file (config-highrisk.yaml for the high-risk agent)")
    args = p.parse_args()
    cfg = load_config(ROOT / args.config)
    global STATE, JOURNAL, HALT
    STATE = ROOT / cfg.get("state_dir", "state")
    JOURNAL, HALT = STATE / "journal.jsonl", STATE / "HALT"
    print(f"[{cfg.get('name', 'ETF agent')}]")
    STATE.mkdir(exist_ok=True)
    if args.command == "accounts":
        try:
            print(list_accounts(cfg))
        except BrokerError as e:
            sys.exit(f"Stopped: {e}")
        return
    try:
        # One lock for ALL agents: two agents running at once could spend the same cash.
        with RunLock(ROOT / "run.lock"):
            (cycle if args.command == "run" else status)(args, cfg)
    except (BrokerError, data.DataError) as e:
        sys.exit(f"Stopped, nothing further done: {e}")


if __name__ == "__main__":
    main()
