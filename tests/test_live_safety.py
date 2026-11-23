"""Safety tests for live trading: the agent must never sell anything it did not buy.

Runs against a fake moomoo account that already holds the owner's own shares,
including shares of the same ETFs the agent trades. No network, no real account.

    python -m unittest discover -s tests -v
"""
import datetime as dt
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import brokers, strategy  # noqa: E402
from agent.brokers import ORDER_COLUMNS, BrokerError, MoomooBroker, RunLock, list_accounts  # noqa: E402
import run  # noqa: E402

PRICES = {
    "VFV.TO": 190.0, "ZQQ.TO": 190.0, "XIU.TO": 54.0, "XEF.TO": 52.0,
    "XEC.TO": 41.0, "ZAG.TO": 13.5, "CGL-C.TO": 46.0, "CASH.TO": 50.0,
}


class FakeAccount:
    """A moomoo account: cash, holdings and order records."""

    def __init__(self, cash, positions, usd=0.0):
        self.cash = cash                  # CAD
        self.usd = usd                    # USD
        self.pos = dict(positions)
        self.today, self.history, self.sells = [], [], []
        self.fill = True                  # fill orders immediately?
        self.crash_after_accept = False   # order goes through, then the connection dies
        self.fail_before_send = False     # connection dies before moomoo sees the order
        self.ids = 0
        self.accounts = [
            {"acc_id": 111, "trd_env": "REAL", "acc_type": "MARGIN", "acc_status": "ACTIVE"},
            {"acc_id": 222, "trd_env": "REAL", "acc_type": "TFSA", "acc_status": "ACTIVE"},
            {"acc_id": 999, "trd_env": "SIMULATE", "acc_type": "TFSA", "acc_status": "ACTIVE"},
        ]
        self.order_accounts = set()               # every acc_id an order was sent to

    def new_day(self):
        """Market close: unfilled DAY orders expire; today's orders move to history."""
        for o in self.today:
            if o["order_status"] == "SUBMITTED":
                o["order_status"] = "CANCELLED_ALL"
        self.history += self.today
        self.today = []


ACCOUNT: FakeAccount = None
UNKNOWN_CODES = {"CA.BOGUS"}


def frame(rows):
    return pd.DataFrame(rows, columns=ORDER_COLUMNS)


class FakeCtx:
    def __init__(self, **kw):
        pass

    def unlock_trade(self, pwd):
        return 0, None

    def get_acc_list(self):
        return 0, pd.DataFrame(ACCOUNT.accounts)

    def accinfo_query(self, **kw):
        return 0, pd.DataFrame({"ca_cash": [ACCOUNT.cash], "us_cash": [ACCOUNT.usd]})

    def position_list_query(self, **kw):
        return 0, pd.DataFrame({"code": list(ACCOUNT.pos), "qty": list(ACCOUNT.pos.values())})

    def place_order(self, price, qty, code, trd_side, remark=None, acc_id=0, **kw):
        a = ACCOUNT
        a.order_accounts.add(acc_id)
        if a.fail_before_send:
            a.fail_before_send = False
            raise ConnectionError("lost connection before the order was sent")
        usd = code.startswith("US.")
        if trd_side == "SELL":
            if qty > a.pos.get(code, 0):
                raise AssertionError(f"short sale of {qty} {code}")
            a.sells.append((code, qty))
        elif qty * price > (a.usd if usd else a.cash) + 1e-9:
            raise AssertionError(f"buying {qty} {code} needs a margin loan")
        a.ids += 1
        filled = qty if a.fill else 0
        sign = 1 if trd_side == "BUY" else -1
        if usd:
            a.usd -= sign * filled * price
        else:
            a.cash -= sign * filled * price
        a.pos[code] = a.pos.get(code, 0) + sign * filled
        a.today.append({
            "order_id": str(a.ids), "order_status": "FILLED_ALL" if filled else "SUBMITTED",
            "dealt_qty": filled, "dealt_avg_price": price if filled else 0.0, "remark": remark,
        })
        if a.crash_after_accept:
            a.crash_after_accept = False
            raise ConnectionError("lost connection after the order was accepted")
        return 0, pd.DataFrame({"order_id": [str(a.ids)]})

    def acctradinginfo_query(self, order_type, code, price, **kw):
        if code in UNKNOWN_CODES:
            return -1, f"unknown stock {code}"
        return 0, pd.DataFrame({"max_cash_buy": [100]})

    def order_list_query(self, order_id="", **kw):
        rows = [o for o in ACCOUNT.today if not order_id or o["order_id"] == order_id]
        return 0, frame(rows)

    def history_order_list_query(self, **kw):
        return 0, frame(ACCOUNT.history)

    def close(self):
        pass


