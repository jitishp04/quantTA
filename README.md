# Barbell Scanner

A zero-cost, serverless scanner that watches your universe for **statistical
extremes confirmed by momentum**, and pushes them to Discord. It runs on
GitHub Actions cron — no server, no paid data feed, no API bill.

The alert lands premarket, so the previous session's signals are waiting before
the day opens.

---

## The signal

Two conditions must hold on the **same candle**. Either one alone is noise;
together they mark the tails of the distribution.

| Alert | Condition | Read |
|---|---|---|
| 🩸 **Capitulation** | `Close < BBL_200_2.5` **AND** `RSI_21 < 30` | Accumulate / buy |
| 🔥 **Euphoria** | `Close > BBU_200_2.5` **AND** `RSI_21 > 70` | Trim / take profit |

### The analytical environment

Identical parameters on both timeframes. These are **fixed in `src/config.py`
by design** and deliberately not exposed as settings — they are the noise
filter, not a knob.

| | Daily | Weekly |
|---|---|---|
| Lookback window | 756 candles (~3y) | 260 candles (~5y) |
| Trend anchors | 50 SMA · 200 EMA | 50 SMA · 200 EMA |
| Momentum | 21 RSI | 21 RSI |
| Volatility bands | BB(200, 2.5σ) | BB(200, 2.5σ) |

A 200-period baseline at 2.5σ is a much higher bar than the conventional
(20, 2.0): roughly a 1-in-80 event against three years of context rather than a
weekly occurrence against one month. That is the point — it should stay quiet
for long stretches.

Both timeframes need 200 completed candles before the bands emit a value.
Anything with less history is skipped and reported, never silently dropped.

---

## Setup

One secret, about two minutes. There is no bot application to create, no
privileged intent to enable and nothing to host — a webhook only ever needs to
*send*, and a scheduled scanner never reads the channel.

**1. Create a webhook.** In Discord: your channel → **Edit Channel →
Integrations → Webhooks → New Webhook** → **Copy Webhook URL**. That single URL
is the entire alert transport. No bot, no application, no server.

**2. Push to GitHub.**

```bash
git init
git add .
git commit -m "feat: barbell extremes scanner"
git branch -M main
git remote add origin https://github.com/<you>/quantTA.git
git push -u origin main
```

**3. Add the secret.** Repo → **Settings → Secrets and variables → Actions →
New repository secret**:

| Name | Value |
|---|---|
| `DISCORD_WEBHOOK_URL` | the URL from step 1 |

**4. Allow the workflows to commit.** Repo → **Settings → Actions → General →
Workflow permissions** → **Read and write permissions** → Save. The jobs commit
alert state back to the repo; without this they run fine and then fail on push.

**5. Smoke test.** **Actions → Barbell Scan → Run workflow**. A Discord message
should land within a minute or two.

---

## Managing the watchlist

Edit `config/tickers.txt` and commit — from the GitHub web UI on a phone, or
locally. The next scheduled scan picks it up; nothing else has to happen, and
nothing in the scanner writes to that file.

Paste a list in whatever shape you have it: one per line, comma separated, or
several to a line all work. Case does not matter, blank lines are fine, and
text after `#` is a comment. If a line contains anything that is not a valid
symbol the **whole line** is skipped and logged — so check the run log after a
bulk paste. A typo that quietly drops a position from the scan is far more
expensive than a noisy log line.

Symbols follow Yahoo Finance: `AAPL` · `^GSPC` · `RELIANCE.NS` · `BTC-USD` ·
`EURUSD=X` · `BRK-B`.

### Running something on demand

Both workflows have a manual trigger, so you never have to wait for the cron:

| Want | Do |
|---|---|
| A scan right now | **Actions → Barbell Scan → Run workflow**, with an optional `force` toggle that ignores the cooldown |
| A study of one ticker | **Actions → Signal Study → Run workflow**, then fill in tickers / timeframe / signal / horizon |

Both post their results to the same Discord channel when they finish. The
Actions tab works fine in a phone browser.

---

## Schedule

| Workflow | Cron (UTC) | Purpose |
|---|---|---|
| `scan.yml` | `0 8 * * 1-5` | Premarket scan → Discord alert |
| `study.yml` | manual only | Backtest + Monte Carlo report |

08:00 UTC is 04:00 ET under EDT — exactly when the US premarket session opens —
and 03:00 ET under EST. Cron is UTC-only and does not follow DST, so the
earlier of the two is used: the alert is occasionally an hour early, never
late.

The candle being read is the **previous** session's, settled for hours. Each
weekday covers the prior close and Monday covers Friday's, so all five sessions
are still scanned — just reported the morning after, when the signal is
actionable, rather than on the evening of the close that produced it.

⚠️ Do not move this past ~13:00 UTC. Once the US session opens Yahoo stamps a
still-forming bar for the day in progress, and that bar is discarded
automatically only for weekend-trading instruments (see below) — an equity
would be evaluated on a partial candle. GitHub's scheduler also drifts 5–20
minutes under load, so leave the margin.

