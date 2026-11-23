#!/usr/bin/env python3
"""Investing agent. Run once per rebalance (weekly is plenty).

  python run.py status          show the account
  python run.py run             one decide-and-trade cycle on the PAPER account
  python run.py run --dry-run   decide and show orders, place nothing
  python run.py run --live      one cycle with REAL money on moomoo
  python run.py accounts        list your moomoo accounts and which one the agent uses
  python run.py check --live    ask moomoo whether it accepts every symbol (places no orders)

Add --config config-highrisk.yaml to any command to use the HIGH-RISK stock agent
instead of the ETF agent. Each agent has its own money, records and state folder.

  python run.py backtest --config config-highrisk.yaml --strategy all
                                compare the high-risk strategies on past prices
"""
import argparse
import datetime as dt
import json
import math
import os
import sys
import types
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from agent import data, strategy
from agent.brokers import BrokerError, MoomooBroker, PaperBroker, RunLock, currency_of, list_accounts

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


def account(broker, prices, fx, cfg):
    """The agent's own money, from its records, valued in CAD: its cash (per currency),
    its shares, and what's held by open orders. `spend` is the cash it can use right
    now in each currency (never borrowed, never converted)."""
    cur = lambda tk: broker.currency.get(tk, "CAD")  # noqa: E731
    own, pos = broker.own_cash(), broker.positions()
    missing = [tk for tk in pos if tk not in prices]
    if missing:
        raise data.DataError(f"no price for {', '.join(missing)}, which the agent holds; not trading blind")
    value = {tk: q * prices[tk] * fx[cur(tk)] for tk, q in pos.items()}
    held = sum(v * fx[c] for c, v in broker.reserved().items())
    cash_cad = sum(v * fx[c] for c, v in own.items() if c in fx)
    return types.SimpleNamespace(
        cash=own, pos=pos, value=value, held=held, cash_cad=cash_cad,
        equity=cash_cad + sum(value.values()) + held,
        spend={c: broker.cash(c) for c in own},
    )


def start_capital(broker, fx) -> float:
    """The agent's starting money in CAD (USD at today's rate), from its own records."""
    return sum(v * fx.get(c, 0.0) for c, v in broker.start().items())


