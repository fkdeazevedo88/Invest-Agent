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

1. Open a **Moomoo Financial Canada** account and fund it. Use a TFSA so gains aren't taxed.
2. Download **OpenD** (moomoo's API gateway) from moomoo's OpenAPI page, run it on the computer
   that will run the agent, and log in. It listens on `127.0.0.1:11111`.
3. Enable API trading in the moomoo app if it asks you to, and set your 6-digit trade password.
4. If you have more than one account, put its ID in `config.yaml` under `moomoo.acc_id`.
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
- Orders are limit orders 0.3% from the last price. On a fast-moving day some may not fill. The next
  run picks up whatever is left.
- To stop everything: don't run it, or create a file named `state/HALT`.
