# Investing agent ($1,000 CAD, moomoo)

An automated agent that manages a small portfolio of Canadian ETFs. Every 4 weeks it reads market data,
decides target weights, and places whole-share limit orders (`rebalance_every_days: 28`; running it
more often only checks the safety rules, unless you add `--force`). By default it trades a **paper**
(simulated) account. Real money only moves with `--live`.

## How it decides

1. **Kill switch (always runs first, plain code).** If the account falls below 75% of the starting
   $1,000, it sells everything and refuses to run again until you delete `state/HALT`.
2. **Rules baseline.** It buys ETFs that are above their 200-day average and have positive 3/6/12-month
   momentum, weighted by inverse volatility. The rest goes to `CASH.TO` (a high-interest savings ETF).
   It keeps an ETF it owns until it falls 5% below its 200-day average (`trend_buffer`), so small dips
   don't trigger a sale. In the Nov 2026 backtest this traded 3x less (about 3 times a year instead of
   9) and did slightly better (+69% vs +67%), with a slightly bigger worst drop (-8% vs -7%).
3. **Claude (optional).** If `ANTHROPIC_API_KEY` is set, Claude sees the signals, your holdings, the
   baseline and its own last 3 decisions, then proposes weights with a written rationale. If Claude
   fails, the agent uses the baseline.
4. **Guardrails (code, not AI).** Only the 11 ETFs in `config.yaml` can be bought. No single ETF can go
   above 40% (cash excepted). No leverage, no shorting, no options. Small trades are skipped, and the
   agent never manages more than `max_equity_cad`, even if the account holds more.

Every run is logged to `state/journal.jsonl`.

### Which ETF list and strategy? (backtest, Nov 23, 2026)

In Nov 2026 three higher-risk ETFs were added: XQQ (Nasdaq-100), XIT (Canadian tech) and XGD (gold
miners). The agent can also use the `pullback` strategy, with dips sized for ETFs (3-10%, about half
the size used for single stocks, because ETFs swing about half as much). Compare them yourself with
`python run.py backtest --strategy all`.

| Weekly, Dec 2022 - Nov 2026, $1,000 start | Total | Worst drop | 1st half | 2nd half | Became |
|---|---|---|---|---|---|
| A. Old list (8 ETFs), trend | +57% | -6% | +26% | +15% | $1,569 |
| **B. New list (11 ETFs), trend (in use)** | **+67%** | **-7%** | +31% | +19% | **$1,667** |
| C. New list, pullback | +20% | -8% | +19% | +7% | $1,199 |
| Just hold XIU | +94% | -10% | +32% | +42% | $1,943 |

B did best of the three. Pullback barely invested (about 17% of the time) because ETFs rarely dip
3-10% while staying strong, so its money mostly waited in the cash ETF; other dip sizes gave about
the same result. None of the three beat simply holding XIU: the trend strategy is smoother (worst
drop -7% vs -10%) but earns less, partly because it often keeps up to 40% in bonds (ZAG). The new
ETFs were picked in 2026, after strong runs, so B's edge over A is slightly flattered.

## Setup

