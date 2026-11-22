"""Brokers: a local paper simulator and moomoo (real money, via OpenD)."""
import datetime as dt
import json
import os
import socket
import time
import uuid
from pathlib import Path
from zoneinfo import ZoneInfo


class BrokerError(RuntimeError):
    pass


REAL_CASH_COLUMN = {"CAD": "ca_cash", "USD": "us_cash"}  # moomoo accinfo columns


def currency_of(u: dict) -> str:
    """US-listed (NASDAQ) entries trade in USD; TSX entries in CAD."""
    return "USD" if str(u.get("market", "")).upper() == "US" else "CAD"


def moomoo_code(u: dict) -> str:
    """moomoo's symbol for a universe entry: given explicitly, CA.<symbol> for a
    .TO ticker, or US.<symbol> for an entry with market: US."""
    if u.get("moomoo"):
        return u["moomoo"]
    t = u["ticker"]
    if currency_of(u) == "USD":
        if "." in t or "-" in t:
            raise BrokerError(f"{t}: give its moomoo symbol explicitly in the config")
        return "US." + t
    if not t.endswith(".TO") or "-" in t:
        raise BrokerError(f"{t}: give its moomoo symbol explicitly in the config")
    return "CA." + t[:-3]


def starting_cash(cfg: dict) -> dict:
    return {"CAD": float(cfg["capital_cad"]), "USD": float(cfg.get("capital_usd", 0))}


def new_state(cfg: dict) -> dict:
    """A fresh account: the starting money is written down once, here. Later edits to
    capital_* in the config don't change it, so profit/loss and the kill switch stay
    measured against what the agent was really given."""
    return {"cash": starting_cash(cfg), "start": starting_cash(cfg), "positions": {}}


def migrate_cash(state: dict, cfg: dict) -> dict:
    """Bring older records up to date. Older versions kept one CAD cash number and
    didn't record the starting money; cash is now kept per currency."""
    if not isinstance(state["cash"], dict):
        state["cash"] = {"CAD": float(state["cash"])}
        state.setdefault("start", {"CAD": float(cfg["capital_cad"])})
    state.setdefault("start", {c: v for c, v in starting_cash(cfg).items() if c in state["cash"]})
    for cur, amount in starting_cash(cfg).items():
        if cur not in state["cash"]:  # a currency added later is new money for the agent
            state["cash"][cur] = amount
            state["start"][cur] = state["start"].get(cur, 0.0) + amount
    return state


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
        self.currency = {u["ticker"]: currency_of(u) for u in cfg["universe"]}
        if self.path.exists():
            self.state = migrate_cash(json.loads(self.path.read_text()), cfg)
        else:
            self.state = new_state(cfg)
        self._save()

    def _save(self):
        write_json_atomic(self.path, self.state)

    def cash(self, cur: str = "CAD") -> float:
        return self.state["cash"].get(cur, 0.0)

    def own_cash(self) -> dict:
        return dict(self.state["cash"])

    def start(self) -> dict:
        return dict(self.state["start"])

    def positions(self) -> dict:
        return dict(self.state["positions"])

    def frozen(self) -> set:
        return set()

    def place(self, ticker: str, side: str, qty: int, limit: float) -> dict:
        pos = self.state["positions"]
        cash = self.state["cash"]
        cur = self.currency.get(ticker, "CAD")
        cost = qty * limit
        if side == "BUY":
            if cost + self.commission > cash.get(cur, 0.0) + 1e-9:
                raise BrokerError(f"insufficient {cur} cash for {qty} {ticker}")
            cash[cur] -= cost + self.commission
            pos[ticker] = pos.get(ticker, 0) + qty
        else:
            if pos.get(ticker, 0) < qty:
                raise BrokerError(f"cannot sell {qty} {ticker}, hold {pos.get(ticker, 0)}")
            cash[cur] = cash.get(cur, 0.0) + cost - self.commission
            pos[ticker] -= qty
            if pos[ticker] == 0:
                del pos[ticker]
        self._save()
        return {"status": "filled", "price": limit}

    def reserved(self) -> dict:
        return {}

    def pending(self) -> list:
        return []

    def settle(self):
        pass

    def close(self):
        pass


