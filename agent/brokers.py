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

    def settle(self):
        pass

    def close(self):
        pass


class MoomooBroker:
    """Real orders on a Moomoo Financial Canada account through the OpenD gateway.

    Requires OpenD running and logged in on this machine, and the trade
    password in the MOOMOO_TRADE_PASSWORD environment variable.
    moomoo has no paper trading for the Canadian market, so this is REAL money.
    """

    live = True

    def __init__(self, cfg: dict):
        import moomoo as mm

        self.mm = mm
        m = cfg["moomoo"]
        self.acc_id = m["acc_id"]
        self.wait = m["fill_wait_seconds"]
        self.to_mm = {u["ticker"]: u["moomoo"] for u in cfg["universe"]}
        self.from_mm = {v: k for k, v in self.to_mm.items()}
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

    def _ok(self, result, what):
        ret, data = result
        if ret != self.mm.RET_OK:
            raise BrokerError(f"moomoo {what} failed: {data}")
        return data

    def _args(self):
        return {"trd_env": self.mm.TrdEnv.REAL, "acc_id": self.acc_id}

    def cash(self) -> float:
        df = self._ok(
            self.ctx.accinfo_query(currency=self.mm.Currency.CAD, refresh_cache=True, **self._args()),
            "accinfo_query",
        )
        return float(df["ca_cash"][0])

    def positions(self) -> dict:
        df = self._ok(self.ctx.position_list_query(refresh_cache=True, **self._args()), "position_list_query")
        out = {}
        for _, row in df.iterrows():
            if row["code"] in self.from_mm and row["qty"] > 0:  # ignore anything the agent doesn't manage
                out[self.from_mm[row["code"]]] = int(row["qty"])
        return out

    def place(self, ticker: str, side: str, qty: int, limit: float) -> dict:
        df = self._ok(
            self.ctx.place_order(
                price=round(limit, 2),
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
        return {"status": "submitted", "order_id": str(df["order_id"][0]), "price": round(limit, 2)}

    def settle(self):
        time.sleep(self.wait)

    def close(self):
        self.ctx.close()
