"""Tests for the research agent: paper only, one stock, $200, +7% / -5% / 10 days, its own
records, the chart checklist, alerts and the log. Uses a fake moomoo; no network.

    python -m unittest discover -s tests -v
"""
import datetime as dt
import math
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import brokers, indicators, notify, paper_us, research  # noqa: E402
from agent.brokers import BrokerError  # noqa: E402
import research as cli  # noqa: E402

ORDER_COLS = ["order_id", "order_status", "dealt_qty", "dealt_avg_price", "remark", "code", "trd_side", "order_type", "price", "aux_price", "qty", "time_in_force", "fill_outside_rth"]


class FakePaper:
    """A moomoo account list with a REAL margin account and a US paper account."""

    def __init__(self):
        self.accounts = [
            {"acc_id": 111, "trd_env": "REAL", "acc_type": "MARGIN", "sim_acc_type": "N/A"},
            {"acc_id": 900, "trd_env": "SIMULATE", "acc_type": "CASH", "sim_acc_type": "STOCK"},
        ]
        self.cash, self.pos, self.orders, self.ids = 1_000_000.0, {}, {}, 0
        self.fill_buys = True
        self.refuse_outside = False      # paper refuses pre-market orders
        self.refuse_stop = False         # paper refuses stop orders
        self.refuse_second_sell = False  # paper refuses two sells on the same shares
        self.envs = []                   # every trd_env used

    def fill(self, oid, price):
        o = self.orders[oid]
        o.update(order_status="FILLED_ALL", dealt_qty=o["qty"], dealt_avg_price=price)
        sign = 1 if o["trd_side"] == "BUY" else -1
        self.pos[o["code"]] = self.pos.get(o["code"], 0) + sign * o["qty"]
        self.cash -= sign * o["qty"] * price


FAKE: FakePaper = None


class FakeCtx:
    def __init__(self, **kw):
        self.market = kw.get("filter_trdmarket")

    def _env(self, kw):
        FAKE.envs.append(kw.get("trd_env"))
        assert kw.get("trd_env") == "SIMULATE", f"non-paper call: {kw.get('trd_env')}"

    def get_acc_list(self):
        return 0, pd.DataFrame(FAKE.accounts)

    def accinfo_query(self, **kw):
        self._env(kw)
        return 0, pd.DataFrame({"us_cash": [FAKE.cash], "cash": [FAKE.cash]})

    def position_list_query(self, **kw):
        self._env(kw)
        return 0, pd.DataFrame({"code": list(FAKE.pos), "qty": list(FAKE.pos.values())})

    def place_order(self, price, qty, code, trd_side, order_type="NORMAL", time_in_force="DAY",
                    fill_outside_rth=False, aux_price=None, remark=None, **kw):
        self._env(kw)
        if fill_outside_rth and FAKE.refuse_outside:
            return -1, "outside regular trading hours is not supported for paper trading"
        if order_type == "STOP" and FAKE.refuse_stop:
            return -1, "order type not supported"
        if trd_side == "SELL":
            resting = sum(o["qty"] for o in FAKE.orders.values()
                          if o["code"] == code and o["trd_side"] == "SELL" and o["order_status"] == "SUBMITTED")
            if FAKE.refuse_second_sell and resting:
                return -1, "insufficient sellable quantity"
            if resting + qty > FAKE.pos.get(code, 0) and not FAKE.refuse_second_sell and order_type == "NORMAL" and time_in_force == "DAY":
                pass
        FAKE.ids += 1
        oid = str(FAKE.ids)
        FAKE.orders[oid] = {"order_id": oid, "order_status": "SUBMITTED", "dealt_qty": 0, "dealt_avg_price": 0.0,
                            "remark": remark, "code": code, "trd_side": trd_side, "order_type": order_type,
                            "price": price, "aux_price": aux_price, "qty": qty, "time_in_force": time_in_force,
                            "fill_outside_rth": fill_outside_rth}
        if trd_side == "BUY" and FAKE.fill_buys:
            FAKE.fill(oid, price)
        return 0, pd.DataFrame({"order_id": [oid]})

    def modify_order(self, op, order_id, qty, price, **kw):
        self._env(kw)
        o = FAKE.orders[str(order_id)]
        if o["order_status"] == "SUBMITTED":
            o["order_status"] = "CANCELLED_ALL"
        return 0, None

    def order_list_query(self, **kw):
        self._env(kw)
        return 0, pd.DataFrame(list(FAKE.orders.values()), columns=ORDER_COLS)

    def history_order_list_query(self, **kw):
        self._env(kw)
        return 0, pd.DataFrame([], columns=ORDER_COLS)

    def close(self):
        pass


