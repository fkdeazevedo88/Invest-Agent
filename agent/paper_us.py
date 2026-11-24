"""The research agent's broker: moomoo PAPER trading for US stocks, through OpenD.

PAPER ONLY. Every call to moomoo passes trd_env=SIMULATE, written into this file; there is no
setting or flag that changes it, and the agent refuses to start if the chosen account isn't a
paper account. Same safety rules as the other agents: it only spends its own cash (no
borrowing), only buys then sells (no short selling), only sells shares it bought itself, and
holds one stock at a time.
"""
import datetime as dt
import json
import uuid
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

from agent.brokers import FINAL_STATUSES, BrokerError, open_trade_context, write_json_atomic

ET = ZoneInfo("America/New_York")
PAPER = "SIMULATE"


def today() -> dt.date:
    return dt.datetime.now(ET).date()


def trading_days_between(start: str, end: dt.date) -> int:
    """Weekdays from the buy date to `end` (US holidays not counted out)."""
    return int(np.busday_count(dt.date.fromisoformat(start), end))


class PaperUSBroker:
    def __init__(self, cfg: dict, state_dir: Path):
        import moomoo as mm

        self.mm, self.cfg = mm, cfg
        self.path = state_dir / "ledger.json"
        self.caps_path = state_dir / "paper_caps.json"
        m = {**cfg["moomoo"]}
        self.ctx = open_trade_context({**m, "market": "US"})
        try:
            self.acc_id = self._choose_paper_account(m.get("acc_id") or 0)
        except Exception:
            self.ctx.close()
            raise
        if self.path.exists():
            self.ledger = json.loads(self.path.read_text())
        else:
            capital = float(cfg["capital_usd"])
            self.ledger = {"cash": capital, "start": capital, "pending": None, "position": None, "trades": []}
        owner = self.ledger.setdefault("acc_id", self.acc_id)
        if owner != self.acc_id:
            self.ctx.close()
            raise BrokerError(f"the research ledger belongs to paper account {owner}, not {self.acc_id}")
        self._save()

    # ------------------------------------------------------------------ moomoo plumbing
    def _ok(self, result, what):
        ret, data = result
        if ret != self.mm.RET_OK:
            raise BrokerError(f"moomoo {what} failed: {data}")
        return data

    def _args(self):
        return {"trd_env": PAPER, "acc_id": self.acc_id}       # paper, always

    def _choose_paper_account(self, wanted: int) -> int:
        df = self._ok(self.ctx.get_acc_list(), "get_acc_list")
        paper = [r for _, r in df.iterrows() if str(r["trd_env"]) == PAPER]
        if wanted:
            paper = [r for r in paper if int(r["acc_id"]) == int(wanted)]
        stock = [r for r in paper if str(r.get("sim_acc_type", "STOCK")) in ("STOCK", "STOCK_AND_OPTION", "N/A")]
        chosen = stock or paper
        if not chosen:
            raise BrokerError("no moomoo US paper trading account found. In the moomoo app, open Paper Trading "
                              "and start a US stock paper account, then try again.")
        return int(chosen[0]["acc_id"])

    def _save(self):
        write_json_atomic(self.path, self.ledger)

    def caps(self) -> dict:
        try:
            return json.loads(self.caps_path.read_text())
        except (OSError, ValueError):
            return {}

    def _set_cap(self, **kw):
        write_json_atomic(self.caps_path, {**self.caps(), **kw})

    def close(self):
        self.ctx.close()

    def real_qty(self, ticker: str) -> int:
        df = self._ok(self.ctx.position_list_query(refresh_cache=True, **self._args()), "position_list_query")
        return int(sum(int(r["qty"]) for _, r in df.iterrows() if r["code"] == "US." + ticker))

    def real_cash(self) -> float:
        df = self._ok(self.ctx.accinfo_query(currency=self.mm.Currency.USD, refresh_cache=True, **self._args()),
                      "accinfo_query")
        for col in ("us_cash", "cash"):
            if col in df.columns and df[col][0] == df[col][0]:
                return float(df[col][0])
        return 0.0

    def _place(self, ticker, side, qty, price, kind="NORMAL", tif="DAY", outside=False, aux=None, tag=None):
        df = self._ok(self.ctx.place_order(
            price=round(price, 2), qty=qty, code="US." + ticker,
            trd_side=getattr(self.mm.TrdSide, side), order_type=getattr(self.mm.OrderType, kind),
            time_in_force=getattr(self.mm.TimeInForce, tif), fill_outside_rth=outside,
            aux_price=None if aux is None else round(aux, 2), remark=tag, **self._args()), "place_order")
        return str(df["order_id"][0])

    def _cancel(self, order_id):
        if order_id:
            ret, data = self.ctx.modify_order(self.mm.ModifyOrderOp.CANCEL, order_id, 0, 0, **self._args())
            if ret != self.mm.RET_OK and "status" not in str(data).lower():
                raise BrokerError(f"moomoo cancel of order {order_id} failed: {data}")

    def _order(self, order_id=None, tag=None, since=None):
        frames = [self._ok(self.ctx.order_list_query(refresh_cache=True, **self._args()), "order_list_query")]
        start = (dt.date.fromisoformat(since) - dt.timedelta(days=1)).isoformat() if since else None
        frames.append(self._ok(self.ctx.history_order_list_query(start=start or "", end=today().isoformat(),
                                                                 **self._args()), "history_order_list_query"))
        for df in frames:
            if df is None or df.empty:
                continue
            hit = df[df["order_id"].astype(str) == order_id] if order_id else df[df["remark"].astype(str) == tag]
            if not hit.empty:
                return hit.iloc[0]
        return None

    # ------------------------------------------------------------------ actions
    def test_premarket(self, ticker: str, price: float) -> tuple[bool, str]:
        """Place a tiny paper buy far below the market with pre-market allowed, then cancel it."""
        try:
            oid = self._place(ticker, "BUY", 1, price * 0.5, outside=True, tag="ia-test")
        except BrokerError as e:
            self._set_cap(premarket_ok=False, premarket_tested=today().isoformat(), premarket_detail=str(e))
            return False, str(e)
        self._cancel(oid)
        self._set_cap(premarket_ok=True, premarket_tested=today().isoformat())
        return True, "accepted (and cancelled)"

    def buy(self, ticker: str, qty: int, limit: float, premarket: bool, details: dict) -> str:
        if self.ledger["position"] or self.ledger["pending"]:
            raise BrokerError("already holding a stock (or an order is waiting): one stock at a time")
        if not isinstance(qty, int) or qty <= 0:
            raise BrokerError(f"refusing order with quantity {qty!r}")
        cost = qty * round(limit, 2)
        if cost > self.ledger["cash"] + 1e-9 or cost > float(self.cfg["capital_usd"]) + 1e-9:
            raise BrokerError(f"refusing: ${cost:.2f} is more than the agent's ${self.ledger['cash']:.2f}")
        if cost > self.real_cash() + 1e-9:
            raise BrokerError("refusing: not enough cash really in the paper account (it never borrows)")
        tag = "ia-" + uuid.uuid4().hex[:12]
        self.ledger["cash"] -= cost                                   # reserve before sending
        self.ledger["pending"] = {"side": "BUY", "ticker": ticker, "qty": qty, "limit": round(limit, 2),
                                  "tag": tag, "order_id": None, "date": today().isoformat(), "details": details}
        self._save()
        oid = self._place(ticker, "BUY", qty, limit, outside=premarket, tag=tag)
        self.ledger["pending"]["order_id"] = oid
        self._save()
        return oid

    def _settle_pending(self) -> list[str]:
        """Book a finished buy or sell. Returns events for alerts."""
        p, events = self.ledger["pending"], []
        if not p:
            return events
        row = self._order(p.get("order_id"), p["tag"], p["date"])
        if row is None:
            if not p.get("order_id") and p["date"] < today().isoformat():
                if p["side"] == "BUY":
                    self.ledger["cash"] += p["qty"] * p["limit"]        # never reached moomoo
                self.ledger["pending"] = None
            return events
        p["order_id"] = str(row["order_id"])
        if str(row["order_status"]) not in FINAL_STATUSES:
            return events
        filled, avg = int(row["dealt_qty"] or 0), float(row["dealt_avg_price"] or 0)
        self.ledger["pending"] = None
        if p["side"] == "BUY":
            self.ledger["cash"] += p["qty"] * p["limit"] - filled * avg   # refund what wasn't spent
            if filled:
                self.ledger["position"] = {
                    "ticker": p["ticker"], "qty": filled, "entry": avg, "date": today().isoformat(),
                    "bought": p["date"], "tp": round(avg * (1 + self.cfg["take_profit"]), 2),
                    "sl": round(avg * (1 - self.cfg["stop_loss"]), 2), "tp_order": None, "sl_order": None,
                    "tp_mode": "watch", "sl_mode": "watch", **p["details"]}
                events.append(f"BOUGHT {filled} {p['ticker']} at ${avg:.2f}")
                events += self._place_exits()
            else:
                events.append(f"buy order for {p['ticker']} expired unfilled")
        else:
            pos = self.ledger["position"]
            if filled and pos:
                events.append(self._close(filled, avg, p["why"]))
        return events

    def _place_exits(self) -> list[str]:
        """As soon as the buy has filled: a -5% stop and a +7% limit sell at moomoo, both good
        till cancelled (GTC). Any exit that isn't resting at moomoo yet is (re)tried on every run;
        until it is, the agent watches that price level itself whenever it runs."""
        pos, events = self.ledger["position"], []
        qty = min(pos["qty"], self.real_qty(pos["ticker"]))
        if not pos.get("sl_order"):
            try:
                pos["sl_order"] = self._place(pos["ticker"], "SELL", qty, pos["sl"], kind="STOP", tif="GTC", aux=pos["sl"])
                pos["sl_mode"] = "moomoo"
                events.append(f"stop loss set at moomoo: sell if it falls to ${pos['sl']:.2f} (good till cancelled)")
            except BrokerError as e:
                events.append(f"stop loss watched by the agent for now (moomoo paper refused it: {e})")
        if not pos.get("tp_order"):
            try:
                pos["tp_order"] = self._place(pos["ticker"], "SELL", qty, pos["tp"], tif="GTC")
                pos["tp_mode"] = "moomoo"
                events.append(f"+{self.cfg['take_profit']*100:.0f}% sell set at moomoo: ${pos['tp']:.2f} (good till cancelled)")
            except BrokerError as e:
                events.append(f"+{self.cfg['take_profit']*100:.0f}% target watched by the agent for now (moomoo paper refused it: {e})")
        self._set_cap(stop_ok=pos["sl_mode"] == "moomoo", two_exits_ok=pos["tp_mode"] == pos["sl_mode"] == "moomoo")
        self._save()
        return events

    def _close(self, qty: int, price: float, why: str) -> str:
        pos = self.ledger["position"]
        pnl = qty * (price - pos["entry"])
        pct = price / pos["entry"] - 1
        self.ledger["cash"] += qty * price
        self.ledger["trades"].append({
            "ticker": pos["ticker"], "qty": qty, "entry": round(pos["entry"], 2), "exit": round(price, 2),
            "pnl": round(pnl, 2), "pct": round(pct * 100, 2), "result": "win" if pnl > 0 else "loss",
            "why_sold": why, "bought": pos["bought"], "sold": today().isoformat(), "reason": pos.get("reason", ""),
        })
        pos["qty"] -= qty
        if pos["qty"] <= 0:
            for leg in ("tp_order", "sl_order"):
                try:
                    self._cancel(pos.get(leg))
                except BrokerError:
                    pass
            self.ledger["position"] = None
        self._save()
        return f"SOLD {qty} {pos['ticker']} at ${price:.2f} ({pct*100:+.1f}%, {'+' if pnl >= 0 else '-'}${abs(pnl):.2f}): {why}"

    def _sell(self, price: float, why: str, keep: str | None = None) -> str:
        """Sell the whole position with a limit order (cancelling resting exits except `keep`)."""
        pos = self.ledger["position"]
        for leg in ("tp_order", "sl_order"):
            if leg != keep:
                self._cancel(pos.get(leg))
                pos[leg] = None
        qty = min(pos["qty"], self.real_qty(pos["ticker"]))
        if qty <= 0:
            raise BrokerError(f"moomoo shows no {pos['ticker']} shares to sell; check the paper account")
        tag = "ia-" + uuid.uuid4().hex[:12]
        self.ledger["pending"] = {"side": "SELL", "ticker": pos["ticker"], "qty": qty, "limit": round(price, 2),
                                  "tag": tag, "order_id": None, "date": today().isoformat(), "why": why}
        self._save()
        self.ledger["pending"]["order_id"] = self._place(pos["ticker"], "SELL", qty, price, tag=tag)
        self._save()
        return f"sell order placed for {qty} {pos['ticker']} at ${price:.2f}: {why}"

    def refresh(self, price_now) -> list[str]:
        """Bring the ledger up to date with moomoo, and apply the selling rules. `price_now` is a
        function ticker -> current price. Returns what happened, for alerts and the log."""
        events = self._settle_pending()
        pos = self.ledger["position"]
        if not pos or self.ledger["pending"]:
            return events
        for leg, why in (("tp_order", f"+{self.cfg['take_profit']*100:.0f}% target reached"),
                         ("sl_order", f"-{self.cfg['stop_loss']*100:.0f}% stop loss")):
            row = self._order(pos[leg], since=pos["date"]) if pos.get(leg) else None
            if row is not None and str(row["order_status"]) in FINAL_STATUSES and int(row["dealt_qty"] or 0) > 0:
                events.append(self._close(int(row["dealt_qty"]), float(row["dealt_avg_price"]), why))
                return events
        if not pos.get("sl_order") or not pos.get("tp_order"):
            events += self._place_exits()               # retry any exit that isn't resting at moomoo yet
            pos = self.ledger["position"]
        price = price_now(pos["ticker"])
        slip = self.cfg["limit_slippage"]
        if pos["tp_mode"] == "watch" and price >= pos["tp"]:
            events.append(self._sell(price * (1 - slip), f"+{self.cfg['take_profit']*100:.0f}% target reached"))
        elif pos["sl_mode"] == "watch" and price <= pos["sl"]:
            events.append(self._sell(price * (1 - slip), f"-{self.cfg['stop_loss']*100:.0f}% stop loss"))
        elif trading_days_between(pos["bought"], today()) >= self.cfg["max_hold_days"]:
            events.append(self._sell(price * (1 - slip), f"held {self.cfg['max_hold_days']} trading days"))
        return events

    def summary(self) -> dict:
        trades = self.ledger["trades"]
        wins = [t for t in trades if t["result"] == "win"]
        return {"wins": len(wins), "losses": len(trades) - len(wins), "trades": len(trades),
                "profit": round(sum(t["pnl"] for t in trades), 2), "cash": round(self.ledger["cash"], 2),
                "start": self.ledger["start"]}


def market_time(now: dt.datetime | None = None) -> str:
    """'premarket' (4:00-9:30 ET), 'open' (9:30-16:00), or 'closed', on weekdays."""
    now = (now or dt.datetime.now(ET)).astimezone(ET)
    if now.weekday() >= 5:
        return "closed"
    t = now.time()
    if dt.time(4, 0) <= t < dt.time(9, 30):
        return "premarket"
    if dt.time(9, 30) <= t < dt.time(16, 0):
        return "open"
    return "closed"

