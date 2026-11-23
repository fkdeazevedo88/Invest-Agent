"""Tests for the high-risk momentum agent: its filters, its limits, and that it
stays out of the ETF agent's shares and the owner's own stocks.

    python -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_live_safety as fake  # noqa: E402  (fake moomoo account)
from agent import brokers, data, strategy  # noqa: E402
import run  # noqa: E402


def sig(r1=0.05, r3=0.20, r6=0.30, above50=True, above200=True, dd20=-0.03):
    return {
        "ret_1m": r1, "ret_3m": r3, "ret_6m": r6, "ret_12m": 0.4,
        "above_50d_avg": above50, "above_200d_avg": above200, "vol_60d_annual": 0.5,
        "drawdown_from_20d_high": dd20, "drawdown_from_1y_high": dd20,
    }


def stock(price=20.0, volume=1e7, **s):
    return {"price": price, "dollar_volume_20d": volume, "signals": sig(**s)}


class MomentumRulesTest(unittest.TestCase):
    def setUp(self):
        self.cfg = run.load_config(ROOT / "config-highrisk.yaml")

    def test_filters(self):
        snap = {
            "GOOD.TO": stock(r3=0.30),
            "PENNY.TO": stock(price=1.5, r3=0.90),           # under $2
            "THIN.TO": stock(volume=100_000, r3=0.90),       # barely trades
            "DOWN.TO": stock(above50=False, r3=0.90),        # lost its trend
            "LONGDOWN.TO": stock(r6=-0.10, r3=0.90),         # down over 6 months
            "STOPPED.TO": stock(dd20=-0.20, r3=0.90),        # fell 20% from its 20-day high
        }
        self.assertEqual(strategy.momentum_candidates(snap, self.cfg), ["GOOD.TO"])

    def test_picks_the_strongest_four_equal_weight(self):
        snap = {f"S{i}.TO": stock(r3=i / 100) for i in range(1, 8)}
        w = strategy.momentum_baseline(snap, self.cfg)
        self.assertEqual(set(w), {"S7.TO", "S6.TO", "S5.TO", "S4.TO"})
        self.assertTrue(all(abs(x - 0.25) < 1e-9 for x in w.values()))

    def test_stays_in_cash_when_nothing_qualifies(self):
        snap = {"A.TO": stock(above200=False), "B.TO": stock(dd20=-0.3)}
        self.assertEqual(strategy.momentum_baseline(snap, self.cfg), {})

    def test_guardrails_on_claudes_answer(self):
        allowed = {"A.TO", "B.TO", "C.TO", "D.TO", "E.TO"}
        proposed = {"A.TO": 0.9, "B.TO": 0.2, "C.TO": 0.2, "D.TO": 0.2, "E.TO": 0.1, "ENB.TO": 0.5, "X.TO": float("nan")}
        out = strategy.sanitize(proposed, self.cfg, allowed)
        self.assertNotIn("ENB.TO", out)                      # not a candidate
        self.assertLessEqual(len(out), 4)                    # max positions
        self.assertLessEqual(max(out.values()), 0.30 + 1e-9) # per-stock cap
        self.assertLessEqual(sum(out.values()), 1 + 1e-9)

    def test_config_keeps_out_owners_stocks_and_etf_agents_funds(self):
        etf = {u["ticker"] for u in run.load_config(ROOT / "config.yaml")["universe"]}
        hr = {u["ticker"] for u in self.cfg["universe"]}
        self.assertFalse(hr & etf)
        self.assertFalse(hr & {"ENB.TO", "BN.TO", "BAM.TO"})
        self.assertNotEqual(self.cfg["state_dir"], "state")
        for u in self.cfg["universe"]:
            if u["ticker"].endswith((".TO", ".NE")):
                self.assertEqual(brokers.moomoo_code(u), "CA." + u["ticker"][:-3])
                self.assertEqual(brokers.currency_of(u), "CAD")
            else:
                self.assertEqual(u.get("market"), "US")
                self.assertEqual(brokers.moomoo_code(u), "US." + u["ticker"])
                self.assertEqual(brokers.currency_of(u), "USD")
        self.assertIn("ON", hr)                              # not read as a yes/no value


class HighRiskBase(unittest.TestCase):
    """Both agents trading the same fake margin account."""

    def setUp(self):
        self.etf_dir, self.hr_dir = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
        self.etf_cfg = run.load_config(ROOT / "config.yaml")
        self.hr_cfg = run.load_config(ROOT / "config-highrisk.yaml")
        for c in (self.etf_cfg, self.hr_cfg):
            c["claude"]["enabled"] = False
            c["moomoo"]["fill_wait_seconds"] = 0
            c["moomoo"]["account_type"] = "MARGIN"
        self.hr_cfg["capital_cad"], self.hr_cfg["capital_usd"] = 500, 0   # CAD-only unless a test says otherwise
        self.hr_cfg["strategy"] = "momentum"                               # these tests exercise momentum's picks
        self.snap = {t: {"price": p, "dollar_volume_20d": 5e7, "signals": sig()} for t, p in fake.PRICES.items()}
        for i, t in enumerate(["SHOP.TO", "CCO.TO", "AEM.TO", "IVN.TO", "CVE.TO"]):
            self.snap[t] = stock(price=50.0, r3=0.5 - i / 20)
        patches = [
            mock.patch.dict(sys.modules, {"moomoo": fake.fake_moomoo}),
            mock.patch.dict(os.environ, {"MOOMOO_TRADE_PASSWORD": "000000"}),
            mock.patch.object(run, "market_open", lambda: True),
            mock.patch.object(run.data, "snapshot", lambda tickers, **kw: {t: dict(self.snap[t]) for t in tickers if t in self.snap}),
            mock.patch.object(run.data, "fx_rates", lambda currencies: {"CAD": 1.0, "USD": 1.4}),
            mock.patch.object(brokers, "today", lambda: "2026-11-23"),
            mock.patch.object(brokers.socket, "create_connection", lambda *a, **k: mock.Mock()),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        fake.ACCOUNT = self.acct = fake.FakeAccount(5000.0, {"CA.XIU": 10, "CA.ENB": 50})

    def run_agent(self, cfg, state, etf_target=None):
        args = types.SimpleNamespace(live=True, dry_run=False)
        with mock.patch.object(run, "STATE", state), mock.patch.object(run, "JOURNAL", state / "j"), \
                mock.patch.object(run, "HALT", state / "HALT"):
            if etf_target is None:
                run.cycle(args, cfg)
            else:
                with mock.patch.object(strategy, "baseline", lambda snap, c, **kw: etf_target):
                    run.cycle(args, cfg)



class HighRiskLiveTest(HighRiskBase):
    def test_buys_the_top_momentum_stocks_within_its_own_money(self):
        self.run_agent(self.hr_cfg, self.hr_dir)
        bought = {c for c, q in self.acct.pos.items() if c not in ("CA.XIU", "CA.ENB") and q}
        self.assertEqual(bought, {"CA.SHOP", "CA.CCO", "CA.AEM", "CA.IVN"})
        self.assertGreaterEqual(self.acct.cash, 5000.0 - 500.0)   # spent at most its $500

    def test_two_agents_never_touch_each_other_or_the_owner(self):
        self.run_agent(self.etf_cfg, self.etf_dir, etf_target={"XIU.TO": 0.4, "VFV.TO": 0.3})
        self.run_agent(self.hr_cfg, self.hr_dir)
        xiu_after_etf = self.acct.pos["CA.XIU"]
        for t in ("SHOP.TO", "CCO.TO", "AEM.TO", "IVN.TO", "CVE.TO"):
            self.snap[t] = stock(price=50.0, dd20=-0.25)          # every stock hits its stop-loss
        self.run_agent(self.hr_cfg, self.hr_dir)                   # high-risk agent sells out
        self.assertEqual(self.acct.pos["CA.XIU"], xiu_after_etf)   # ETF agent's and owner's XIU untouched
        self.assertEqual(self.acct.pos["CA.ENB"], 50)
        for code in ("CA.SHOP", "CA.CCO", "CA.AEM", "CA.IVN"):
            self.assertEqual(self.acct.pos[code], 0)
        self.run_agent(self.etf_cfg, self.etf_dir, etf_target={})  # ETF agent sells out too
        self.assertEqual(self.acct.pos["CA.XIU"], 10)              # owner's 10 XIU remain
        self.assertEqual(self.acct.pos["CA.ENB"], 50)
        self.assertEqual(self.acct.order_accounts, {111})          # margin account only

    def test_claude_failure_falls_back_to_rules(self):
        self.hr_cfg["claude"]["enabled"] = True
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "x"}), \
                mock.patch.object(strategy, "research_news", side_effect=TypeError("boom")):
            self.run_agent(self.hr_cfg, self.hr_dir)
        self.assertEqual(self.acct.pos.get("CA.SHOP", 0) > 0, True)

    def test_claude_cannot_pick_outside_the_candidates(self):
        self.hr_cfg["claude"]["enabled"] = True
        self.hr_cfg["claude"]["web_search"] = False
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "x"}), \
                mock.patch.object(strategy, "claude_weights", return_value=({"ENB.TO": 0.9, "XIU.TO": 0.5, "SHOP.TO": 0.3}, "test")):
            self.run_agent(self.hr_cfg, self.hr_dir)
        self.assertEqual(self.acct.pos["CA.ENB"], 50)
        self.assertEqual(self.acct.pos["CA.XIU"], 10)
        self.assertGreater(self.acct.pos.get("CA.SHOP", 0), 0)

    def test_no_price_for_a_held_stock_means_no_trading(self):
        self.run_agent(self.hr_cfg, self.hr_dir)
        orders = len(self.acct.today)
        del self.snap["SHOP.TO"]
        with self.assertRaises(data.DataError):
            self.run_agent(self.hr_cfg, self.hr_dir)
        self.assertEqual(len(self.acct.today), orders)


class CurrencyAndNewsTest(HighRiskBase):
    """NASDAQ stocks in US dollars, frozen positions, and the news research request."""

    def us_setup(self, real_usd):
        self.hr_cfg["capital_cad"], self.hr_cfg["capital_usd"] = 300, 150
        self.acct.usd = real_usd
        for t in ("SHOP.TO", "CCO.TO", "AEM.TO", "IVN.TO", "CVE.TO"):
            del self.snap[t]
        self.snap["NVDA"] = stock(price=40.0, r3=0.9)        # strongest momentum, US$40 a share
        self.snap["CCO.TO"] = stock(price=50.0, r3=0.5)

    def test_nasdaq_stock_bought_with_real_us_dollars(self):
        self.us_setup(real_usd=1000.0)
        self.run_agent(self.hr_cfg, self.hr_dir)
        self.assertGreater(self.acct.pos.get("US.NVDA", 0), 0)
        self.assertGreaterEqual(self.acct.usd, 1000.0 - 150.0)   # spent at most its US$150
        self.assertEqual(self.acct.order_accounts, {111})

    def test_never_borrows_or_converts_for_a_nasdaq_stock(self):
        self.us_setup(real_usd=0.0)                          # no US dollars in the account
        self.run_agent(self.hr_cfg, self.hr_dir)             # the fake fails on any borrowing
        self.assertEqual(self.acct.pos.get("US.NVDA", 0), 0)
        self.assertGreater(self.acct.pos.get("CA.CCO", 0), 0)   # CAD stocks still bought with CAD

    def test_equity_counts_us_dollars_in_cad(self):
        self.us_setup(real_usd=1000.0)
        self.run_agent(self.hr_cfg, self.hr_dir)
        b = brokers.MoomooBroker(self.hr_cfg, self.hr_dir)
        prices = {"NVDA": 40.0, "CCO.TO": 50.0}
        a = run.account(b, prices, {"CAD": 1.0, "USD": 1.4}, self.hr_cfg)
        expected = (a.cash["CAD"] + a.cash["USD"] * 1.4 + a.pos.get("NVDA", 0) * 40 * 1.4 + a.pos.get("CCO.TO", 0) * 50)
        self.assertAlmostEqual(a.equity, expected, places=6)
        self.assertAlmostEqual(run.start_capital(b, {"CAD": 1.0, "USD": 1.4}), 300 + 150 * 1.4)

    def test_frozen_when_moomoo_shows_fewer_shares_than_the_records(self):
        self.run_agent(self.etf_cfg, self.etf_dir, etf_target={"XIU.TO": 0.4})
        agent_xiu = self.acct.pos["CA.XIU"] - 10
        self.acct.pos["CA.XIU"] = agent_xiu - 1              # owner sold 11 by hand
        sold_before = list(self.acct.sells)
        self.run_agent(self.etf_cfg, self.etf_dir, etf_target={})   # wants out of XIU
        self.assertEqual(self.acct.sells, sold_before)       # frozen: not traded

    def test_symbol_check_places_no_orders(self):
        b = brokers.MoomooBroker(self.hr_cfg, self.hr_dir)
        self.assertEqual(b.check_symbol("NVDA.NE", 36.6), (True, "OK"))
        b.to_mm["FAKE.TO"] = "CA.BOGUS"
        ok, msg = b.check_symbol("FAKE.TO", 10.0)
        self.assertFalse(ok)
        self.assertIn("unknown", msg)
        self.assertEqual(self.acct.today, [])

    def test_old_single_currency_records_still_load(self):
        (self.hr_dir / "paper_account.json").write_text('{"cash": 250.0, "positions": {"CCO.TO": 2}}')
        self.hr_cfg["capital_usd"] = 150
        b = brokers.PaperBroker(self.hr_cfg, self.hr_dir)
        self.assertEqual(b.own_cash(), {"CAD": 250.0, "USD": 150.0})   # US dollars are new money
        self.assertEqual(b.start(), {"CAD": 500.0, "USD": 150.0})      # started with $500 CAD
        self.assertEqual(b.positions(), {"CCO.TO": 2})

    def test_changing_capital_later_does_not_change_the_starting_money(self):
        self.run_agent(self.hr_cfg, self.hr_dir)
        self.hr_cfg["capital_cad"] = 50                      # edited after the agent started
        b = brokers.MoomooBroker(self.hr_cfg, self.hr_dir)
        self.assertEqual(b.start(), {"CAD": 500.0, "USD": 0.0})
        self.assertAlmostEqual(run.start_capital(b, {"CAD": 1.0, "USD": 1.4}), 500.0)
        self.assertFalse((self.hr_dir / "HALT").exists())

    def test_news_research_covers_world_news_from_trusted_sources_only(self):
        calls = []

        class FakeMessages:
            def create(self, **kw):
                calls.append(kw)
                return types.SimpleNamespace(stop_reason="end_turn", content=[types.SimpleNamespace(type="text", text="ok")])

        fake_client = types.SimpleNamespace(messages=FakeMessages())
        with mock.patch("anthropic.Anthropic", return_value=fake_client):
            self.assertEqual(strategy.research_news(["SHOP.TO", "NVDA"], self.hr_cfg), "ok")
        prompt = calls[0]["messages"][0]["content"]
        for words in ("tariffs", "Bank of Canada", "Federal Reserve", "tensions between countries",
                      "SHOP (TSX)", "NVDA (NASDAQ)", "source name, the date"):
            self.assertIn(words, prompt)
        tool = calls[0]["tools"][0]
        self.assertEqual(tool["type"], "web_search_20260209")
        for site in ("reuters.com", "bankofcanada.ca", "federalreserve.gov", "sedarplus.ca", "sec.gov"):
            self.assertIn(site, tool["allowed_domains"])
        self.assertFalse(any(s in tool["allowed_domains"] for s in ("reddit.com", "x.com", "twitter.com")))


if __name__ == "__main__":
    unittest.main()