class Enum:
    def __getattr__(self, name):
        return name


fake_moomoo = types.ModuleType("moomoo")
for _n in ("TrdMarket", "SecurityFirm", "TrdEnv", "Currency", "TrdSide", "OrderType", "TimeInForce", "ModifyOrderOp"):
    setattr(fake_moomoo, _n, Enum())
fake_moomoo.RET_OK = 0
fake_moomoo.OpenSecTradeContext = FakeCtx


class PaperBase(unittest.TestCase):
    def setUp(self):
        global FAKE
        FAKE = FakePaper()
        self.fake = FAKE
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = yaml.safe_load((ROOT / "config-research.yaml").read_text())
        self.cfg["ntfy"]["topic"] = ""
        self.today = dt.date(2026, 11, 24)
        for p in (mock.patch.dict(sys.modules, {"moomoo": fake_moomoo}),
                  mock.patch.object(brokers.socket, "create_connection", lambda *a, **k: mock.Mock()),
                  mock.patch.object(paper_us, "today", lambda: self.today)):
            p.start()
            self.addCleanup(p.stop)

    def broker(self):
        b = paper_us.PaperUSBroker(self.cfg, self.tmp)
        self.addCleanup(b.close)
        return b

    def holding(self, price=20.0, qty=10):
        b = self.broker()
        b.buy("ACME", qty, price, False, {"reason": "test"})
        b.refresh(lambda t: price)
        return b


class PaperOnlyTest(PaperBase):
    def test_every_call_is_paper(self):
        b = self.holding()
        b.refresh(lambda t: 30.0)
        self.assertTrue(self.fake.envs)
        self.assertEqual(set(self.fake.envs), {"SIMULATE"})
        self.assertEqual(b.acc_id, 900)

    def test_refuses_without_a_paper_account(self):
        self.fake.accounts = [a for a in self.fake.accounts if a["trd_env"] == "REAL"]
        with self.assertRaises(BrokerError):
            paper_us.PaperUSBroker(self.cfg, self.tmp)

    def test_no_real_money_option_in_the_code(self):
        src = (ROOT / "agent" / "paper_us.py").read_text() + (ROOT / "research.py").read_text()
        self.assertNotIn("TrdEnv.REAL", src)
        self.assertNotIn('"REAL"', src.replace('trd_env"]) == PAPER', ""))
        self.assertNotIn("--live", (ROOT / "research.py").read_text())


