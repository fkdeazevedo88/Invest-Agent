"""Tests for the high-risk strategies (breakout, pullback, sector rotation,
leveraged trend, combo) and the backtester.

    python -m unittest discover -s tests -v
"""
import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import backtest, data, strategy  # noqa: E402
import run  # noqa: E402


def sig(r1=0.05, r3=0.20, r6=0.30, above50=True, above200=True, dd20=-0.03, dd1y=-0.05, rsi=55):
    return {"ret_5d": 0.0, "ret_1m": r1, "ret_3m": r3, "ret_6m": r6, "ret_12m": 0.4,
            "above_50d_avg": above50, "above_200d_avg": above200, "vol_60d_annual": 0.5,
            "drawdown_from_20d_high": dd20, "drawdown_from_1y_high": dd1y, "rsi_14": rsi}


def entry(price=20.0, volume=1e7, sector=None, kind="stock", **s):
    return {"price": price, "dollar_volume_20d": volume, "sector": sector, "kind": kind, "signals": sig(**s)}


class StrategyRulesTest(unittest.TestCase):
    def setUp(self):
        self.cfg = run.load_config(ROOT / "config-highrisk.yaml")

    def with_strategy(self, name):
        return {**self.cfg, "strategy": name}

    def test_breakout_buys_near_highs_and_holds_within_the_band(self):
        snap = {"NEAR.TO": entry(dd1y=-0.01), "FAR.TO": entry(dd1y=-0.08), "WEAK.TO": entry(dd1y=-0.01, r3=-0.05)}
        cfg = self.with_strategy("breakout")
        self.assertEqual(strategy.candidates(snap, cfg), ["NEAR.TO"])
        self.assertIn("FAR.TO", strategy.candidates(snap, cfg, held={"FAR.TO"}))   # held: 12% band
        snap["FAR.TO"] = entry(dd1y=-0.20)
        self.assertNotIn("FAR.TO", strategy.candidates(snap, cfg, held={"FAR.TO"}))

    def test_pullback_buys_dips_in_strong_stocks_and_sells_on_recovery(self):
        cfg = self.with_strategy("pullback")
        snap = {
            "DIP.TO": entry(r6=0.40, dd20=-0.12, rsi=30),
            "NODIP.TO": entry(r6=0.40, dd20=-0.02, rsi=60),         # not dipped
            "NOTOVERSOLD.TO": entry(r6=0.40, dd20=-0.12, rsi=55),   # RSI too high
            "WEAK.TO": entry(r6=0.02, dd20=-0.12, rsi=30),          # not a strong stock
            "CRASH.TO": entry(r6=0.40, dd20=-0.35, rsi=15),         # falling knife
            "BROKEN.TO": entry(r6=0.40, dd20=-0.12, rsi=30, above200=False),
        }
        self.assertEqual(strategy.candidates(snap, cfg), ["DIP.TO"])
        snap["DIP.TO"] = entry(r6=0.40, dd20=-0.05, rsi=50)          # bouncing back, still held
        self.assertEqual(strategy.candidates(snap, cfg, held={"DIP.TO"}), ["DIP.TO"])
        snap["DIP.TO"] = entry(r6=0.40, dd20=-0.01, rsi=65)          # recovered: sell
        self.assertEqual(strategy.candidates(snap, cfg, held={"DIP.TO"}), [])

    def test_sector_rotation_picks_the_hottest_sectors(self):
        cfg = self.with_strategy("sector_rotation")
        snap = {}
        for i, (sec, r3) in enumerate([("gold", 0.5)] * 3 + [("chips", 0.3)] * 3 + [("oil", -0.2)] * 3):
            snap[f"S{i}.TO"] = entry(sector=sec, r3=r3 + i / 1000, r6=0.2 if r3 > 0 else -0.1,
                                     above200=r3 > 0, above50=r3 > 0)
        picks = strategy.candidates(snap, cfg)
        sectors = [snap[t]["sector"] for t in picks]
        self.assertEqual(sectors, ["gold", "gold", "chips", "chips"])
        w = strategy.baseline(snap, cfg)
        self.assertEqual(len(w), 4)

    def test_leveraged_etfs_only_in_the_leveraged_strategy(self):
        snap = {"QQU.TO": entry(kind="leveraged_etf", r3=0.9), "STOCK.TO": entry(r3=0.2)}
        self.assertEqual(strategy.candidates(snap, self.with_strategy("leveraged_trend")), ["QQU.TO"])
        for name in ("momentum", "breakout", "sector_rotation"):
            self.assertNotIn("QQU.TO", strategy.candidates(snap, self.with_strategy(name)), name)
        snap["QQU.TO"] = entry(kind="leveraged_etf", r3=0.9, above200=False)   # trend broken: cash
        self.assertEqual(strategy.baseline(snap, self.with_strategy("leveraged_trend")), {})

    def test_combo_splits_the_money_across_strategies(self):
        cfg = {**self.with_strategy("combo"), "combo": {"parts": ["momentum", "pullback"]}}
        snap = {"MOM.TO": entry(r3=0.5), "DIP.TO": entry(r6=0.40, dd20=-0.12, rsi=30, above50=False)}
        w = strategy.baseline(snap, cfg)
        self.assertAlmostEqual(w["MOM.TO"], 0.125)    # 25% slot x half the money
        self.assertAlmostEqual(w["DIP.TO"], 0.125)
        self.assertEqual(strategy.max_positions(cfg), 8)
        self.assertLessEqual(sum(strategy.sanitize(w, cfg, set(snap)).values()), 1 + 1e-9)

    def test_every_strategy_respects_the_guardrails(self):
        snap = {f"S{i}.TO": entry(r3=0.1 + i / 100, sector=["gold", "chips"][i % 2], dd1y=-0.01) for i in range(12)}
        snap.update({"DIP.TO": entry(r6=0.4, dd20=-0.1, rsi=30, above50=False), "QQU.TO": entry(kind="leveraged_etf")})
        for name in backtest.ALL:
            cfg = self.with_strategy(name)
            w = strategy.sanitize(strategy.baseline(snap, cfg), cfg, set(snap))
            self.assertLessEqual(len(w), strategy.max_positions(cfg), name)
            self.assertLessEqual(max(w.values(), default=0), cfg["risk"]["max_position_weight"] + 1e-9, name)
            self.assertLessEqual(sum(w.values()), 1 + 1e-9, name)

    def test_config_strategies_and_sectors(self):
        for u in self.cfg["universe"]:
            self.assertTrue(u.get("sector") or u.get("kind") == "leveraged_etf", u["ticker"])
        self.assertEqual(set(backtest.ALL), set(strategy.STRATEGIES))


