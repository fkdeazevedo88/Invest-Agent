#!/usr/bin/env python3
"""Research agent: one US stock at a time, moomoo PAPER trading only.

You don't run this by hand: in Claude Code, type "run the research" and Claude follows
.claude/skills/run-the-research/SKILL.md, calling these commands:

  python research.py status                     what it holds, apply the sell rules, wins/losses
  python research.py signals AAA BBB CCC        the free chart checklist for each candidate
  python research.py buy AAA --mcap 2.1e9 --reason "..." [--analyst "..."] [--earnings 2026-12-04]
                         [--fda "..."] [--compared "BBB: ...; CCC: ..."] [--dry-run]
  python research.py skip --reason "..." [--compared "..."]     record a no-buy day
  python research.py test-paper AAPL           one-time check: does paper trading take pre-market orders?
  python research.py log                       the decision log and the win/loss count

Paper only: there is no real-money option anywhere in this agent.
"""
import argparse
import datetime as dt
import json
import math
import sys
import time
from pathlib import Path

import yaml

from agent import data, notify, research
from agent.brokers import BrokerError, RunLock
from agent.paper_us import ET, PaperUSBroker, market_time, today, trading_days_between

ROOT = Path(__file__).parent


def load(cfg_path: Path) -> tuple[dict, Path]:
    cfg = yaml.safe_load(cfg_path.read_text())
    state = ROOT / cfg.get("state_dir", "state-research")
    state.mkdir(exist_ok=True)
    return cfg, state


def log(state: Path, kind: str, text: str, record: dict):
    """Every decision goes to journal.jsonl (for code) and research-log.md (for you)."""
    now = dt.datetime.now(ET)
    with (state / "journal.jsonl").open("a") as f:
        f.write(json.dumps({"time": now.isoformat(), "kind": kind, **record}) + "\n")
    with (state / "research-log.md").open("a") as f:
        f.write(f"\n## {now:%a %b %d, %Y %H:%M} ET: {kind}\n\n{text}\n")


def tally(b: PaperUSBroker) -> str:
    s = b.summary()
    rate = f", {s['wins'] / s['trades'] * 100:.0f}% wins" if s["trades"] else ""
    return (f"Score so far: {s['wins']} won, {s['losses']} lost{rate}. "
            f"Total profit: {'+' if s['profit'] >= 0 else '-'}${abs(s['profit']):.2f} on ${s['start']:.0f} of paper money.")


def report_events(cfg, state, b, events):
    for e in events:
        print("  •", e)
        if e.startswith(("BOUGHT", "SOLD")):
            win = "+" in e.split("(")[-1][:2] if e.startswith("SOLD") else None
            notify.send(cfg, "Research agent (paper)", e + ("\n" + tally(b) if e.startswith("SOLD") else ""),
                        tags="white_check_mark" if win else ("rotating_light" if win is False else "chart_with_upwards_trend"))
            log(state, "trade", e + ("\n\n" + tally(b) if e.startswith("SOLD") else ""), {"event": e})


def cmd_status(cfg, state, args):
    b = PaperUSBroker(cfg, state)
    try:
        report_events(cfg, state, b, b.refresh(research.latest_price))
        pos, pend = b.ledger["position"], b.ledger["pending"]
        print(f"\nPaper account {b.acc_id} · market is {market_time()}")
        if pend:
            print(f"Waiting order: {pend['side']} {pend['qty']} {pend['ticker']} at ${pend['limit']:.2f} "
                  f"(placed {pend['date']}). Run the research again after 9:30 AM ET to see if it filled.")
            nxt = "WAIT"
        elif pos:
            price = research.latest_price(pos["ticker"])
            pct = price / pos["entry"] - 1
            days = trading_days_between(pos["bought"], today())
            print(f"Holding {pos['qty']} {pos['ticker']}: bought ${pos['entry']:.2f}, now ${price:.2f} "
                  f"({pct*100:+.1f}%, {'+' if pct >= 0 else '-'}${abs(pos['qty'] * (price - pos['entry'])):.2f})")
            print(f"  Sell at ${pos['tp']:.2f} (+{cfg['take_profit']*100:.0f}%, {'automatic at moomoo' if pos['tp_mode'] == 'moomoo' else 'checked when you run this'})"
                  f" · stop at ${pos['sl']:.2f} (-{cfg['stop_loss']*100:.0f}%, {'automatic at moomoo' if pos['sl_mode'] == 'moomoo' else 'checked when you run this'})")
            print(f"  Day {days} of {cfg['max_hold_days']} trading days.")
            if pos.get("earnings"):
                hold_end = dt.date.fromisoformat(pos["bought"]) + dt.timedelta(days=16)
                if today().isoformat() <= pos["earnings"] <= hold_end.isoformat():
                    print(f"  ⚠️ Earnings report on {pos['earnings']}, during the holding period: the price can jump either way.")
            if pos.get("fda"):
                print(f"  ⚠️ FDA event: {pos['fda']}")
            nxt = "HOLD"
        else:
            print("No stock held.")
            nxt = "RESEARCH"
        print("\n" + tally(b))
        print(f"\nNEXT: {nxt}")
    finally:
        b.close()