class TradingRulesTest(PaperBase):
    def test_buy_within_200_and_one_at_a_time(self):
        b = self.broker()
        with self.assertRaises(BrokerError):
            b.buy("ACME", 11, 20.0, False, {})                 # $220: over $200
        b.buy("ACME", 10, 19.9, False, {})
        b.refresh(lambda t: 19.9)
        b.ledger["cash"] = 200.0                               # even with cash to spare...
        with self.assertRaises(BrokerError):
            b.buy("OTHER", 1, 10.0, False, {})                 # ...one stock at a time

    def test_never_borrows(self):
        self.fake.cash = 50.0
        with self.assertRaises(BrokerError):
            self.broker().buy("ACME", 9, 20.0, False, {})

    def test_exits_placed_right_after_the_fill(self):
        b = self.holding(price=20.0)
        pos = b.ledger["position"]
        sells = [o for o in self.fake.orders.values() if o["trd_side"] == "SELL"]
        stop = next(o for o in sells if o["order_type"] == "STOP")
        target = next(o for o in sells if o["order_type"] == "NORMAL")
        self.assertAlmostEqual(stop["aux_price"], 19.0)
        self.assertAlmostEqual(target["price"], 21.4)
        self.assertEqual({stop["time_in_force"], target["time_in_force"]}, {"GTC"})
        self.assertEqual((pos["sl_mode"], pos["tp_mode"]), ("moomoo", "moomoo"))

    def test_buy_that_fills_later_gets_its_exits_on_the_next_run(self):
        self.fake.fill_buys = False
        b = self.broker()
        oid = b.buy("ACME", 10, 19.9, False, {})
        self.assertEqual(b.refresh(lambda t: 19.9), [])                    # still waiting for 9:30
        self.assertFalse([o for o in self.fake.orders.values() if o["trd_side"] == "SELL"])
        self.fake.fill(oid, 19.8)                                          # filled at the open
        events = b.refresh(lambda t: 19.8)                                 # next "run the research"
        self.assertTrue(any(e.startswith("BOUGHT") for e in events))
        sells = {o["order_type"]: o for o in self.fake.orders.values() if o["trd_side"] == "SELL"}
        self.assertEqual(set(sells), {"STOP", "NORMAL"})
        self.assertEqual({o["time_in_force"] for o in sells.values()}, {"GTC"})
        self.assertAlmostEqual(sells["STOP"]["aux_price"], round(19.8 * 0.95, 2))
        self.assertAlmostEqual(sells["NORMAL"]["price"], round(19.8 * 1.07, 2))

    def test_an_exit_that_failed_is_retried_on_the_next_run(self):
        self.fake.refuse_stop = True
        b = self.holding(price=20.0)
        self.assertIsNone(b.ledger["position"]["sl_order"])
        self.fake.refuse_stop = False                                      # e.g. a temporary problem
        events = b.refresh(lambda t: 20.1)
        self.assertTrue(any("stop loss set" in e for e in events))
        self.assertEqual(b.ledger["position"]["sl_mode"], "moomoo")
        b.refresh(lambda t: 20.1)
        self.assertEqual(sum(o["order_type"] == "STOP" for o in self.fake.orders.values()), 1)   # not placed twice

    def test_target_hit_is_a_win_and_cancels_the_stop(self):
        b = self.holding(price=20.0)
        pos = b.ledger["position"]
        self.fake.fill(pos["tp_order"], 21.4)
        events = b.refresh(lambda t: 21.5)
        self.assertTrue(any(e.startswith("SOLD") for e in events))
        self.assertIsNone(b.ledger["position"])
        self.assertEqual(self.fake.orders[pos["sl_order"]]["order_status"], "CANCELLED_ALL")
        self.assertEqual((b.summary()["wins"], b.summary()["losses"]), (1, 0))
        self.assertAlmostEqual(b.ledger["cash"], 200 - 200 + 214, places=6)

    def test_stop_hit_is_a_loss(self):
        b = self.holding(price=20.0)
        self.fake.fill(b.ledger["position"]["sl_order"], 19.0)
        b.refresh(lambda t: 18.9)
        self.assertEqual((b.summary()["wins"], b.summary()["losses"]), (0, 1))
        self.assertAlmostEqual(b.summary()["profit"], -10.0)

    def test_stop_watched_when_paper_refuses_stop_orders(self):
        self.fake.refuse_stop = True
        b = self.holding(price=20.0)
        self.assertEqual(b.ledger["position"]["sl_mode"], "watch")
        events = b.refresh(lambda t: 18.9)                     # under the -5% level
        self.assertTrue(any("stop loss" in e for e in events))
        sell = b.ledger["pending"]
        self.assertEqual(sell["side"], "SELL")
        self.assertEqual(self.fake.orders[[o for o in self.fake.orders if self.fake.orders[o]["trd_side"] == "SELL"][-1]]["qty"], 10)

    def test_target_watched_when_two_sells_arent_allowed(self):
        self.fake.refuse_second_sell = True
        b = self.holding(price=20.0)
        pos = b.ledger["position"]
        self.assertEqual((pos["sl_mode"], pos["tp_mode"]), ("moomoo", "watch"))
        stop_id = pos["sl_order"]
        b.refresh(lambda t: 21.6)                              # over +7%
        self.assertEqual(self.fake.orders[stop_id]["order_status"], "CANCELLED_ALL")
        self.assertEqual(b.ledger["pending"]["side"], "SELL")

    def test_sells_after_10_trading_days(self):
        b = self.holding(price=20.0)
        self.today = dt.date(2026, 12, 8)                      # 10 weekdays after Nov 24
        events = b.refresh(lambda t: 20.3)
        self.assertTrue(any("10 trading days" in e for e in events))
        self.assertEqual(b.ledger["pending"]["side"], "SELL")

    def test_only_sells_what_it_bought(self):
        b = self.holding(price=20.0, qty=10)
        self.fake.pos["US.ACME"] = 0                           # shares gone from the account
        self.fake.refuse_stop = True
        b.ledger["position"]["sl_mode"] = "watch"
        with self.assertRaises(BrokerError):
            b.refresh(lambda t: 18.0)

    def test_unfilled_buy_refunds_the_cash(self):
        self.fake.fill_buys = False
        b = self.broker()
        b.buy("ACME", 10, 19.9, False, {})
        self.assertAlmostEqual(b.ledger["cash"], 1.0)
        oid = b.ledger["pending"]["order_id"]
        self.fake.orders[oid]["order_status"] = "CANCELLED_ALL"    # expired at the close
        b.refresh(lambda t: 19.9)
        self.assertAlmostEqual(b.ledger["cash"], 200.0)
        self.assertIsNone(b.ledger["pending"])

    def test_premarket_check(self):
        self.fake.fill_buys = False          # a buy at half the market price never fills
        b = self.broker()
        self.assertTrue(b.test_premarket("ACME", 20.0)[0])
        self.assertTrue(b.caps()["premarket_ok"])
        self.assertEqual(list(self.fake.orders.values())[-1]["order_status"], "CANCELLED_ALL")
        self.fake.refuse_outside = True
        self.assertFalse(b.test_premarket("ACME", 20.0)[0])
        self.assertFalse(b.caps()["premarket_ok"])


