"""Brokers: a local paper simulator and moomoo (real money, via OpenD)."""
import json
import os
import time
from pathlib import Path


class BrokerError(RuntimeError):
    pass


class PaperBroker:
    """Simulated account. Orders fill instantly at the limit price."""

    live = False

    def __init__(self, cfg: dict, state_dir: Path):
        self.path = state_dir / "paper_account.json"
        self.commission = cfg["paper"]["commission_cad"]
        if self.path.exists():
            self.state = json.loads(self.path.read_text())
        else:
            self.state = {"cash": float(cfg["capital_cad"]), "positions": {}}
            self._save()

    def _save(self):
        self.path.write_text(json.dumps(self.state, indent=2))

    def cash(self) -> float:
        return self.state["cash"]

    def positions(self) -> dict:
        return dict(self.state["positions"])

    def place(self, ticker: str, side: str, qty: int, limit: float) -> dict:
        pos = self.state["positions"]
        cost = qty * limit
        if side == "BUY":
            if cost + self.commission > self.state["cash"] + 1e-9:
                raise BrokerError(f"insufficient cash for {qty} {ticker}")
            self.state["cash"] -= cost + self.commission
            pos[ticker] = pos.get(ticker, 0) + qty
        else:
            if pos.get(ticker, 0) < qty:
                raise BrokerError(f"cannot sell {qty} {ticker}, hold {pos.get(ticker, 0)}")
            self.state["cash"] += cost - self.commission
            pos[ticker] -= qty
            if pos[ticker] == 0:
                del pos[ticker]
        self._save()
        return {"status": "filled", "price": limit}

    def reserved(self) -> float:
        return 0.0

    def pending(self) -> list:
        return []

    def settle(self):
        pass

    def close(self):
        pass


FINAL_STATUSES = {
    "FILLED_ALL", "CANCELLED_ALL", "CANCELLED_PART", "DELETED", "DISABLED",
    "FAILED", "FILL_CANCELLED", "SUBMIT_FAILED", "TIMEOUT",
}


