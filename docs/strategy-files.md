# Your own strategy file

Crypto Bot can run a strategy of your own instead of one of its three built-in ones. You write it as a short text
file of settings (in the TOML format), the bot checks every setting against a fixed menu, and it tells you back in
plain words what it understood. A strategy file is never a program: nothing in it is run. A setting the menu does not
have is refused, never guessed.

Nothing here is advice or a forecast. A strategy that looks sensible can lose money, and so can every example below.

## Where the file goes and how it is used

1. Write the file, for example `my-trend.toml`. The `name` inside the file must be the file's name without `.toml`.
2. Check it: `godspeed-crypto-bot strategy check my-trend.toml`. It lists every problem, one sentence each, or says
   in plain words what the bot understood, with a short fingerprint of the file (it changes whenever the file
   changes).
3. Put it in the bot's strategies folder, `/etc/godspeed-crypto-bot/strategies/my-trend.toml`, and choose it:
   `godspeed-crypto-bot configure --strategy file:my-trend`. That checks the file again and refuses one that fails.
   From the next daily run (00:02 UTC) the bot runs it, and `status`, the page and the weekly report name it.

In this version a strategy file runs on paper (simulated money on real prices) and in dry-run; for real money choose
one of the built-in strategies.

## The settings

Every file has these settings at the top:

- `name`: 1 to 40 lowercase letters, digits or dashes, starting with a letter, and the same as the file's name.
- `description` (optional): your own words for it, up to 500 characters. The bot does not read it.
- `coins`: 1 to 10 of Hyperliquid's perpetual contracts, written as Hyperliquid writes them, like
  `["BTC", "ETH"]`. They must be listed on Hyperliquid when the file is checked.
- `rebalance`: how often it trades. `"daily"` (every day), `"weekly"` (every Monday) or `"monthly"` (the first day
  of each month). In between it leaves the positions as they are. It also trades on the first day it runs.
- `direction`: `"long-short"` (bets on rises and on falls), `"long-only"` (only bets on rises; where the signal says
  falling it holds nothing) or `"short-only"` (only bets on falls).
- `no_trade_band` (optional, 0.02 if left out): a share of the money from 0 to 0.5. While a coin's position is within
  this share of the money of its target, the bot leaves it alone, unless the bet turns around or ends. It saves fees.

Then two tables: `[signal]`, what it looks at, and `[sizing]`, how much it puts on each coin.

## The signals (`[signal]`)

Each signal gives every coin a number from -1 (bet on falling) to 1 (bet on rising) from the daily closing prices
up to yesterday's close. Choose one with `kind`:

- `kind = "momentum"`, `lookbacks = [20, 60, 120]`: for each number of days (1 to 5 of them, each from 2 to 365), a
  rise over that many days counts as a vote for rising and a fall as a vote for falling; the votes are averaged.
- `kind = "moving-average-cross"`, `fast = 20`, `slow = 100`: the average closing price over the last `fast` days
  against the average over the last `slow` days (2 to 365 days, `slow` longer than `fast`). The shorter average above
  the longer one counts as rising (1), below it as falling (-1).
- `kind = "mean-reversion"`, `days = 14`, `buy_below = 30`, `sell_above = 70`: how much of the coin's daily moves over
  the last `days` days (2 to 100) were rises, on a scale from 0 to 100. Below `buy_below` it bets on a rise back (1),
  above `sell_above` on a fall back (-1), in between it holds nothing in that coin (0).
- `kind = "funding"`, `days = 7`, and optionally `min_annual_rate = 0.1`: the funding each coin paid over the last
  `days` days (1 to 30). Where bets on rising paid bets on falling it bets on falling (-1), and the other way round
  (1), so it is on the side that receives the funding. With `min_annual_rate` it holds nothing in a coin whose
  funding came to less than that yearly rate (0.1 is 10% a year).