def series(n=300, start=50.0, drift=0.002, wiggle=0.01, vol=1e6, last_vol=None):
    import random
    rnd = random.Random(1)
    closes, highs, lows = [], [], []
    p = start
    for _ in range(n):
        p *= 1 + drift + rnd.uniform(-wiggle, wiggle)
        closes.append(p)
        highs.append(p * 1.02)
        lows.append(p * 0.98)
    vols = [vol] * n
    if last_vol:
        vols[-1] = last_vol
    days = [dt.date(2025, 1, 1) + dt.timedelta(days=i) for i in range(n)]
    return {"days": days, "highs": highs, "lows": lows, "closes": closes, "volumes": vols, "price": closes[-1]}


class SignalsTest(unittest.TestCase):
    def setUp(self):
        self.cfg = yaml.safe_load((ROOT / "config-research.yaml").read_text())

    def test_indicators(self):
        up = [100 * 1.01 ** i for i in range(100)]
        self.assertGreater(indicators.rsi(up), 95)
        self.assertLess(indicators.rsi(list(reversed(up))), 5)
        dip_then_rise = [100 - i for i in range(40)] + [60 + 2 * i for i in range(10)]
        self.assertTrue(indicators.macd(dip_then_rise)["above"])
        self.assertAlmostEqual(indicators.atr_pct([102] * 30, [98] * 30, [100] * 30), 0.04)
        flat_then_pop = [100 + (0.05 if i % 2 else -0.05) for i in range(150)] + [104]
        sq = indicators.squeeze_breakout(flat_then_pop)
        self.assertTrue(sq["squeeze"])
        self.assertTrue(sq["breakout"])
        highs = [100] * 50 + [130] + [100] * 30          # an old peak 30% above today
        self.assertAlmostEqual(indicators.resistance_room(highs, 100), 0.30)
        self.assertIsNone(indicators.resistance_room([90] * 100, 100))

    def test_market_falling_blocks_everything(self):
        stock, spy = series(drift=0.003), series(drift=-0.003)
        c = research.checklist("ACME", stock, spy, self.cfg)
        self.assertFalse(c["market_ok"])
        self.assertFalse(c["passes"])
        self.assertTrue(any("market" in b for b in c["blockers"]))

    def test_overbought_and_slow_stocks_are_blocked(self):
        hot = series(drift=0.02, wiggle=0.001)            # straight up: RSI far over 70
        spy = series(drift=0.001)
        self.assertTrue(any("RSI" in b for b in research.checklist("HOT", hot, spy, self.cfg)["blockers"]))
        slow = series(drift=0.001)
        slow["highs"] = [c * 1.004 for c in slow["closes"]]
        slow["lows"] = [c * 0.996 for c in slow["closes"]]
        self.assertTrue(any("too slow" in b for b in research.checklist("SLOW", slow, spy, self.cfg)["blockers"]))

    def test_analyst_upgrade_counts_as_a_signal(self):
        stock, spy = series(drift=0.003, last_vol=3e6), series(drift=0.001)
        a = research.checklist("ACME", stock, spy, self.cfg)
        b = research.checklist("ACME", stock, spy, self.cfg, analyst_upgrade=True)
        self.assertEqual(b["score"], a["score"] + 1)
        self.assertTrue(a["positives"]["volume"])

    def test_todays_partial_bar_is_ignored_while_the_market_is_open(self):
        h = series()
        h["days"][-1] = dt.date(2026, 11, 24)
        during = dt.datetime(2026, 11, 24, 11, 0, tzinfo=research.ET)
        after = dt.datetime(2026, 11, 24, 17, 0, tzinfo=research.ET)
        self.assertEqual(len(research.completed(h, during)["closes"]), len(h["closes"]) - 1)
        self.assertEqual(len(research.completed(h, after)["closes"]), len(h["closes"]))