class Enum:
    def __getattr__(self, name):
        return name


fake_moomoo = types.ModuleType("moomoo")
for _name in ("TrdMarket", "SecurityFirm", "TrdEnv", "Currency", "TrdSide", "OrderType", "TimeInForce"):
    setattr(fake_moomoo, _name, Enum())
fake_moomoo.RET_OK = 0
fake_moomoo.OpenSecTradeContext = FakeCtx


class LiveSafetyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
        self.cfg["claude"]["enabled"] = False
        self.cfg["moomoo"]["fill_wait_seconds"] = 0
        self.cfg["rebalance_every_days"] = 0              # these tests trade on consecutive runs
        self.cfg["moomoo"]["account_type"] = "TFSA"   # most tests use the TFSA (222); margin tests switch
        self.day = dt.date(2026, 11, 23)
        snap = {t: {"price": p, "dollar_volume_20d": 5e7, "signals": {}} for t, p in PRICES.items()}
        patches = [
            mock.patch.dict(sys.modules, {"moomoo": fake_moomoo}),
            mock.patch.dict(os.environ, {"MOOMOO_TRADE_PASSWORD": "000000"}),
            mock.patch.object(run, "STATE", self.tmp),
            mock.patch.object(run, "JOURNAL", self.tmp / "journal.jsonl"),
            mock.patch.object(run, "HALT", self.tmp / "HALT"),
            mock.patch.object(run, "market_open", lambda: True),
            mock.patch.object(run.data, "snapshot", lambda tickers, **kw: snap),
            mock.patch.object(run.data, "fx_rates", lambda currencies: {"CAD": 1.0, "USD": 1.4}),
            mock.patch.object(brokers, "today", lambda: self.day.isoformat()),
            mock.patch.object(brokers.socket, "create_connection", lambda *a, **k: mock.Mock()),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def account(self, cash=5000.0, **holdings):
        global ACCOUNT
        ACCOUNT = FakeAccount(cash, holdings)
        return ACCOUNT

    def cycle(self, target, dry_run=False):
        with mock.patch.object(strategy, "baseline", lambda snap, cfg, **kw: target):
            run.cycle(types.SimpleNamespace(live=True, dry_run=dry_run), self.cfg)

    def next_day(self):
        ACCOUNT.new_day()
        self.day += dt.timedelta(days=1)

    BUY_THREE = {"XIU.TO": 0.34, "XEF.TO": 0.33, "VFV.TO": 0.31}
    ALL_CASH = {"CASH.TO": 1.0}

    def test_never_sells_owners_shares_of_the_same_etf(self):
        acct = self.account(**{"CA.XIU": 10, "CA.ENB": 50})
        self.cycle(self.BUY_THREE)
        self.assertEqual(acct.pos["CA.XIU"], 16)  # agent bought 6
        self.cycle(self.ALL_CASH)                  # agent wants out of everything
        self.assertEqual(acct.pos["CA.XIU"], 10)
        self.assertEqual(acct.pos["CA.ENB"], 50)
        self.assertEqual(sum(q for c, q in acct.sells if c == "CA.XIU"), 6)

    def test_without_a_ledger_nothing_is_sold(self):
        acct = self.account(**{"CA.XIU": 10, "CA.VFV": 3, "CA.ENB": 50})
        self.cycle(self.ALL_CASH)
        self.assertEqual(acct.sells, [])
        self.assertEqual(acct.pos["CA.XIU"], 10)
        self.assertEqual(acct.pos["CA.VFV"], 3)

    def test_last_check_refuses_bad_sells(self):
        acct = self.account(**{"CA.XIU": 10, "CA.ENB": 50})
        b = MoomooBroker(self.cfg, self.tmp)
        with self.assertRaises(BrokerError):
            b.place("ENB.TO", "SELL", 1, 50.0)   # not one of the agent's ETFs
        with self.assertRaises(BrokerError):
            b.place("ENB.TO", "BUY", 1, 50.0)    # not one of the agent's ETFs, even to buy
        b.ledger["positions"]["ENB.TO"] = 50     # even if the ledger were edited to claim it
        with self.assertRaises(BrokerError):
            b.place("ENB.TO", "SELL", 1, 50.0)
        del b.ledger["positions"]["ENB.TO"]
        with self.assertRaises(BrokerError):
            b.place("XIU.TO", "SELL", 1, 54.0)   # the agent bought none
        with self.assertRaises(BrokerError):
            b.place("XIU.TO", "BUY", 0, 54.0)    # nonsense quantity
        self.assertEqual(acct.sells, [])
        self.assertEqual(acct.today, [])

    def test_sell_is_capped_by_what_is_really_held(self):
        acct = self.account(**{"CA.XIU": 10})
        self.cycle(self.BUY_THREE)               # agent owns 6, account holds 16
        acct.pos["CA.XIU"] = 4                   # owner sold 12 by hand
        b = MoomooBroker(self.cfg, self.tmp)
        with self.assertRaises(BrokerError):
            b.place("XIU.TO", "SELL", 6, 54.0)
        self.assertEqual(b.positions()["XIU.TO"], 4)

    def test_crash_after_sell_accepted_does_not_sell_again(self):
        acct = self.account(**{"CA.XIU": 10})
        self.cycle(self.BUY_THREE)
        acct.crash_after_accept = True
        with self.assertRaises(ConnectionError):
            self.cycle(self.ALL_CASH)            # first sell goes through, then the crash
        self.cycle(self.ALL_CASH)                # next run must recognise that sale
        self.next_day()
        self.cycle(self.ALL_CASH)
        self.assertEqual(acct.pos["CA.XIU"], 10)
        for code in ("CA.VFV", "CA.XEF", "CA.XIU"):
            sold = sum(q for c, q in acct.sells if c == code)
            self.assertEqual(sold, {"CA.VFV": 1, "CA.XEF": 6, "CA.XIU": 6}[code], code)

    def test_order_that_never_reached_moomoo_is_released_next_day(self):
        acct = self.account(**{"CA.XIU": 10})
        self.cycle(self.BUY_THREE)
        acct.fail_before_send = True
        with self.assertRaises(ConnectionError):
            self.cycle(self.ALL_CASH)
        self.assertEqual(acct.sells, [])
        self.cycle(self.ALL_CASH)                # same day: unresolved, so no trading
        self.assertEqual(acct.sells, [])
        self.next_day()
        self.cycle(self.ALL_CASH)                # released, then sold exactly once
        self.assertEqual(acct.pos["CA.XIU"], 10)
        self.assertEqual(sum(q for c, q in acct.sells if c == "CA.XIU"), 6)

    def test_unfilled_orders_block_trading_until_they_finish(self):
        acct = self.account(**{"CA.XIU": 10})
        acct.fill = False
        self.cycle(self.BUY_THREE)
        orders = len(acct.today)
        self.cycle(self.BUY_THREE)               # still waiting: must not order again
        self.assertEqual(len(acct.today), orders)

    def test_market_closed_blocks_live_orders_but_not_a_dry_run(self):
        acct = self.account(**{"CA.XIU": 10})
        with mock.patch.object(run, "market_open", lambda: False):
            self.cycle(self.BUY_THREE, dry_run=True)       # allowed: places nothing
            with self.assertRaises(SystemExit):
                self.cycle(self.BUY_THREE)                 # real orders: refused
        self.assertEqual(acct.today, [])

    def test_dry_run_places_nothing(self):
        acct = self.account(**{"CA.XIU": 10})
        self.cycle(self.BUY_THREE, dry_run=True)
        self.assertEqual(acct.today, [])

    def test_uses_the_tfsa_not_the_margin_account(self):
        acct = self.account(**{"CA.XIU": 10})
        self.cycle(self.BUY_THREE)
        self.cycle(self.ALL_CASH)
        self.assertEqual(acct.order_accounts, {222})

    def test_refuses_when_the_chosen_account_type_is_missing(self):
        acct = self.account()
        acct.accounts = [a for a in acct.accounts if a["acc_type"] != "TFSA" or a["trd_env"] != "REAL"]
        with self.assertRaises(BrokerError):
            self.cycle(self.BUY_THREE)
        self.assertEqual(acct.today, [])

    def test_refuses_when_two_accounts_share_the_type(self):
        acct = self.account()
        acct.accounts.append({"acc_id": 333, "trd_env": "REAL", "acc_type": "TFSA", "acc_status": "ACTIVE"})
        with self.assertRaises(BrokerError):
            self.cycle(self.BUY_THREE)
        self.cfg["moomoo"]["acc_id"] = 333       # once picked explicitly, it works
        self.cycle(self.BUY_THREE)
        self.assertEqual(acct.order_accounts, {333})

    def test_refuses_acc_id_that_contradicts_the_type(self):
        acct = self.account()
        self.cfg["moomoo"]["acc_id"] = 111       # the margin account, but account_type says TFSA
        with self.assertRaises(BrokerError):
            self.cycle(self.BUY_THREE)
        self.cfg["moomoo"]["acc_id"] = 12345     # an account that doesn't exist
        with self.assertRaises(BrokerError):
            self.cycle(self.BUY_THREE)
        self.assertEqual(acct.today, [])

    def test_ledger_cannot_be_used_on_another_account(self):
        acct = self.account(**{"CA.XIU": 10})
        self.cycle(self.BUY_THREE)               # ledger now belongs to the TFSA (222)
        self.cfg["moomoo"]["account_type"] = "MARGIN"
        with self.assertRaises(BrokerError):
            self.cycle(self.ALL_CASH)
        self.assertEqual(acct.order_accounts, {222})

    def test_clear_error_when_opend_is_not_running(self):
        self.account()
        with mock.patch.object(brokers.socket, "create_connection", side_effect=ConnectionRefusedError):
            with self.assertRaises(BrokerError):
                self.cycle(self.BUY_THREE)

    def test_margin_account_is_used_when_configured(self):
        acct = self.account(**{"CA.XIU": 10})
        self.cfg["moomoo"]["account_type"] = "MARGIN"
        self.cycle(self.BUY_THREE)
        self.cycle(self.ALL_CASH)
        self.assertEqual(acct.order_accounts, {111})
        self.assertEqual(acct.pos["CA.XIU"], 10)

    def test_margin_account_never_borrows(self):
        self.cfg["moomoo"]["account_type"] = "MARGIN"
        acct = self.account(cash=300.0)          # less than the agent's $1,000
        self.cycle(self.BUY_THREE)               # the fake account fails the test on any borrowing
        self.assertGreaterEqual(acct.cash, 0)
        acct = self.account(cash=-2000.0)        # owner already has a margin loan
        self.tmp = Path(tempfile.mkdtemp())
        with mock.patch.object(run, "STATE", self.tmp), mock.patch.object(run, "JOURNAL", self.tmp / "j"):
            self.cycle(self.BUY_THREE)
        self.assertEqual(acct.today, [])         # no cash of its own to spend: buys nothing

    def test_owner_spending_cash_does_not_trigger_the_kill_switch(self):
        self.cfg["moomoo"]["account_type"] = "MARGIN"
        acct = self.account(**{"CA.XIU": 10})
        self.cycle(self.BUY_THREE)
        acct.cash = 20.0                         # owner spent the rest on their own stocks
        self.cycle(self.BUY_THREE)
        self.assertEqual(acct.sells, [])
        self.assertFalse((self.tmp / "HALT").exists())

    def test_margin_account_never_short_sells(self):
        self.cfg["moomoo"]["account_type"] = "MARGIN"
        acct = self.account(**{"CA.XIU": 10})
        self.cycle(self.BUY_THREE)               # agent owns 6 XIU, account holds 16
        acct.pos["CA.XIU"] = 0                   # owner sold everything by hand
        b = MoomooBroker(self.cfg, self.tmp)
        with self.assertRaises(BrokerError):
            b.place("XIU.TO", "SELL", 1, 54.0)
        self.assertEqual(b.positions().get("XIU.TO", 0), 0)

    def test_accounts_command_shows_the_choice(self):
        self.account()
        self.cfg["moomoo"]["account_type"] = "MARGIN"
        text = list_accounts(self.cfg)
        self.assertIn("MARGIN", text)
        self.assertIn("The agent will use: MARGIN account 111", text)
        self.assertNotIn("999", text)            # paper-trading accounts are not listed
        self.cfg["moomoo"]["account_type"] = "RRSP"
        self.assertIn("will NOT trade", list_accounts(self.cfg))

    def test_only_one_run_at_a_time(self):
        lock = self.tmp / "run.lock"
        with RunLock(lock):
            with self.assertRaises(BrokerError):
                with RunLock(lock):
                    pass
        with RunLock(lock):                      # free again once the first run ends
            pass
        lock.write_text("left by a crash")
        old = time.time() - 2 * RunLock.STALE_SECONDS
        os.utime(lock, (old, old))
        with RunLock(lock):                      # a stale lock does not block forever
            pass


if __name__ == "__main__":
    unittest.main()