- `kind = "fixed"`, `weights = { BTC = 0.6, ETH = 0.4 }`: one number from -1 to 1 for every coin in `coins`. On its
  own, a fixed signal is the share of the money held in each coin (negative is a bet on falling), set back to those
  shares on every rebalance day. Its `[sizing]` table may then hold only `gross_exposure` and `max_per_coin`, and the
  weights must fit inside them.
- `kind = "average"`: 2 to 5 of the signals above, each written as its own `[[signal.parts]]` table with its kind, its
  settings and a `weight` (above 0, at most 10), averaged by those weights. Inside an average, fixed weights count as
  a signal from -1 to 1 like the others.

A coin is traded once it has 120 days of daily prices on Hyperliquid. A signal that needs more days than the coin
has gives no number, and the coin is not traded that day. A day on which a coin did not trade at all, or opened more
than 50% away from the day before, breaks its run of days: its signals count only the days since then, as the
built-in strategies do.

## Sizing (`[sizing]`)

- `method = "equal"`: the money is shared equally among the coins it can trade that day, each coin's share
  multiplied by its signal, so a weaker signal gets a smaller share.
- `method = "volatility"`, `days = 60`: each coin's share is set by how much its price swung over the last `days`
  days (10 to 365): a coin that swings half as much gets twice the share. With `target_volatility = 0.2` the whole
  book is instead scaled so its price swings come to about that much a year by recent standards (0.2 is 20%),
  measured together over `covariance_days` (10 to 365, 90 if left out). This is how the built-in trend strategy sizes.
- `gross_exposure` (1 if left out): the most it bets in all, as a share of the money, above 0 and at most 2 (twice
  the money, the most the bot's limits allow).
- `max_per_coin` (1 if left out): the most on one coin, above 0 and at most 1.

The bot's own limits apply on top, exactly as for the built-in strategies: the cap on the money it may use, at most
1.4 times that cap per order and 4 times per day, the halt after a 25% loss, the five-minute guard and, for real
money, the confirmation you type yourself. Orders under $11 are skipped.

## Three examples

### Trend on BTC and ETH, every Monday

The built-in trend strategy's rules, but trading only on Mondays.

```toml
name = "weekly-trend"
description = "Follow the trend of BTC and ETH, checked once a week."
coins = ["BTC", "ETH"]
rebalance = "weekly"
direction = "long-short"
no_trade_band = 0.02

[signal]
kind = "momentum"
lookbacks = [20, 60, 120]

[sizing]
method = "volatility"
days = 60
target_volatility = 0.2
gross_exposure = 2.0
max_per_coin = 1.0
```

### Moving averages and funding together, every day

Half the say to the moving-average cross, half to the side that receives funding, on three coins, shared equally.

```toml
name = "cross-and-funding"
coins = ["BTC", "ETH", "SOL"]
rebalance = "daily"
direction = "long-short"
no_trade_band = 0.05

[signal]
kind = "average"

[[signal.parts]]
kind = "moving-average-cross"
fast = 20
slow = 100
weight = 1

[[signal.parts]]
kind = "funding"
days = 7
weight = 1

[sizing]
method = "equal"
gross_exposure = 1.0
max_per_coin = 0.5
```

### Sixty-forty, set back every month

60% of the money as a bet on BTC rising and 40% on ETH rising, set back to those shares on the first of each month.

```toml
name = "sixty-forty"
coins = ["BTC", "ETH"]
rebalance = "monthly"
direction = "long-only"
no_trade_band = 0.02

[signal]
kind = "fixed"
weights = { BTC = 0.6, ETH = 0.4 }

[sizing]
gross_exposure = 1.0
```

## What a strategy file cannot do

- Run code, call a program or reach the bot's key: it is settings only.
- Hold the coins themselves (spot tokens): it bets on prices through perpetual contracts only. The built-in carry
  strategy keeps its own spot-and-perpetual pairs.
- Trade on another exchange than Hyperliquid, more often than once a day, or on prices within the day (no stop
  orders).
- Try thousands of settings against the past until one looks good. The bot tests exactly the file it is given.