class CommandTest(PaperBase):
    """research.py buy/skip/status, with the checklist and prices faked."""

    def setUp(self):
        super().setUp()
        self.passing = {"passes": True, "score": 7, "blockers": [], "positives": {k: True for k in research.POSITIVES},
                        "ticker": "ACME", "price": 20.0, "as_of": "2026-11-23"}
        self.sent = []
        for p in (mock.patch.object(cli, "load", lambda path: (self.cfg, self.tmp)),
                  mock.patch.object(research, "history", lambda t: {}),
                  mock.patch.object(research, "latest_price", lambda t: 20.0),
                  mock.patch.object(research, "explain", lambda c: "checklist"),
                  mock.patch.object(cli.time, "sleep", lambda s: None),
                  mock.patch.object(notify, "send", lambda cfg, title, msg, tags="": self.sent.append(msg) or True)):
            p.start()
            self.addCleanup(p.stop)

    def buy_args(self, **kw):
        base = dict(ticker="ACME", mcap=2e9, reason="New $50M contract; revenue up 30%.", analyst="", earnings="2026-12-03",
                    fda="", compared="BETA: weaker news", dry_run=False)
        return types.SimpleNamespace(**{**base, **kw})

    def test_buy_places_a_paper_order_logs_and_alerts(self):
        with mock.patch.object(research, "checklist", lambda *a, **k: self.passing):
            cli.cmd_buy(self.cfg, self.tmp, self.buy_args())
        buys = [o for o in self.fake.orders.values() if o["trd_side"] == "BUY"]
        self.assertEqual(len(buys), 1)
        self.assertEqual(buys[0]["qty"], math.floor(200 / round(20.0 * 1.005, 2)))
        self.assertLessEqual(buys[0]["qty"] * buys[0]["price"], 200)
        log = (self.tmp / "research-log.md").read_text()
        self.assertIn("revenue up 30%", log)
        self.assertIn("2026-12-03", log)
        self.assertTrue(any("Buy order" in m for m in self.sent))
        self.assertTrue(any(m.startswith("BOUGHT") for m in self.sent))

    def test_buy_refuses_small_companies_and_failed_checklists(self):
        with self.assertRaises(SystemExit):
            cli.cmd_buy(self.cfg, self.tmp, self.buy_args(mcap=2e8))
        failing = {**self.passing, "passes": False}
        with mock.patch.object(research, "checklist", lambda *a, **k: failing), self.assertRaises(SystemExit):
            cli.cmd_buy(self.cfg, self.tmp, self.buy_args())
        self.assertEqual(self.fake.orders, {})
        self.assertIn("did not pass", (self.tmp / "research-log.md").read_text())

    def test_buy_refuses_shares_over_200(self):
        with mock.patch.object(research, "checklist", lambda *a, **k: self.passing), \
                mock.patch.object(research, "latest_price", lambda t: 250.0), self.assertRaises(SystemExit):
            cli.cmd_buy(self.cfg, self.tmp, self.buy_args())
        self.assertEqual(self.fake.orders, {})

    def test_dry_run_places_nothing(self):
        with mock.patch.object(research, "checklist", lambda *a, **k: self.passing):
            cli.cmd_buy(self.cfg, self.tmp, self.buy_args(dry_run=True))
        self.assertEqual(self.fake.orders, {})

    def test_premarket_flag_only_after_a_successful_test(self):
        pre = lambda now=None: "premarket"  # noqa: E731
        with mock.patch.object(research, "checklist", lambda *a, **k: self.passing), mock.patch.object(cli, "market_time", pre):
            cli.cmd_buy(self.cfg, self.tmp, self.buy_args())
        self.assertFalse(list(self.fake.orders.values())[0]["fill_outside_rth"])   # not tested yet: waits for 9:30

    def test_skip_and_status_and_score(self):
        cli.cmd_skip(self.cfg, self.tmp, types.SimpleNamespace(reason="Market falling.", compared=""))
        self.assertIn("No buy today", (self.tmp / "research-log.md").read_text())
        b = self.holding(price=20.0)
        self.fake.fill(b.ledger["position"]["tp_order"], 21.4)
        b.close()
        out = []
        with mock.patch("builtins.print", lambda *a, **k: out.append(" ".join(str(x) for x in a))):
            cli.cmd_status(self.cfg, self.tmp, types.SimpleNamespace())
        text = "\n".join(out)
        self.assertIn("1 won, 0 lost", text)
        self.assertIn("NEXT: RESEARCH", text)
        self.assertTrue(any(m.startswith("SOLD") for m in self.sent))