def show(a, cfg, fx, broker):
    start = start_capital(broker, fx)
    eq = a.equity or 1
    print(f"\nEquity ${a.equity:,.2f} CAD  ({(a.equity / start - 1) * 100:+.2f}% vs ${start:,.0f} start)")
    for tk in sorted(a.value, key=a.value.get, reverse=True):
        usd = f"  (US${a.value[tk] / fx['USD']:,.2f})" if broker.currency.get(tk) == "USD" else ""
        print(f"  {tk:<9} {a.pos[tk]:>4} sh  ${a.value[tk]:>8,.2f}  {a.value[tk] / eq * 100:5.1f}%{usd}")
    for c, v in sorted(a.cash.items()):
        if c == "CAD" or v or a.spend.get(c):
            label = "cash" if c == "CAD" else f"cash {c}"
            extra = f"  (= ${v * fx[c]:,.2f} CAD)" if c != "CAD" and c in fx else ""
            share = v * fx.get(c, 0) / eq * 100
            print(f"  {label:<9}          {'$' if c == 'CAD' else 'US$'}{v:>8,.2f}  {share:5.1f}%{extra}")
            if a.spend.get(c, 0) < v - 0.01:
                print(f"  Note: only {c} {max(a.spend.get(c, 0), 0):,.2f} of the agent's {c} cash is in the account "
                      "right now. It never borrows or converts currency, so it can only spend that.")
    if a.held:
        print(f"  {'in orders':<9}          ${a.held:>8,.2f}  {a.held / eq * 100:5.1f}%  (waiting to fill)")


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
    currencies = {u["ticker"]: currency_of(u) for u in cfg["universe"]}
    info = {u["ticker"]: u for u in cfg["universe"]}
    momentum = strategy.is_stock_strategy(cfg)        # individual stocks / leveraged ETFs

    broker = MoomooBroker(cfg, STATE) if args.live else PaperBroker(cfg, STATE)
    try:
        if broker.live:
            print(f"Trading moomoo {broker.acc_type} account {broker.acc_id}")
        fx = data.fx_rates(set(currencies.values()) | {c for c, v in {**broker.own_cash(), **broker.start()}.items() if v})
        twins = {u["ticker"]: u["signal"] for u in cfg["universe"] if u.get("signal")}
        snap = data.snapshot(tickers, skip_missing=momentum, currencies=currencies, signals_from=twins)
        for tk, d in snap.items():
            c = currencies[tk]
            d.update(currency=c, price_cad=d["price"] * fx[c], dollar_volume_cad=d["dollar_volume_20d"] * fx[c],
                     sector=info[tk].get("sector"), kind=info[tk].get("kind", "stock"), signal=info[tk].get("signal"))
        prices = {tk: d["price"] for tk, d in snap.items()}          # in each stock's own currency
        prices_cad = {tk: d["price_cad"] for tk, d in snap.items()}
        a = account(broker, prices, fx, cfg)
        cash, pos, value, equity = a.cash, a.pos, a.value, a.equity
        show(a, cfg, fx, broker)
        if broker.pending():
            print(f"\n{len(broker.pending())} earlier order(s) still waiting to fill. Not trading until they finish (DAY orders expire at market close).")
            return
        slip = cfg["risk"]["limit_slippage"]
        log = {"time": dt.datetime.now(dt.timezone.utc).isoformat(), "mode": "live" if args.live else "paper", "equity_before": round(equity, 2)}

        # Kill switch: deterministic, checked before any model is consulted.
        floor = start_capital(broker, fx) * (1 - cfg["risk"]["max_drawdown"])
        if equity < floor:
            source, rationale, target = "kill-switch", f"equity ${equity:.2f} below floor ${floor:.2f}; selling everything", {}
            if not args.dry_run:
                HALT.write_text(f"{log['time']}: {rationale}\n")
        else:
            buyable = None
            if momentum:
                # A stock can only be picked if the agent could afford at least one share
                # of it, in that stock's own currency, within one position's budget.
                n = strategy.max_positions(cfg)
                per_position = min(1 / n, cfg["risk"]["max_position_weight"]) * min(equity, cfg["max_equity_cad"])
                in_cur = {c: v for c, v in cash.items()}
                for tk, q in pos.items():
                    in_cur[currencies.get(tk, "CAD")] = in_cur.get(currencies.get(tk, "CAD"), 0) + q * prices[tk]
                buyable = {tk for tk, d in snap.items()
                           if tk in pos or (d["price_cad"] <= per_position and d["price"] <= in_cur.get(d["currency"], 0))}
            held = set(pos)
            candidates = strategy.candidates(snap, cfg, buyable, held) if momentum else None
            allowed = set(candidates) if momentum else None
            base = strategy.sanitize(strategy.baseline(snap, cfg, buyable=buyable, held=held), cfg, allowed)
            if momentum:
                rationale = (f"{strategy.describe(cfg)}: top picks among {len(candidates)} that pass its rules"
                             if candidates else f"{strategy.describe(cfg)}: nothing passes its rules; staying in cash")
            else:
                rationale = "trend filter + inverse-volatility baseline"
            source, target = "rules", base
            if cfg["claude"]["enabled"] and os.environ.get("ANTHROPIC_API_KEY") and (candidates or not momentum):
                try:
                    research = ""
                    if momentum and cfg["claude"].get("web_search"):
                        print("\nClaude is checking world news and news on the candidates...")
                        research = strategy.research_news(candidates[: strategy.filters(cfg)["research_top"]], cfg)
                    current = {t: v / equity for t, v in value.items()}
                    raw, why = strategy.claude_weights(snap, current, base, recent_decisions(), cfg, candidates, research)
                    target, source, rationale = strategy.sanitize(raw, cfg, allowed), "claude", why
                except Exception as e:  # Claude is optional: any failure falls back to the rules
                    print(f"\nClaude unavailable ({type(e).__name__}: {e}); using rules baseline.")

        print(f"\nDecision by: {source}\n{rationale}\n\nTarget:")
        for t, w in sorted(target.items(), key=lambda x: -x[1]):
            print(f"  {t:<9} {w * 100:5.1f}%")

        frozen = broker.frozen()
        if frozen:
            print(f"\nNot trading {', '.join(sorted(frozen))}: moomoo shows fewer shares than the agent's records. "
                  "Check them in the app; the agent won't buy or sell them until the numbers match.")
        target = {tk: w for tk, w in target.items() if tk not in frozen}
        sells, buys = plan_orders(target, {tk: q for tk, q in pos.items() if tk not in frozen}, prices_cad, equity, cfg)
        executed = []
        print("\nOrders:" if sells or buys else "\nNo trades needed.")
        for t, q in sells:
            limit = prices[t] * (1 - slip)
            print(f"  SELL {q:>3} {t:<9} @ {limit:.2f} {currencies[t]}")
            if not args.dry_run:
                executed.append({"side": "SELL", "ticker": t, "qty": q, **broker.place(t, "SELL", q, limit)})
        if sells and buys and not args.dry_run:
            broker.settle()
        budget = {}
        for c in set(cash) | {currencies[t] for t, _ in buys}:
            if args.dry_run:
                budget[c] = a.spend.get(c, 0.0) + sum(q * prices[t] * (1 - slip) for t, q in sells if currencies[t] == c)
            else:
                budget[c] = broker.cash(c)
        cap = max(0.0, cfg["max_equity_cad"] - sum(value.values())) + sum(q * prices_cad[t] for t, q in sells)
        for t, q in buys:
            c = currencies[t]
            limit = prices[t] * (1 + slip)
            q = min(q, math.floor((budget[c] - cfg["paper"]["commission_cad"]) / limit), math.floor(cap / (limit * fx[c])))
            if q <= 0:
                hint = " (add US dollars in moomoo; the agent never borrows or converts)" if c == "USD" else ""
                print(f"  skip BUY {t}: not enough {c} cash{hint}")
                continue
            budget[c] -= q * limit
            cap -= q * limit * fx[c]
            print(f"  BUY  {q:>3} {t:<9} @ {limit:.2f} {c}")
            if not args.dry_run:
                executed.append({"side": "BUY", "ticker": t, "qty": q, **broker.place(t, "BUY", q, limit)})

        if buys and broker.live and not args.dry_run:
            broker.settle()  # give buy orders a moment to fill before showing the result
        if args.dry_run:
            print("\n(dry run: nothing placed, nothing recorded)")
            return
        a = account(broker, prices, fx, cfg)
        equity = a.equity
        show(a, cfg, fx, broker)
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
        cur = {t: broker.currency.get(t, "CAD") for t in held}
        fx = data.fx_rates(set(cur.values()) | {c for c, v in {**broker.own_cash(), **broker.start()}.items() if v})
        prices = {t: data.fetch(t, cur[t])["price"] for t in sorted(held)}
        if broker.live:
            print(f"moomoo {broker.acc_type} account {broker.acc_id}")
        show(account(broker, prices, fx, cfg), cfg, fx, broker)
    finally:
        broker.close()
    if HALT.exists():
        print(f"\nHALTED: {HALT.read_text().strip()}")


