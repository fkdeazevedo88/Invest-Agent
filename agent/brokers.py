"""Brokers: a local paper simulator and moomoo (real money, via OpenD)."""
import datetime as dt
import json
import os
import time
import uuid
from pathlib import Path
from zoneinfo import ZoneInfo


class BrokerError(RuntimeError):
    pass


def today() -> str:
    """Trading date on the TSX (Toronto time), YYYY-MM-DD."""
    return dt.datetime.now(ZoneInfo("America/Toronto")).strftime("%Y-%m-%d")


def write_json_atomic(path: Path, obj):
    """Write via a temp file and rename, so a crash never leaves a half-written file."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2))
    os.replace(tmp, path)


class RunLock:
    """Only one run at a time may touch the account or its ledger. Two runs at
    once could each sell the same shares."""

    STALE_SECONDS = 3600  # a lock this old was left by a crashed run

    def __init__(self, path: Path):
        self.path = path

    def __enter__(self):
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if time.time() - self.path.stat().st_mtime < self.STALE_SECONDS:
                    raise BrokerError(
                        f"another run is in progress (lock file {self.path}). "
                        "Wait for it to finish; if none is running, delete that file."
                    )
                self.path.unlink(missing_ok=True)
                continue
            os.write(fd, f"pid {os.getpid()} at {dt.datetime.now().isoformat()}\n".encode())
            os.close(fd)
            return self
        raise BrokerError(f"could not take the lock {self.path}")

    def __exit__(self, *exc):
        self.path.unlink(missing_ok=True)


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
        write_json_atomic(self.path, self.state)

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
ORDER_COLUMNS = ["order_id", "order_status", "dealt_qty", "dealt_avg_price", "remark"]


class MoomooBroker:
    """Real orders on a Moomoo Financial Canada account through the OpenD gateway.

    Requires OpenD running and logged in on this machine, and the trade
    password in the MOOMOO_TRADE_PASSWORD environment variable.
    moomoo has no paper trading for the Canadian market, so this is REAL money.

    SAFETY RULE: the agent never sells anything it did not buy.
    It shares the account with your own investments, so it keeps a ledger
    (state/live_ledger.json) of the cash it was given (capital_cad) and the
    shares it bought, and enforces, immediately before every order:
      * it only trades the ETFs listed in config.yaml;
      * a sell is at most min(shares in the ledger, shares really in the account);
      * a buy is at most the ledger's cash.
    Each order is written to the ledger, with a unique tag, BEFORE it is sent,
    so a crash mid-order can never make the agent forget a sale and sell again.
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
        write_json_atomic(self.path, self.ledger)

    def _find_order(self, entry):
        """Look an order up in moomoo's records: by order id, or, if the id was
        never recorded (crash mid-order), by the unique tag sent as its remark."""
        frames = [self._ok(self.ctx.order_list_query(refresh_cache=True, **self._args()), "order_list_query")]
        since = (dt.date.fromisoformat(entry["date"]) - dt.timedelta(days=1)).isoformat()
        frames.append(self._ok(self.ctx.history_order_list_query(start=since, end=today(), **self._args()), "history_order_list_query"))
        for df in frames:
            if df is None or df.empty:
                continue
            if entry.get("order_id"):
                hit = df[df["order_id"].astype(str) == entry["order_id"]]
            else:
                hit = df[df["remark"].astype(str) == entry["tag"]]
            if not hit.empty:
                return hit.iloc[0]
        return None

    def _release(self, entry, filled, avg):
        """Close out a finished order: book what filled, return what didn't."""
        pos = self.ledger["positions"]
        t = entry["ticker"]
        if entry["side"] == "BUY":
            self.ledger["cash"] += entry["qty"] * entry["limit"] - filled * avg  # refund the unused reservation
            pos[t] = pos.get(t, 0) + filled
        else:
            self.ledger["cash"] += filled * avg
            pos[t] = pos.get(t, 0) + (entry["qty"] - filled)  # unsold shares stay the agent's
        if pos.get(t) == 0:
            del pos[t]

    def reconcile(self):
        """Book the results of earlier orders into the ledger. Cash for pending
        buys and shares for pending sells were reserved when each order was placed."""
        still_pending = []
        for entry in self.ledger["pending"]:
            row = self._find_order(entry)
            if row is None:
                if entry.get("order_id") is None and entry["date"] < today():
                    # Written down but never reached moomoo (not in its records a day later).
                    self._release(entry, 0, 0.0)
                else:
                    still_pending.append(entry)
                continue
            entry["order_id"] = str(row["order_id"])
            if str(row["order_status"]) not in FINAL_STATUSES:
                still_pending.append(entry)
                continue
            self._release(entry, int(row["dealt_qty"] or 0), float(row["dealt_avg_price"] or 0))
        self.ledger["pending"] = still_pending
        self._save()

    def _real_positions(self) -> dict:
        """Shares of the agent's ETFs really in the account (yours and the agent's together)."""
        df = self._ok(self.ctx.position_list_query(refresh_cache=True, **self._args()), "position_list_query")
        real = {}
        for _, r in df.iterrows():
            if r["code"] in self.from_mm:
                t = self.from_mm[r["code"]]
                real[t] = real.get(t, 0) + int(r["qty"])
        return real

    def cash(self) -> float:
        df = self._ok(
            self.ctx.accinfo_query(currency=self.mm.Currency.CAD, refresh_cache=True, **self._args()),
            "accinfo_query",
        )
        return min(float(df["ca_cash"][0]), self.ledger["cash"])

    def positions(self) -> dict:
        """The agent's shares only: never more than the ledger, never more than really held."""
        real = self._real_positions()
        out = {t: min(q, real.get(t, 0)) for t, q in self.ledger["positions"].items()}
        return {t: q for t, q in out.items() if q > 0}

    def place(self, ticker: str, side: str, qty: int, limit: float) -> dict:
        limit = round(limit, 2)
        if ticker not in self.to_mm:
            raise BrokerError(f"refusing to trade {ticker}: not one of the agent's ETFs")
        if not isinstance(qty, int) or qty <= 0:
            raise BrokerError(f"refusing order with quantity {qty!r}")
        if side == "SELL":
            own = self.ledger["positions"].get(ticker, 0)
            real = self._real_positions().get(ticker, 0)
            if qty > min(own, real):
                raise BrokerError(
                    f"refusing to sell {qty} {ticker}: the agent bought {own} and the account holds {real}"
                )
        elif side == "BUY":
            if qty * limit > self.ledger["cash"] + 1e-9:
                raise BrokerError(f"refusing to buy {qty} {ticker}: over the agent's cash")
        else:
            raise BrokerError(f"unknown side {side!r}")

        # Write the order down (and reserve its cash or shares) BEFORE sending it.
        entry = {
            "order_id": None, "tag": "ia-" + uuid.uuid4().hex[:12], "ticker": ticker,
            "side": side, "qty": qty, "limit": limit, "date": today(),
        }
        if side == "BUY":
            self.ledger["cash"] -= qty * limit
        else:
            self.ledger["positions"][ticker] -= qty
        self.ledger["pending"].append(entry)
        self._save()

        # If this fails or crashes, the entry stays pending and the next run settles
        # it from moomoo's records by its tag; the reservation is never undone blindly.
        df = self._ok(
            self.ctx.place_order(
                price=limit,
                qty=qty,
                code=self.to_mm[ticker],
                trd_side=self.mm.TrdSide.BUY if side == "BUY" else self.mm.TrdSide.SELL,
                order_type=self.mm.OrderType.NORMAL,  # limit order
                time_in_force=self.mm.TimeInForce.DAY,
                remark=entry["tag"],
                **self._args(),
            ),
            "place_order",
        )
        entry["order_id"] = str(df["order_id"][0])
        self._save()
        return {"status": "submitted", "order_id": entry["order_id"], "price": limit}

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