`scan.yml` pushes alert state back to the repo, so it uses a `concurrency`
group to stop a manual dispatch interleaving with the scheduled run, and never
cancels in progress — a cancelled scan would record alerts it never delivered.

Cost: a couple of runner-minutes a day. Unlimited free on public repos, and
well inside the 2,000 min/month free tier on private ones.

### How the data is fetched

There is no price cache and no stored database. **Every run re-downloads the
full window from scratch** — 756 daily bars, 260 weekly. Actions runners are
ephemeral, so an incremental cache would have to be committed to the repo, and
you would be maintaining a data store (reconciling splits, backfills and
revisions) to save about nine seconds. Re-fetching is cheaper and always
correct. The only files that persist between runs are `state/` and
`config/tickers.txt`.

Indicators are **never fetched** — Yahoo has no indicator endpoint. yfinance
returns raw OHLCV only; every SMA, EMA, RSI and band is computed locally from
the `Close` column. That is what makes the zero-cost mandate hold, and what
lets the test suite verify the bands against a raw numpy recomputation.

### Only closed candles are evaluated

A signal must never appear and then retract, so an in-progress candle is
discarded before the indicators are computed:

| | Rule | Effect |
|---|---|---|
| **Weekly** | Bar dropped until `today >= week_start + 7d` | Signals confirm on Monday's run, on a fully closed week |
| **Daily, exchange-traded** | Kept — at 08:00 UTC the newest bar is the previous session's | Confirms the morning after the close |
| **Daily, 24/7 (crypto, some FX)** | Bar dated today dropped | Confirms next run, on the closed UTC day |

This matters more than it looks. yfinance stamps a weekly bar with the Monday
that starts it and keeps mutating it all week — mid-week its close is just the
latest daily close. Read on a Wednesday, a "weekly" signal is computed from a
two-day stub and can vanish by Friday. Likewise a crypto daily candle rolls at
00:00 UTC, so when the scan runs at 08:00 UTC the bar dated today is only eight
hours old and still forming.

Whether an instrument trades weekends is detected from its own price index, not
guessed from the symbol suffix, so a `-USD` naming convention is never relied
on. The fetch buffer absorbs the dropped bar, so the analytical window is still
exactly 756 / 260 completed candles.

> Weekly bars are taken from yfinance's native `interval="1wk"`, not resampled
> from daily. Resampling was tested and reproduces `^GSPC` and `BTC-USD`
> exactly, but diverges on adjusted equities — `SPY` weekly high by 2.01 and
> `RELIANCE.NS` close by 48.18 — because yfinance applies split/dividend
> adjustment differently across granularities.

---

## Alert de-duplication

A ticker that sits below its lower band for six weeks would otherwise fire an
identical alert every trading day until you learn to ignore the channel. So
`state/alerts.json` tracks the last alert per ticker per timeframe:

- Signal flips to neutral → record cleared, the setup **re-arms**
- New or changed signal → **alert**
- Same signal still active → silent until `cooldown_days` (default 14) passes,
  then one reminder and the clock resets

Set `cooldown_days: 0` in `config/settings.yml` for strict edge-only alerting.

Because the state is committed to the repo rather than held in memory, a missed
cron run does not cause a duplicate storm on the next one.

---

## Study layers — is the signal actually any good?

The scanner tells you a condition fired. `study.py` tells you whether that has
ever been worth acting on.

```bash
python study.py --tickers SPY --signal capitulation
python study.py --timeframe weekly --horizon 26
python study.py --no-mc --out study.txt      # historical evidence only
```

Or run it on GitHub: **Actions → Signal Study → Run workflow**. The report
lands in the job summary, as a downloadable artifact, and in the Discord
channel. It is deliberately manual — it pulls decades of
history per ticker and simulates 20,000 paths, and a signal's historical edge
does not change week to week.

### Layer 1 — conditional forward returns (evidence)

Finds every historical occurrence of the *exact* live condition and measures
what happened next, at 1/3/6/12-month horizons. Three things make it honest:

- **Edge-triggered, debounced events.** A 36-candle capitulation is one
  observation, not 36. Re-triggers within a month merge into the same episode —
  SPY's `2011-08-08` and `2011-08-10` are one August-2011 selloff, and counting
  both double-weights it while making the statistics look more confident.
- **A baseline.** An 87% win rate at six months sounds like an edge until you
  see equities are up 76% of six-month windows anyway. Every figure is reported
  against the unconditional return over the same instrument and history. The
  **`vs hold`** column is the actual edge; everything else is decoration.
- **Max adverse excursion.** Mean return says the trade worked. MAE says how
  far underwater you sat first — the number that sets position size, and the
  one most backtests quietly omit.

### Layer 2 — Ornstein-Uhlenbeck Monte Carlo (model)