def open_trade_context(m: dict):
    """Connect to OpenD. moomoo's own client retries forever if OpenD isn't there,
    so check the port first and fail with a clear message instead."""
    try:
        socket.create_connection((m["host"], m["port"]), timeout=5).close()
    except OSError:
        raise BrokerError(
            f"can't reach OpenD at {m['host']}:{m['port']}. Open the OpenD program, log in "
            "with your moomoo account, and try again."
        )
    import moomoo as mm

    return mm.OpenSecTradeContext(
        filter_trdmarket=mm.TrdMarket.CA, host=m["host"], port=m["port"],
        security_firm=getattr(mm.SecurityFirm, m["security_firm"]),
    )


def real_accounts(ctx) -> list[dict]:
    """The real-money accounts OpenD can see, as [{acc_id, acc_type, status}]."""
    import moomoo as mm

    ret, df = ctx.get_acc_list()
    if ret != mm.RET_OK:
        raise BrokerError(f"moomoo get_acc_list failed: {df}")
    out = []
    for _, r in df.iterrows():
        if str(r["trd_env"]) != mm.TrdEnv.REAL:
            continue
        status = str(r["acc_status"]) if "acc_status" in df.columns else "ACTIVE"
        markets = r["trdmarket_auth"] if "trdmarket_auth" in df.columns else []
        markets = ",".join(str(m) for m in markets) if isinstance(markets, (list, tuple)) else str(markets or "")
        out.append({"acc_id": int(r["acc_id"]), "acc_type": str(r["acc_type"]), "status": status, "markets": markets})
    return out


def describe(accounts: list[dict]) -> str:
    return "\n".join(
        f"  {a['acc_type']:<8} acc_id {a['acc_id']}  ({a['status']})" + (f"  markets: {a['markets']}" if a.get("markets") else "")
        for a in accounts
    ) or "  (none)"


def choose_account(ctx, m: dict) -> tuple[int, str]:
    """Pick exactly one account, by type (and acc_id if several share a type).
    Never falls back to 'the first account'."""
    want = str(m.get("account_type", "")).upper()
    acc_id = int(m.get("acc_id") or 0)
    accounts = [a for a in real_accounts(ctx) if a["status"] != "DISABLED"]
    if acc_id:
        match = [a for a in accounts if a["acc_id"] == acc_id]
        if not match:
            raise BrokerError(f"acc_id {acc_id} is not one of your active accounts:\n{describe(accounts)}")
        if want and match[0]["acc_type"] != want:
            raise BrokerError(
                f"acc_id {acc_id} is a {match[0]['acc_type']} account, but config.yaml says account_type: {want}. "
                "Fix one of them so they agree."
            )
        return acc_id, match[0]["acc_type"]
    if not want:
        raise BrokerError("set moomoo.account_type in config.yaml (for example TFSA)")
    match = [a for a in accounts if a["acc_type"] == want]
    if len(match) == 1:
        return match[0]["acc_id"], want
    if not match:
        raise BrokerError(f"no active {want} account found. Your accounts:\n{describe(accounts)}")
    raise BrokerError(f"you have {len(match)} {want} accounts; set moomoo.acc_id in config.yaml to one of:\n{describe(match)}")