```bash
cd invest-agent
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Optional: turn on Claude (you need an API key from console.anthropic.com; it costs a few cents per run):

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

## Step 1: paper trade for a few weeks

```bash
.venv/bin/python run.py run --dry-run
```

```bash
.venv/bin/python run.py run
```

```bash
.venv/bin/python run.py status
```

## Step 2: going live on moomoo

1. Open a **Moomoo Financial Canada** account and fund it. (A TFSA would make gains tax-free; this
   setup uses the margin account, where gains are taxable.)
2. Download **OpenD** (moomoo's API gateway) from moomoo's OpenAPI page, run it on the computer
   that will run the agent, and log in. It listens on `127.0.0.1:11111`.
3. Enable API trading in the moomoo app if it asks you to, and set your 6-digit trade password.
4. Check which account the agent will trade. It uses your **margin** account (`moomoo.account_type`
   in `config.yaml`) and refuses to start if it can't tell which account that is: it never guesses or
   falls back to "the first account". This lists your accounts and shows its choice (no trading):

```bash
.venv/bin/python run.py accounts
```

   To use a different account, change `account_type` (for example `TFSA`). Only if you have two
   accounts of the same type, also set `acc_id` to the one you want. The agent's records belong to
   one account, so it refuses to run if you switch accounts later.
5. Run a live **dry run** first (reads your real account, places nothing):

```bash
export MOOMOO_TRADE_PASSWORD=123456
```

```bash
.venv/bin/python run.py run --live --dry-run
```

6. If everything looks right, trade for real (only works during TSX hours, 9:35 to 15:55 Toronto time):

```bash
.venv/bin/python run.py run --live
```

**moomoo has no paper trading for Canadian stocks**, so `--live` is always real money.

### Safety rule: `--live` never sells anything the agent didn't buy

The agent can share an account with your own investments. It keeps its own record
(`state/live_ledger.json`) of the cash it was given (`capital_cad`) and the shares it bought, and
right before every order it checks:

- **Only its own ETFs.** It refuses to trade anything not listed in `config.yaml`, whatever its
  ledger says.
- **Only its own shares.** A sell is capped at the smaller of: the shares its ledger says it bought,
  and the shares moomoo says are really in the account. With no ledger, it owns nothing and sells
  nothing. If you hold the same ETF yourself, your shares are left alone.
- **Only its own cash, never borrowed.** A buy is capped at the ledger's cash (starting at
  `capital_cad`) and at the CAD cash really in the account, so in a margin account it never takes a
  margin loan. Sells are capped at shares really held, so it never short sells.
- **Written down before sending.** Each order goes into the ledger, with a unique tag, before it is
  sent to moomoo. If the program or connection dies mid-order, the next run finds the order in
  moomoo's history by that tag, so it can never forget a sale and sell again.
- **One run at a time.** A lock file (`state/run.lock`) stops a scheduled run and a manual run from
  overlapping. If a run crashes, the lock clears itself after an hour.
- **Waits for open orders.** While an earlier order is unfinished, it doesn't trade.

Don't delete or edit `state/live_ledger.json`, and don't move the agent's folder. If the ledger is
lost, the agent forgets what it bought: it will then sell nothing (safe), but it will treat its
leftover shares as yours.

These rules are covered by automatic tests against a simulated account:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

## Running it automatically every week

On the computer running OpenD, add a cron job (`crontab -e`). This runs Wednesdays at 10:30 Toronto time:

```
CRON_TZ=America/Toronto
30 10 * * 3 cd /path/to/invest-agent && MOOMOO_TRADE_PASSWORD=... ANTHROPIC_API_KEY=... .venv/bin/python run.py run --live >> state/cron.log 2>&1
```

## Things to know

- This is a diversified, trend-following ETF strategy. It aims for steady growth with smaller crashes.
  It will not double your money fast, and nothing can guarantee a profit. You can lose money.
- With $1,000, only whole shares are bought, so some cash stays unused (about 15% at today's prices).
- Orders are limit orders 0.3% from the last price. On a fast-moving day some may not fill. While an
  order is still waiting, the agent won't trade again; unfilled orders expire at market close and the
  next run picks up whatever is left.
- To stop everything: don't run it, or create a file named `state/HALT`.

## High-risk agent (optional)

A second, separate agent for a small amount of money you can afford to lose. It holds a few
individual TSX or NASDAQ stocks (tech, AI chips, uranium, gold, copper, oil; the list, with each
stock's sector, is in `config-highrisk.yaml`), or leveraged NASDAQ-100 ETFs, picked by one of six
strategies (`strategy:` in the config):

| Strategy | Idea |
|---|---|
| `momentum` | Hold the 4 strongest recent winners that are still trending up; sell one that falls 15% from its 20-day high. |
| `breakout` (default) | Buy stocks hitting new 52-week highs; keep them while they stay within 12% of the high. |
| `pullback` | Buy the dip in strong stocks: up 10%+ over 6 months but recently down 7-20% and oversold (RSI under 40); sell when they bounce back near the high, or if the drop goes past 20%. |
| `sector_rotation` | Find the 2 hottest sectors (gold, uranium, chips, software...) and hold the 2 strongest stocks in each. |
| `leveraged_trend` | Hold QQU (2x NASDAQ-100, CAD) and TQQQ (3x, USD) only while they're in an uptrend; otherwise cash. |
| `combo` | Split the money evenly across several strategies (default: momentum + pullback). |

Every strategy only buys stocks above $2 that trade at least $2M a day and that it can afford at
least one share of, holds at most a few positions (no stock above 30%), keeps the rest in cash, and
sells a holding once it no longer passes its rules. Each strategy's settings are in the config.

**News check (optional, needs an Anthropic API key).** Before deciding, Claude searches the web for
the big picture (trade and tariffs between countries, the Bank of Canada and the US Federal
Reserve, conflicts, tensions and elections, oil, metals, chips, the Canadian dollar) and for news on
the top candidates. The search only uses established news organizations, central banks and
government statistics, regulators and company press releases (the list is `NEWS_SOURCES` in
`agent/strategy.py`), and every item must come with its source, date and link. Claude can then
drop a candidate with bad news or hold more cash, but can only choose from stocks that passed the
rules. Expect roughly C$0.50 to C$1 of API cost per run.

**Two currencies.** TSX stocks are bought with Canadian dollars (`capital_cad`, default $500) and
NASDAQ stocks with US dollars (`capital_usd`, default US$150). The agent never converts currency
and never borrows: put the US dollars in the account yourself (moomoo app: currency exchange), or
set `capital_usd: 0` to skip NASDAQ. Profit and loss are shown in CAD at today's exchange rate.
With small amounts, expensive shares (for example US$300+) can't be bought; the agent skips them.

**CDRs (Canadian Depositary Receipts).** The list includes 12 CDRs on Cboe Canada (NVDA.NE, AAPL.NE,
MSFT.NE, AMZN.NE, GOOG.NE, META.NE, TSLA.NE, AMD.NE, AVGO.NE, NFLX.NE, PLTR.NE, CRWD.NE): the same US
companies, traded in CAD, currency-hedged, at a fraction of the share price (NVIDIA's CDR was about
C$36 when the share was about US$165). Yahoo has no price history for CDRs, so the agent judges their
trend from the US stock (`signal:` in the config) and trades the CDR at its own current price. When a
US stock and its CDR both qualify, it buys only the CDR.

Before trading live, ask moomoo whether it accepts every symbol (it places no orders):

```bash
.venv/bin/python run.py check --live --config config-highrisk.yaml
```

Add any symbol it rejects to `exclude:` in the config.

**Its own money and records.** It manages its own money, recorded in `state-highrisk/` on its first
run, separate from the ETF agent. Changing `capital_*` later doesn't change what it was given. The same safety rules apply: it never sells
anything it didn't buy, never borrows, never short sells, and it never trades your own stocks or
the ETF agent's funds (`exclude` in the config). Only one agent runs at a time, so they never
spend the same cash. If moomoo ever shows fewer shares of a stock than its records say it owns, it
stops trading that stock until you check. Its kill switch is at -40%.

**Compare the strategies on past prices before choosing:**

```bash
.venv/bin/python run.py backtest --config config-highrisk.yaml --strategy all
```

Result on Nov 23, 2026, with the 12 CDRs (weekly, in CAD, after trading costs). A weekly strategy's
result can depend a lot on which weekday it happens to trade, so the backtest replays each strategy
starting on all 5 weekdays and reports the median and the range:

| Strategy | Total (median) | Range over weekdays | Last 2 years (median) | Worst drop |
|---|---|---|---|---|
| momentum | +209% | +62% to +713% | -30% | -59% |
| **breakout (default)** | **+410%** | **+232% to +506%** | **+50%** | -51% |
| pullback | +282% | +89% to +1061% | +35% | -46% |
| sector_rotation | +249% | +126% to +327% | -12% | -58% |
| leveraged_trend | +40% | +27% to +64% | +6% | -22% |
| combo (momentum + pullback) | +328% | +129% to +687% | +9% | -46% |
| just hold XIU (TSX) | +94% | | +42% | -10% |
| just hold QQQ (NASDAQ) | +150% | | +34% | -22% |

Breakout is the default because it had the best median, the narrowest range (it never did badly on
any weekday) and the best last two years, ahead of holding XIU. An earlier version of this README
showed pullback at +1072%: that was one lucky weekday; its median is +282%.

**Read this table with care.** The totals are mostly hindsight: the stock list was chosen in 2026 and
includes stocks now known to have soared (Palantir, AppLovin, Strategy, Rocket Lab...). On the TSX
list alone, momentum made +44% over the whole period, less than holding XIU. Expect real results to
be far lower than this table, and worst drops of 40-60%. High risk does not mean high reward; it
means a wider range of outcomes, good and bad.

Loosening pullback's stop (selling only past a 25% or 30% dip instead of 20%) made every result worse
in an earlier test, so its 20% limit stays. An "oversold rebound" strategy (buying stocks right after
a 15-30% crash, which would have caught NVIDIA's +32% day on Nov 23, 2026) was also tested and lost
money overall (last two years -8% to -50%): for every crashed stock that bounces, others keep falling.

**How often to trade: every 4 weeks.** The same rules were replayed trading daily, weekly, monthly,
quarterly and yearly (Nov 2026 backtest, median of 5 start days, worst drop measured daily):

| Agent / strategy | Daily | Weekly | **Monthly** | Quarterly | Yearly |
|---|---|---|---|---|---|
| ETF agent (trend) | +53% | +63% | **+73%** (+72 to +80) | +79% (+72 to +87) | +70% |
| High-risk breakout | +116% | +410% | **+694%** (+498 to +1020) | +324% | +379% |
| High-risk pullback | +18% | +282% | +149% | +405% | +150% |
| High-risk momentum | +60% | +209% | +537% | +649% | +418% |

Daily was the worst for every strategy, because trading often multiplies costs and reacts to noise.
Monthly was the best or close to it, the steadiest across start days, and keeps the safety checks
frequent, so both agents trade every 28 days. Breakout monthly also had the best last two years
(+74%) and the smallest worst drop (-44%) of the high-risk options. The kill switch is still checked
on every run, due or not. These totals carry the same hindsight caution as the table above.

Run it on pretend money first:

```bash
.venv/bin/python run.py run --config config-highrisk.yaml
```

```bash
.venv/bin/python run.py status --config config-highrisk.yaml
```

Live (real money), exactly like the ETF agent, with `--config config-highrisk.yaml` added. It
trades every 28 days; running it in between only checks the safety rules. Keep enough CAD cash in the account for both agents (`capital_cad` of each), or the second one
will buy less.