def check(args, cfg):
    """Ask moomoo, for every symbol the agent may trade, whether it would accept an order.
    Places nothing. Use it once before trading live, and after adding symbols."""
    if not args.live:
        sys.exit("check talks to moomoo: run it with --live (it places no orders)")
    broker = MoomooBroker(cfg, STATE)
    try:
        print(f"moomoo {broker.acc_type} account {broker.acc_id}: checking {len(cfg['universe'])} symbols (no orders)...\n")
        bad = []
        for u in cfg["universe"]:
            t = u["ticker"]
            try:
                price = data.quote(t, currency_of(u)) if u.get("signal") else data.fetch(t, currency_of(u))["price"]
            except data.DataError as e:
                print(f"  ?   {t:<9} no price data ({e})")
                continue
            ok, msg = broker.check_symbol(t, price)
            print(f"  {'OK ' if ok else 'NO '} {t:<9} {broker.to_mm[t]:<11} {'' if ok else msg}")
            if not ok:
                bad.append(t)
        print(f"\n{len(bad)} symbol(s) moomoo won't accept" + (": " + ", ".join(bad) + ". Add them to `exclude:` in the config, or tell Claude." if bad else "."))
    finally:
        broker.close()


def load_config(path: Path) -> dict:
    cfg = yaml.safe_load(path.read_text())
    for u in cfg["universe"]:
        if not isinstance(u.get("ticker"), str):
            raise SystemExit(f"{path.name}: ticker {u.get('ticker')!r} must be text; put it in quotes, e.g. ticker: \"ON\"")
    exclude = {e.upper() for e in cfg.get("exclude", [])}
    cfg["universe"] = [u for u in cfg["universe"] if u["ticker"].upper() not in exclude]
    return cfg


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["run", "status", "accounts", "backtest", "check"])
    p.add_argument("--live", action="store_true", help="use the real moomoo account (real money)")
    p.add_argument("--dry-run", action="store_true", help="show the decision and orders without placing them")
    p.add_argument("--config", default="config.yaml", help="config file (config-highrisk.yaml for the high-risk agent)")
    p.add_argument("--strategy", default=None, help="backtest only: a strategy name, or 'all' to compare them")
    args = p.parse_args()
    cfg = load_config(ROOT / args.config)
    global STATE, JOURNAL, HALT
    STATE = ROOT / cfg.get("state_dir", "state")
    JOURNAL, HALT = STATE / "journal.jsonl", STATE / "HALT"
    print(f"[{cfg.get('name', 'ETF agent')}]")
    STATE.mkdir(exist_ok=True)
    if args.command == "backtest":
        from agent import backtest
        backtest.main(cfg, args.strategy)
        return
    if args.command == "accounts":
        try:
            print(list_accounts(cfg))
        except BrokerError as e:
            sys.exit(f"Stopped: {e}")
        return
    try:
        # One lock for ALL agents: two agents running at once could spend the same cash.
        with RunLock(ROOT / "run.lock"):
            {"run": cycle, "status": status, "check": check}[args.command](args, cfg)
    except (BrokerError, data.DataError) as e:
        sys.exit(f"Stopped, nothing further done: {e}")


if __name__ == "__main__":
    main()
