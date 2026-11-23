# Investing agent ($1,000 CAD, moomoo)

An automated agent that manages a small portfolio of Canadian ETFs. Once a week it reads market data,
decides target weights, and places whole-share limit orders. By default it trades a **paper**
(simulated) account. Real money only moves with `--live`.

## How it decides

1. **Kill switch (always runs first, plain code).** If the account falls below 80% of the starting
   $1,000, it sells everything and refuses to run again until you delete `state/HALT`.
2. **Rules baseline.** It holds ETFs that are above their 200-day average and have positive 3/6/12-month
   momentum, weighted by inverse volatility. The rest goes to `CASH.TO` (a high-interest savings ETF).
3. **Claude (optional).** If `ANTHROPIC_API_KEY` is set, Claude sees the signals, your holdings, the
   baseline and its own last 3 decisions, then proposes weights with a written rationale. If Claude
   fails, the agent uses the baseline.
4. **Guardrails (code, not AI).** Only the 8 ETFs in `config.yaml` can be bought. No single ETF can go
   above 40% (cash excepted). No leverage, no shorting, no options. Small trades are skipped, and the
   agent never manages more than `max_equity_cad`, even if the account holds more.

Every run is logged to `state/journal.jsonl`.

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
| `momentum` (default) | Hold the 4 strongest recent winners that are still trending up; sell one that falls 15% from its 20-day high. |
| `breakout` | Buy stocks hitting new 52-week highs; keep them while they stay within 12% of the high. |
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

Result on Nov 23, 2026 (weekly, in CAD, after trading costs, $1,000 start):

| Strategy | Total (3.8 yrs) | Worst drop | 1st half (Dec 2022 - Nov 2024) | 2nd half (Nov 2024 - Nov 2026) |
|---|---|---|---|---|
| momentum | +726% | -47% | +337% | **-49%** |
| breakout | +512% | -52% | +295% | -4% |
| pullback | +1072% | -33% | +332% | **+42%** |
| sector_rotation | +174% | -37% | +171% | -50% |
| leveraged_trend | +64% | -20% | +50% | -1% |
| combo (momentum + pullback) | +701% | -40% | +320% | -17% |
| just hold XIU (TSX) | +94% | -10% | +32% | +42% |
| just hold QQQ (NASDAQ) | +150% | -22% | +85% | +34% |

**Read this table with care.** The huge totals come almost entirely from the first half, and they
are mostly hindsight: the NASDAQ list was chosen in 2026 and includes stocks that are now known to
have soared (Palantir, AppLovin, Strategy, Rocket Lab...). On the TSX list alone, momentum made
+44% over the whole period, less than holding XIU. In the last two years, every high-risk strategy
did worse than or the same as simply holding the market, and momentum lost about half its money.
`pullback` is the only one that made money in both halves. High risk does not mean high reward;
it means a wider range of outcomes, good and bad.

Run it on pretend money first:

```bash
.venv/bin/python run.py run --config config-highrisk.yaml
```

```bash
.venv/bin/python run.py status --config config-highrisk.yaml
```

Live (real money), exactly like the ETF agent, with `--config config-highrisk.yaml` added. For its
stop-loss to react faster, run it every weekday instead of weekly. Keep enough CAD cash in the
account for both agents (`capital_cad` of each), or the second one will buy less.