def list_accounts(cfg: dict) -> str:
    """Show the accounts OpenD can see and which one the agent would use (no trading, no password)."""
    m = cfg["moomoo"]
    ctx = open_trade_context(m)
    try:
        text = "Your moomoo accounts:\n" + describe(real_accounts(ctx))
        try:
            acc_id, acc_type = choose_account(ctx, m)
            text += f"\n\nThe agent will use: {acc_type} account {acc_id}"
        except BrokerError as e:
            text += f"\n\nThe agent will NOT trade until this is fixed: {e}"
        return text
    finally:
        ctx.close()


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

    It trades exactly one account, chosen by type (config: moomoo.account_type,
    TFSA by default); it refuses to start if that is ambiguous.

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
        self.wait = m["fill_wait_seconds"]
        self.to_mm = {u["ticker"]: moomoo_code(u) for u in cfg["universe"]}
        self.from_mm = {v: k for k, v in self.to_mm.items()}
        self.currency = {u["ticker"]: currency_of(u) for u in cfg["universe"]}
        self.path = state_dir / "live_ledger.json"
        if self.path.exists():
            self.ledger = migrate_cash(json.loads(self.path.read_text()), cfg)
        else:
            self.ledger = {**new_state(cfg), "pending": []}
        pwd = os.environ.get("MOOMOO_TRADE_PASSWORD")
        if not pwd:
            raise BrokerError("set MOOMOO_TRADE_PASSWORD to trade live")
        self.ctx = open_trade_context(m)
        self.acc_id, self.acc_type = choose_account(self.ctx, m)
        owner = self.ledger.setdefault("acc_id", self.acc_id)
        if owner != self.acc_id:
            raise BrokerError(
                f"the ledger belongs to account {owner}, not {self.acc_id}. The agent's records are per "
                "account; switching accounts would let it sell shares it never bought there."
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
        cash = self.ledger["cash"]
        t, cur = entry["ticker"], entry.get("currency", "CAD")
        if entry["side"] == "BUY":
            cash[cur] = cash.get(cur, 0.0) + entry["qty"] * entry["limit"] - filled * avg  # refund the unused reservation
            pos[t] = pos.get(t, 0) + filled
        else:
            cash[cur] = cash.get(cur, 0.0) + filled * avg
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

    def cash(self, cur: str = "CAD") -> float:
        """What the agent can spend now in this currency: its own cash per the ledger,
        but never more than that currency's cash really in the account. It never
        borrows and never converts currencies."""
        df = self._ok(
            self.ctx.accinfo_query(currency=self.mm.Currency.CAD, refresh_cache=True, **self._args()),
            "accinfo_query",
        )
        col = REAL_CASH_COLUMN[cur]
        real = float(df[col][0]) if col in df.columns and df[col][0] == df[col][0] else 0.0
        return min(real, self.ledger["cash"].get(cur, 0.0))

    def own_cash(self) -> dict:
        """The agent's cash per its ledger, by currency."""
        return dict(self.ledger["cash"])

    def start(self) -> dict:
        """The money the agent was given when it started, by currency."""
        return dict(self.ledger["start"])

    def positions(self) -> dict:
        """The agent's shares only: never more than the ledger, never more than really held."""
        real = self._real_positions()
        out = {t: min(q, real.get(t, 0)) for t, q in self.ledger["positions"].items()}
        return {t: q for t, q in out.items() if q > 0}

    def frozen(self) -> set:
        """Stocks where moomoo shows fewer shares than the ledger says the agent owns
        (sold by hand, or not visible). The agent doesn't trade them until that's fixed."""
        real = self._real_positions()
        return {t for t, q in self.ledger["positions"].items() if q > real.get(t, 0)}

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
            cur = self.currency[ticker]
            if qty * limit > self.ledger["cash"].get(cur, 0.0) + 1e-9:
                raise BrokerError(f"refusing to buy {qty} {ticker}: over the agent's {cur} cash")
        else:
            raise BrokerError(f"unknown side {side!r}")

        # Write the order down (and reserve its cash or shares) BEFORE sending it.
        entry = {
            "order_id": None, "tag": "ia-" + uuid.uuid4().hex[:12], "ticker": ticker,
            "side": side, "qty": qty, "limit": limit, "currency": self.currency[ticker], "date": today(),
        }
        if side == "BUY":
            self.ledger["cash"][entry["currency"]] -= qty * limit
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

    def reserved(self) -> dict:
        """Value held by orders that haven't finished, by currency: cash set aside
        for buys, shares set aside for sells (at their limit price)."""
        out = {}
        for o in self.ledger["pending"]:
            cur = o.get("currency", "CAD")
            out[cur] = out.get(cur, 0.0) + o["qty"] * o["limit"]
        return out

    def pending(self) -> list:
        return list(self.ledger["pending"])

    def settle(self):
        time.sleep(self.wait)
        self.reconcile()

    def close(self):
        self.ctx.close()