class SignalsTest(unittest.TestCase):
    def test_rsi(self):
        up = [100 * 1.01 ** i for i in range(300)]
        down = [100 * 0.99 ** i for i in range(300)]
        self.assertEqual(data.signals(up)["rsi_14"], 100.0)
        self.assertLess(data.signals(down)["rsi_14"], 1)
        self.assertAlmostEqual(data.signals(up)["ret_5d"], 1.01 ** 5 - 1)


class BacktestTest(unittest.TestCase):
    """The backtester on made-up prices: no network."""

    def setUp(self):
        self.cfg = {**run.load_config(ROOT / "config-highrisk.yaml"), "strategy": "momentum"}
        n = backtest.WARMUP + 200
        self.dates = [f"d{i:04d}" for i in range(n)]
        rise = 10 * 1.004 ** np.arange(n)
        fall = 10 * 0.997 ** np.arange(n)
        flat = np.full(n, 10.0)
        dv = np.full(n, 1e8)
        self.info = {
            "UP.TO": {"adj": rise, "dv": dv, "currency": "CAD", "sector": "a", "kind": "stock"},
            "DOWN.TO": {"adj": fall, "dv": dv, "currency": "CAD", "sector": "b", "kind": "stock"},
            "__XIU.TO": {"adj": flat, "dv": dv, "currency": "CAD"},
            "__QQQ": {"adj": flat, "dv": dv, "currency": "USD"},
        }
        self.fx = np.full(n, 1.4)

    def test_buys_the_riser_not_the_faller(self):
        m = backtest.simulate(self.cfg, self.dates, self.info, self.fx)
        weeks = (len(self.dates) - backtest.WARMUP - 1) // backtest.STEP
        expected = 1.004 ** (backtest.STEP * weeks)   # 25% of the money in UP, the rest in cash
        self.assertGreater(m["total"], 0)
        self.assertLess(m["total"], expected - 1)
        self.assertAlmostEqual(m["invested"], 0.25)

    def test_no_peeking_at_the_future(self):
        # JUMP drifts down (never a candidate), then leaps 50% in the middle of a trading week.
        # Deciding only on past prices, the strategy can't own it before the leap.
        n = len(self.dates)
        jump = 10 * 0.999 ** np.arange(n)
        leap = backtest.WARMUP + 10 * backtest.STEP + 2
        jump[leap:] *= 1.5
        self.info["JUMP.TO"] = {"adj": jump, "dv": np.full(n, 1e8), "currency": "CAD", "sector": "c", "kind": "stock"}
        m = backtest.simulate(self.cfg, self.dates, self.info, self.fx)
        self.assertLess(m["best_week"], 0.05)     # peeking would show a ~+12% week (25% x 50%)

    def test_buy_and_hold_and_halves(self):
        flat = backtest.buy_and_hold(self.info, "XIU.TO", self.dates, self.fx)
        self.assertAlmostEqual(flat["total"], 0.0)
        mid = backtest.WARMUP + 100
        a = backtest.simulate(self.cfg, self.dates, self.info, self.fx, end=mid)
        b = backtest.simulate(self.cfg, self.dates, self.info, self.fx, start=mid)
        self.assertGreater(a["total"], 0)
        self.assertGreater(b["total"], 0)


if __name__ == "__main__":
    unittest.main()