class MoomooBroker:
    """Real orders on a Moomoo Financial Canada account through the OpenD gateway.

    Requires OpenD running and logged in on this machine, and the trade
    password in the MOOMOO_TRADE_PASSWORD environment variable.
    moomoo has no paper trading for the Canadian market, so this is REAL money.

    The agent can share an account with your own investments. It keeps its own
    ledger (state/live_ledger.json) of the cash it was given (capital_cad) and
    the shares it bought, and only ever sells shares or spends cash from that
    ledger. Your other holdings and cash are never touched.
    """

    live = True

    def __init__(self, cfg: dict, state_dir: Path):
        import moomoo as mm

        self.mm = mm
        m = cfg["moomoo"]
        self.acc_id = m["acc_id"]
        self.wait = m["fill_wait_seconds"]
        self.to_mm = {u["ticker"]: u["moomoo"] for u in cfg["universe"]}
        self.from_mm = {v: k for k, v in self.to_mm.items()}
        self.path = state_dir / "live_ledger.json"
        if self.path.exists():
            self.ledger = json.loads(self.path.read_text())
        else:
            self.ledger = {"cash": float(cfg["capital_cad"]), "positions": {}, "pending": []}
        pwd = os.environ.get("MOOMOO_TRADE_PASSWORD")
        if not pwd:
            raise BrokerError("set MOOMOO_TRADE_PASSWORD to trade live")
        self.ctx = mm.OpenSecTradeContext(
            filter_trdmarket=mm.TrdMarket.CA,
            host=m["host"],
            port=m["port"],
            security_firm=getattr(mm.SecurityFirm, m["security_firm"]),
        )
        self._ok(self.ctx.unlock_trade(pwd), "unlock_trade")
        self.reconcile()

    def _ok(self, result, what):
        ret, data = result
        if ret != self.mm.RET_OK:
            raise BrokerError(f"moomoo {what} failed: {data}")
        return data

    def _args(self):
        return {"trd_env": self.mm.TrdEnv.REAL, "acc_id": self.acc_id}

    def _save(self):
        self.path.write_text(json.dumps(self.ledger, indent=2))

    def _find_order(self, order):
        df = self._ok(self.ctx.order_list_query(order_id=order["order_id"], refresh_cache=True, **self._args()), "order_list_query")
        if df.empty:
            df = self._ok(
                self.ctx.history_order_list_query(start=order["date"], end=time.strftime("%Y-%m-%d"), **self._args()),
                "history_order_list_query",
            )
            df = df[df["order_id"].astype(str) == order["order_id"]]
        return None if df.empty else df.iloc[0]

    def reconcile(self):
        """Book fills of earlier orders into the ledger. Cash for pending buys and
        shares for pending sells were reserved when the order was placed."""
        still_pending = []
        for o in self.ledger["pending"]:
            row = self._find_order(o)
            if row is None or str(row["order_status"]) not in FINAL_STATUSES:
                still_pending.append(o)
                continue
            filled = int(row["dealt_qty"] or 0)
            avg = float(row["dealt_avg_price"] or 0)
            pos = self.ledger["positions"]
            if o["side"] == "BUY":
                self.ledger["cash"] += o["qty"] * o["limit"] - filled * avg  # refund the unused reservation
                pos[o["ticker"]] = pos.get(o["ticker"], 0) + filled
            else:
                self.ledger["cash"] += filled * avg
                pos[o["ticker"]] = pos.get(o["ticker"], 0) + (o["qty"] - filled)  # unsold shares come back
            if pos.get(o["ticker"]) == 0:
                del pos[o["ticker"]]
        self.ledger["pending"] = still_pending
        self._save()

    def cash(self) -> float:
        df = self._ok(
            self.ctx.accinfo_query(currency=self.mm.Currency.CAD, refresh_cache=True, **self._args()),
            "accinfo_query",
        )
        return min(float(df["ca_cash"][0]), self.ledger["cash"])

    def positions(self) -> dict:
        df = self._ok(self.ctx.position_list_query(refresh_cache=True, **self._args()), "position_list_query")
        real = {self.from_mm[r["code"]]: int(r["qty"]) for _, r in df.iterrows() if r["code"] in self.from_mm}
        out = {t: min(q, real.get(t, 0)) for t, q in self.ledger["positions"].items()}
        return {t: q for t, q in out.items() if q > 0}

    def place(self, ticker: str, side: str, qty: int, limit: float) -> dict:
        limit = round(limit, 2)
        if side == "SELL" and self.ledger["positions"].get(ticker, 0) < qty:
            raise BrokerError(f"refusing to sell {qty} {ticker}: the agent only owns {self.ledger['positions'].get(ticker, 0)}")
        if side == "BUY" and qty * limit > self.ledger["cash"] + 1e-9:
            raise BrokerError(f"refusing to buy {qty} {ticker}: over the agent's cash")
        df = self._ok(
            self.ctx.place_order(
                price=limit,
                qty=qty,
                code=self.to_mm[ticker],
                trd_side=self.mm.TrdSide.BUY if side == "BUY" else self.mm.TrdSide.SELL,
                order_type=self.mm.OrderType.NORMAL,  # limit order
                time_in_force=self.mm.TimeInForce.DAY,
                remark="invest-agent",
                **self._args(),
            ),
            "place_order",
        )
        order_id = str(df["order_id"][0])
        if side == "BUY":
            self.ledger["cash"] -= qty * limit
        else:
            self.ledger["positions"][ticker] -= qty
        self.ledger["pending"].append(
            {"order_id": order_id, "ticker": ticker, "side": side, "qty": qty, "limit": limit, "date": time.strftime("%Y-%m-%d")}
        )
        self._save()
        return {"status": "submitted", "order_id": order_id, "price": limit}

    def reserved(self) -> float:
        """Value held by orders that haven't finished: cash set aside for buys,
        shares set aside for sells (at their limit price)."""
        return sum(o["qty"] * o["limit"] for o in self.ledger["pending"])

    def pending(self) -> list:
        return list(self.ledger["pending"])

    def settle(self):
        time.sleep(self.wait)
        self.reconcile()

    def close(self):
        self.ctx.close()