A plain GBM Monte Carlo cannot answer "is this a good buy": it is memoryless,
so it random-walks from the current price with no idea that price is 2.5σ below
its own mean. It reports "volatile, could go either way" for every input.

So the simulated quantity is the **spread**, `z = log(Close) − log(BBM_200)` —
log distance from the same 200-period baseline the bands are built on. A
capitulation is by construction a large negative `z`. Modelling `z` as
Ornstein-Uhlenbeck (an AR(1) fitted by OLS) gives the mean reversion the signal
is implicitly betting on, and yields a reversion half-life, the probability of
returning to the mid-band, and how much further underwater the path goes first.

**Two gates stop this from manufacturing false confidence**, because an OU fit
returns confident-looking numbers for a series with no mean reversion at all:

1. **Dickey-Fuller test** (the t-ratio on `b−1`). Fails → the spread is a
   random walk and the report says so instead of printing percentiles.
   Verified for both size (4.5–7.0% false positives against a nominal 5%,
   empirical critical values −2.78/−2.99/−2.87 vs the theoretical −2.86) and
   power (100% against a true OU).
2. **Half-life vs horizon.** A spread can clear the DF gate at `b = 0.9964` —
   formally mean-reverting, half-life 192 candles, which closes barely a fifth
   of the gap over 63. Statistically real, practically useless. Flagged
   separately.

The baseline is held **flat** over the simulation. Letting it drift up at its
recent slope would bake an assumed bull market into every path and inflate the
reversion probability.

### Reading the output

Real output for `TLT`, daily capitulation:

```
   horizon    n    win%    median      mean   vs hold   MAE med  MAE worst       p
       21d    6   16.7%    -2.61%    -2.07%    -2.38%    -3.38%     -6.41%  0.942
       63d    6   66.7%    +4.25%    +4.38%    +3.39%    -3.64%     -9.12%  0.104
```

At one month this signal is *worse than doing nothing* on TLT (16.7% win rate
against a 53% baseline, p=0.94). SPY over the same test returns +4.89% vs hold
at 21 days with p=0.001. Same signal, opposite verdict — which is the entire
reason to run Layer 1 before trusting an alert.

> **Layer 1 is evidence; Layer 2 is a model.** Overlapping forward windows and
> clustered events make the p-values optimistic, and a handful of 2.5σ events
> per instrument is a small sample by construction. Treat a low event count as
> descriptive, never as significance. Layer 2 is only worth reading once
> Layer 1 shows an edge worth modelling.

---

## Local use

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux
pip install -r requirements.txt

python tests/test_pipeline.py                     # verify the scanner maths
python tests/test_study.py                        # verify the study maths
python scan.py --dry-run --show-all               # print, send nothing
python scan.py --dry-run --tickers SPY,QQQ,TLT    # ad-hoc universe
python scan.py --timeframe daily                  # one environment
python scan.py --force                            # ignore cooldown state
```

`--dry-run` never writes state and never sends, so it is safe to run
repeatedly. Discord credentials are only needed for a real send — put them in
`.env` (see `.env.example`) or export them.

---

## Layout

```
scan.py                  scan entrypoint
study.py                 backtest + Monte Carlo report
config/
  tickers.txt            watchlist (hand-edited)
  settings.yml           cadence, cooldown, heartbeat (human-managed)
src/
  config.py              FIXED indicator parameters and lookback windows
  data.py                batched yfinance download, candle-completeness gates
  indicators.py          vectorised SMA/EMA/RSI/BB computation
  signals.py             capitulation / euphoria evaluation
  state.py               alert de-duplication and cooldown
  notify.py              Discord webhook transport and embed formatting
  backtest.py            Layer 1 - conditional forward returns vs baseline
  montecarlo.py          Layer 2 - OU fit, Dickey-Fuller gate, simulation
state/
  alerts.json            last alert per ticker+timeframe
tests/
  test_pipeline.py       scanner self-checks, incl. band verification
  test_study.py          study self-checks, incl. DF size and power
  test_discord.py        message chunking / fence-balance self-checks
```

---

## A note on `pandas_ta`

`pandas_ta` **0.4.x renamed the Bollinger parameter** from `std=` to
`lower_std=` / `upper_std=`, and the library absorbs unknown keywords into
`**kwargs` without raising. So the natural-looking call

```python
ta.bbands(close, length=200, std=2.5)      # WRONG on 0.4.x
```

returns **2.0σ bands** — no error, no warning, every trigger silently loosened.
Three defences are in place:

1. `requirements.txt` pins the exact version.
2. `src/indicators.py` asserts the returned column names carry the sigma that
   was requested, and raises otherwise.
3. `tests/test_pipeline.py` recomputes the bands from raw numpy and fails if
   they deviate — and the scan workflow runs that test **before** it is allowed
   to send an alert.

If you upgrade `pandas_ta`, run the test suite first.