def cmd_signals(cfg, state, args):
    spy = research.history("SPY")
    m = research.market_check(spy)
    print(f"Whole market: S&P 500 {m['sp500']:.2f} vs 50-day average {m['sp500_50d']:.2f} → "
          + ("OK to buy" if m["ok"] else "FALLING: no buys today"))
    analyst = {t.strip().upper() for t in (args.analyst_upgrade or "").split(",") if t.strip()}
    out = []
    for t in args.tickers:
        t = t.upper()
        try:
            c = research.checklist(t, research.history(t), spy, cfg, analyst_upgrade=t in analyst)
        except data.DataError as e:
            print(f"\n{t}: no price data ({e})")
            continue
        out.append(c)
        print("\n" + research.explain(c))
    if args.json:
        print("\nJSON " + json.dumps(out))


def cmd_buy(cfg, state, args):
    t = args.ticker.upper()
    if args.mcap < cfg["min_market_cap_usd"]:
        sys.exit(f"Refused: {t} is worth ${args.mcap/1e6:,.0f}M, under the ${cfg['min_market_cap_usd']/1e6:,.0f}M minimum.")
    spy = research.history("SPY")
    c = research.checklist(t, research.history(t), spy, cfg, analyst_upgrade=bool(args.analyst))
    print(research.explain(c))
    if not c["passes"]:
        log(state, "rejected", f"**{t}** did not pass the checklist, so it was not bought.\n\n```\n{research.explain(c)}\n```",
            {"ticker": t, "checklist": c})
        sys.exit(f"\nRefused: {t} does not pass the checklist (needs no ⛔ and at least {cfg['signals']['min_positive']} ✅).")
    price = research.latest_price(t)
    limit = round(price * (1 + cfg["limit_slippage"]), 2)
    b = PaperUSBroker(cfg, state)
    try:
        report_events(cfg, state, b, b.refresh(research.latest_price))
        if b.ledger["position"] or b.ledger["pending"]:
            sys.exit("Refused: already holding a stock or waiting on an order (one stock at a time).")
        budget = min(b.ledger["cash"], float(cfg["capital_usd"]))
        qty = math.floor(budget / limit)
        if qty < 1:
            sys.exit(f"Refused: one share of {t} (${limit:.2f}) costs more than the ${budget:.2f} available.")
        session = market_time()
        premarket = session == "premarket" and b.caps().get("premarket_ok") is True
        when = ("now, in pre-market" if premarket else
                "at 9:30 AM ET when the market opens" if session in ("premarket", "closed") else "now")
        text = (f"**Bought {t}** (paper): {qty} shares, limit ${limit:.2f} (latest price ${price:.2f}), order fills {when}.\n\n"
                f"**Why:** {args.reason}\n\n"
                f"- Company value: ${args.mcap/1e9:.2f}B\n"
                f"- Earnings date: {args.earnings or 'not found'}\n"
                f"- Analysts: {args.analyst or 'no recent upgrades found'}\n"
                + (f"- FDA: {args.fda}\n" if args.fda else "")
                + (f"- Also compared: {args.compared}\n" if args.compared else "")
                + f"\n```\n{research.explain(c)}\n```")
        if args.dry_run:
            print(f"\n(dry run) Would buy {qty} {t} at ${limit:.2f}, filling {when}. Nothing placed.")
            return
        details = {"reason": args.reason, "mcap": args.mcap, "earnings": args.earnings, "fda": args.fda,
                   "analyst": args.analyst, "compared": args.compared, "score": c["score"]}
        oid = b.buy(t, qty, limit, premarket, details)
        log(state, "buy order", text, {"ticker": t, "qty": qty, "limit": limit, "order_id": oid, "checklist": c, **details})
        notify.send(cfg, "Research agent (paper)",
                    f"Buy order: {qty} {t} at ${limit:.2f}, fills {when}.\nTarget ${limit*(1+cfg['take_profit']):.2f} · "
                    f"stop ${limit*(1-cfg['stop_loss']):.2f}\n{args.reason}")
        print(f"\nOrder placed: {qty} {t} at ${limit:.2f} (paper), fills {when}.")
        for _ in range(6):                         # a pre-market or open-market order may fill right away
            time.sleep(5)
            events = b.refresh(research.latest_price)
            if events:
                report_events(cfg, state, b, events)
                break
        if b.ledger["pending"]:
            print("Not filled yet. Run the research again after 9:30 AM ET: the sell orders are placed once it fills.")
    finally:
        b.close()