class NtfyTest(unittest.TestCase):
    def test_sends_to_the_topic_and_never_crashes(self):
        cfg = {"ntfy": {"server": "https://ntfy.sh", "topic": "secret-topic"}}
        with mock.patch.object(notify.requests, "post") as post:
            self.assertTrue(notify.send(cfg, "Title", "Bought 5 ACME"))
            self.assertEqual(post.call_args.args[0], "https://ntfy.sh/secret-topic")
        with mock.patch.object(notify.requests, "post", side_effect=notify.requests.ConnectionError("down")):
            self.assertFalse(notify.send(cfg, "Title", "x"))
        with mock.patch.object(notify, "TOPIC_FILE", Path(tempfile.mkdtemp()) / "none.txt"), \
                mock.patch.dict("os.environ", {"NTFY_TOPIC": ""}):
            self.assertFalse(notify.send({"ntfy": {"topic": ""}}, "Title", "x"))

    def test_secret_topic_from_a_private_file(self):
        f = Path(tempfile.mkdtemp()) / "ntfy-topic.txt"
        f.write_text("my-secret-channel\n")
        with mock.patch.object(notify, "TOPIC_FILE", f), mock.patch.dict("os.environ", {"NTFY_TOPIC": ""}), \
                mock.patch.object(notify.requests, "post") as post:
            self.assertTrue(notify.send({"ntfy": {"server": "https://ntfy.sh", "topic": ""}}, "T", "m"))
            self.assertEqual(post.call_args.args[0], "https://ntfy.sh/my-secret-channel")

    def test_the_secret_topic_is_never_in_the_project_files(self):
        self.assertEqual(yaml.safe_load((ROOT / "config-research.yaml").read_text())["ntfy"]["topic"], "")
        self.assertIn("ntfy-topic.txt", (ROOT / ".gitignore").read_text())


if __name__ == "__main__":
    unittest.main()