def cmd_skip(cfg, state, args):
    text = f"**No buy today.** {args.reason}" + (f"\n\nCompared: {args.compared}" if args.compared else "")
    log(state, "no buy", text, {"reason": args.reason, "compared": args.compared})
    print("Recorded: no buy today.")


def cmd_test_paper(cfg, state, args):
    b = PaperUSBroker(cfg, state)
    try:
        t = args.ticker.upper()
        ok, detail = b.test_premarket(t, research.latest_price(t))
        print(f"Paper account {b.acc_id}: pre-market orders {'ARE' if ok else 'are NOT'} allowed ({detail}).")
        print("The agent will " + ("buy right away in pre-market." if ok else "place its orders to fill at 9:30 AM ET."))
        if market_time() != "premarket":
            print("Note: for the clearest answer, run this test between 4:00 and 9:30 AM ET.")
    finally:
        b.close()


def cmd_log(cfg, state, args):
    p = state / "research-log.md"
    entries = p.read_text().split("\n## ")[1:] if p.exists() else []
    for e in entries[-args.n:]:
        print("## " + e.strip() + "\n")
    if not entries:
        print("The log is empty.")
    try:
        ledger = json.loads((state / "ledger.json").read_text())
        trades = ledger["trades"]
        wins = sum(t["result"] == "win" for t in trades)
        print(f"Trades: {wins} won, {len(trades) - wins} lost, total {'+' if sum(t['pnl'] for t in trades) >= 0 else '-'}"
              f"${abs(sum(t['pnl'] for t in trades)):.2f}")
    except (OSError, ValueError, KeyError):
        pass


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default="config-research.yaml")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    s = sub.add_parser("signals")
    s.add_argument("tickers", nargs="+")
    s.add_argument("--analyst-upgrade", default="", help="comma-separated tickers with a recent upgrade or higher target")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("buy")
    s.add_argument("ticker")
    s.add_argument("--mcap", type=float, required=True, help="company value in US dollars, from a source")
    s.add_argument("--reason", required=True)
    s.add_argument("--analyst", default="")
    s.add_argument("--earnings", default="")
    s.add_argument("--fda", default="")
    s.add_argument("--compared", default="")
    s.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("skip")
    s.add_argument("--reason", required=True)
    s.add_argument("--compared", default="")
    s = sub.add_parser("test-paper")
    s.add_argument("ticker", nargs="?", default="AAPL")
    s = sub.add_parser("log")
    s.add_argument("-n", type=int, default=10)
    args = p.parse_args()
    cfg, state = load(ROOT / args.config)
    fn = {"status": cmd_status, "signals": cmd_signals, "buy": cmd_buy, "skip": cmd_skip,
          "test-paper": cmd_test_paper, "log": cmd_log}[args.command]
    try:
        if args.command in ("signals", "log", "skip"):
            fn(cfg, state, args)
        else:
            with RunLock(ROOT / "run.lock"):
                fn(cfg, state, args)
    except (BrokerError, data.DataError) as e:
        sys.exit(f"Stopped, nothing further done: {e}")


if __name__ == "__main__":
    main()
