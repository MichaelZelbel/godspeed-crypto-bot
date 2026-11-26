# Crypto Bot

> **Read this first.** Crypto Bot is free software that comes with no warranty. It promises no results, and no
> figure anywhere in this repository is a forecast. **You can lose everything you put into it.** It is not
> investment, tax or legal advice. Whether you may use Hyperliquid where you live is yours to check: its terms
> of use exclude people who live in the United States or Ontario (Canada) and residents or citizens of
> sanctioned countries. It starts in paper mode, with simulated money, and trades real money only after you have
> typed a confirmation yourself.

Crypto Bot is a trading bot for [Hyperliquid](https://hyperliquid.xyz), made as an add-on for your own
[Godspeed Mission Control](https://godspeedmissioncontrol.com) server (technical name `godspeed-crypto-bot`). It
decides once a day, checks your account every five minutes, tells you through your own Godspeed when something
needs you, shows its page behind your Godspeed sign-in, and keeps the renewal of its exchange key among your
Godspeed reminders. Nobody else holds your money or your keys. Crypto Bot takes no cut and charges nothing itself; Hyperliquid's own trading fees apply, and they are in the records.

## What it does

- **Three built-in strategies, you choose one** with the `strategy` setting:
  - `"trend"`: trend on BTC and ETH. It bets on the two coins rising or falling, depending on how they moved over
    the last one to four months, so it moves with the market.
  - `"carry"`: carry on BTC, ETH and SOL. It holds a coin and bets the same amount on its price falling, so price
    moves cancel out, and it collects the funding that bets on falling earn when most traders bet on rising.
  - `"mix"` (the default): half trend, half carry, set back to half and half every month.
  Risk limits, the cap, the guard and the live gate are the same for all three. Changing the setting takes effect
  at the next daily run, as a rebalance to the new targets. `status`, the page and the weekly report name the
  strategy in plain words.
- **Or your own strategy**, as a file of settings (`strategy = "file:<name>"`): which perpetuals, a signal from a
  fixed menu (momentum, moving-average cross, mean reversion, funding, fixed weights or an average of these), the
  direction, the sizing and how often it trades. `strategy check <file>` refuses anything outside the menu, one
  sentence per problem, or says in plain words what it understood. It is settings, never code. In this version it
  runs on paper and in dry-run. The format and three examples: [docs/strategy-files.md](docs/strategy-files.md).
- **Paper first.** The installer starts in paper mode: the bot trades a pretend account against the live order
  book and books the real hourly funding, and nothing is signed or sent.
- **Real money only by your own typed sentence.** `go-live` prints one sentence (the network, your account, the
  bot's key and the most it may use); only you typing it, in a terminal, writes the confirmation file. The bot's
  key can place orders but can never withdraw or send money out of your account.
- **Safety checks.** A guard every five minutes, hard limits, a halt on a loss, a halt when the account
  stops matching the bot's books, a weekly check of Hyperliquid's terms. They limit risk; they do not remove it.

## Install

On a Linux server that runs Godspeed Mission Control, with systemd, as root. A Windows or Mac computer cannot run
it: the bot decides every day at 00:02 UTC and checks every five minutes, so it needs a server that stays on.

    curl -fsSL https://raw.githubusercontent.com/MichaelZelbel/godspeed-crypto-bot/v0.1.1/install.sh | bash -s -- --godspeed <your Godspeed Mission Control folder>

The Godspeed folder is the one that holds `.godspeed/`: `/opt/godspeed-mission-control` on a server set up with
Godspeed's one-line server installer, or the `godspeed` folder in the home of the account Godspeed's assistant runs as
on a Linux install of Godspeed itself. The line names the published release (`v0.1.1`), never the newest code. It asks once whether you live in the
United States or Ontario, or are a resident or citizen of a sanctioned country (your assistant can pass
`--restricted no`); if you do, it stops. Then it installs `uv` if it is missing, the program into
`/opt/godspeed-crypto-bot`, a system user, the timers and a configuration in **paper** mode (`network = "mainnet"`,
`strategy = "mix"`, 2,000 USDC of pretend money), checks itself, does one paper run and prints the page's address.
It makes no key and never switches to real money.

- It refuses, in one sentence and changing nothing: not Linux, no systemd, not root, no Godspeed at the folder, or a
  Godspeed older than 2.0.0-beta.6 (the first release with the deadline door on both server installs).
- **Running it again is the update**: the configuration, key, records, HALT file and page stay.
- **What changed in 0.1.1:** on a native Godspeed server the bot no longer restarts Godspeed's web address.
- **Remove it:** `godspeed-crypto-bot uninstall` stops and removes the timers, command, page address, program and
  user, and keeps the records and settings; `--purge` removes those too. It never touches the exchange: positions
  stay open unless you ran `flatten --yes` first (it refuses while the account holds positions, unless `--yes`), and
  the API wallet stays approved on Hyperliquid until you remove it there. A reminder to renew the key stays among your
  Godspeed reminders until you close it there; the uninstall says so when there is one.
- To install from your own checkout, add `--source <folder or .tar.gz>` (or set `CRYPTO_BOT_SOURCE`).

An assistant with a shell on the server can do all of it for you: [INSTALL-FOR-ASSISTANTS.md](INSTALL-FOR-ASSISTANTS.md)
holds its steps. On a Godspeed installed on the server itself, the installer also puts a recipe (`skill/crypto-bot`)
where Godspeed's assistant reads recipes. On the one-line Docker server Godspeed's assistant lives in its container and
cannot run commands on the server, so you run them there, or an assistant you use over SSH does.

## Going live

Real money is the last step, and the only one the bot cannot do for you. In order:

1. Deposit on Hyperliquid (your own step; the wallet address is the account).
2. `godspeed-crypto-bot configure --master <your address>` tells the bot which account is yours.
3. `godspeed-crypto-bot keygen --network mainnet` makes the bot's own key on this server and prints its address.
4. You approve that address as an API wallet on Hyperliquid's API page. It expires after at most 180 days; the bot
   keeps a reminder for the renewal among your Godspeed reminders.
5. You name the most the bot may use: `godspeed-crypto-bot configure --cap <USDC>` (there is no default for live).
   Then `godspeed-crypto-bot go-live`, typed by you in a terminal: it checks everything, prints the sentence, and
   switches on real money only when you type that sentence exactly. Input that is not a terminal is refused.
6. From the next daily run real orders are placed, with positions sized from the cap you named. `halt` stops the bot;
   `flatten --yes` tries to close everything (and says what it could not).

## What the guard and the limits do

- **Cap:** the strategy trades with at most `capital_cap_usdc`; money above it is ignored. Per order at most 1.4
  times the cap, per day at most 4 times the cap of traded value. Every perpetual is set to 3x cross.
- **Halt on loss:** equity 25% below the starting capital halts, tries to close everything and tells you.
- **Guard (every five minutes):** compares what Hyperliquid holds with what the bot's books say it should hold (after
  a forced deleveraging it tries to restore the hedge, or sells the leftover; anything it cannot explain halts it),
  watches the price of the bridged tokens UBTC, UETH and USOL against their perpetuals (a gap stops new carry, a wide
  gap makes it try to close the pairs) and works to keep leverage and margin inside limits (it moves free cash, then
  shrinks carry pairs, and tells you when it cannot). Each of these can fail in a fast or thin market.
- **Terms check (weekly):** if Hyperliquid's terms of use change, new positions stop and you are sent the new
  clause to read; existing positions are kept.
- **Messages:** every halt the bot makes itself (a loss, a mismatch, an expired key) and every missed daily run is
  reported to you in one plain message through your Godspeed, when its channel works; a message waits and is retried
  until it does. A halt you ask for with `halt` is not announced.

## Commands

On the server everything goes through `/usr/local/sbin/godspeed-crypto-bot`:

    godspeed-crypto-bot status | report [--weekly] | preflight | test-alert
    godspeed-crypto-bot configure [--master ADDRESS] [--cap USDC] [--strategy mix|trend|carry|file:<name>]
    godspeed-crypto-bot strategy check <file> [--json]
    godspeed-crypto-bot keygen --network mainnet|testnet [--replace] | go-live | account-mode
    godspeed-crypto-bot halt [--reason R] | resume | flatten --yes [--reason R]
    godspeed-crypto-bot run-daily [--allow-late] | guard | check-deadline | terms-check | terms-accept
    godspeed-crypto-bot export [--start YYYY-MM-DD] [--end YYYY-MM-DD] [--out FILE]
    godspeed-crypto-bot page | relay | eligibility --restricted yes|no | uninstall [--purge] [--yes]
    godspeed-crypto-bot --config <testnet config> selftest [--spot TOKEN]

## Records and the tax export

Everything is in SQLite at `/var/lib/godspeed-crypto-bot/bot.sqlite`: every run with its saved decision, every
order and attempt, fills with their slippage against the decision price, funding payments, equity per day, skipped
orders, guard actions, alerts, and the exchange's own fill and ledger records with their hashes.
`export` writes a tax-ready CSV of fills, fees, funding and transfers (UTC, the euro column blank).
`report` compares the account with the backtest's assumptions on the same days and splits any difference into
fills, fees, funding and orders that never happened. The page shows the account against its starting money, what it
holds, every trade (one sentence each), funding and fees per day, and what the safety checks did.

## Limits worth knowing

- Trading with real money can lose all of it. Paper trading does not remove the exchange's forced deleveraging, the
  bridge (2-of-3 MPC) behind the Unit tokens UBTC, UETH and USOL, failure of the exchange itself, or legal
  eligibility; the guard, the peg check and the terms check only limit them.
- Live trading is one cross-margin account with one position per coin. The guard works to keep perpetual leverage under 4x, and can fail to;
  the virtual books do not simulate isolated liquidations.
- Whether you may use Hyperliquid where you live is yours to check. The weekly terms check tells you when its list
  changes; it does not decide for you.
- A server is needed because the bot runs on a schedule. If the server is off at 00:02 UTC the day is skipped (a
  scheduled run starting after 06:00 UTC skips it) and you are told.
- Crypto Bot supports Hyperliquid only. See "Another exchange".

## Another exchange

The exchange is meant to be one replaceable part, and it is not fully that yet. The strategy
(`godspeed_crypto_bot/strategy.py`) is one pure function that knows no exchange, key or clock and never imports from
`venue/`. What talks to Hyperliquid (the API, instruments, signing, account modes, the public data the uninstall check
reads) lives in `godspeed_crypto_bot/venue/`, and the terms check in `godspeed_crypto_bot/terms.py`. A few other
places still name Hyperliquid: the way the bot builds its venue (`bot.py`), the settings for its tokens and networks
(`config.py`), the country question (`eligibility.py`), the safety checks that know the Unit tokens, the page text and
the self-test. So a second exchange is an addition that implements the interface in `venue/base.py`, its own terms
check and its own section in the assistant's instructions, and then widens those places. It is not only a new folder.

## How it works

**Strategy.** `strategy.py` holds `decide(history, books, equity)`. The trend book takes the average sign of
the 20, 60 and 120 day returns, sizes by 60 day volatility toward a volatility target with a covariance estimate,
with caps per coin and for the book, and a no-trade band. The carry book holds the Unit spot token long
and the perpetual short in equal quantity, enters when the trailing 7 day funding rate is above a threshold and exits
when it is below zero, resets both legs after a large price move and shares its equity between the active coins. In
the mix the two books are virtual, reset to half and half monthly, and netted per coin into one Hyperliquid position.
All inputs are Hyperliquid's own daily candles and hourly funding, through the 00:00 UTC close.

**Where the strategy comes from.** The arithmetic is vendored from the research that chose it, a long-history
hold-out test run separately (its engine is not part of this repository). `tests/fixtures/s3maj_holdout_parity.json.gz`
is that engine's own replay, and `tests/test_parity.py` feeds the same inputs to `decide` on every one of its
1,016 days and requires the same targets (bit for bit on the platform the fixture was made on, 1e-12 relative on
others), for the mix and for each single book. Nothing in it is a forecast of what the strategy will do from now on.

**The daily run** claims the day in SQLite (a finished day is never repeated; an interrupted one resumes with the
decision it saved). Every order is target minus what the account holds, read just before, so a rerun only does what
the first attempt did not. Perpetual orders are limit orders that fill immediately or cancel, at the mid price
moved by a small allowance; orders under $11 are skipped and recorded. A carry pair's two legs go to Hyperliquid
back to back, spot first; if a leg still fills under 90% after three attempts the bot tries to reverse the whole pair and you
are told.

**API wallet expiry.** The expiry is read from Hyperliquid each day. When a live key exists the bot keeps one
reminder in Godspeed's deadline list ("Renew Crypto Bot's exchange key before it expires"), and moving the key moves
its date. `preflight` refuses to go live with less than 14 days left. At expiry the bot halts and tells you.

**Messages.** The bot writes each message to an outbox; one root unit (`relay`, every minute) tells it on your
assistant's Telegram bot, the way Godspeed's other add-ons do, and keeps the reminder and the page's address. A
message waits and is retried until a channel takes it. Practice alerts (paper, dry-run, testnet) are marked as such.
When your Godspeed has no Telegram connected, the installer and `status` say so, and messages wait until it has.
`test-alert` queues one test message for your Godspeed's Telegram; check whether it arrives.

**Modes.** `dry-run` simulates every intended order at the mid price moved by the backtest's slippage model.
`paper` fills against the live order book and books the real funding. `live` signs with the bot's own key and
requires `mode = "live"`, the key, the account address, a cap you named, the country answer, and the confirmation
file for that network, whose text names the network, the account, the key and the cap. The bot never writes that
file. `network = "testnet"` runs the same code on Hyperliquid's practice exchange, where `selftest` proves
live signing, the order paths and, above all, that the bot's key cannot withdraw or send money: a withdrawal or send
is signed by the account's own key, and Hyperliquid attributes an API wallet's attempt to the API wallet's own empty
address, so the balance cannot move.

**Account modes.** The bot runs in either of Hyperliquid's two ordinary balance modes (unified account, or separate
spot and perpetual balances) and never changes the mode; `preflight` refuses anything else.

**The page** is one HTML file in plain words, written to `/var/lib/godspeed-crypto-bot-page/` and served behind your
Godspeed sign-in: on a native Godspeed server by one file in Caddy's add-on folder, on the one-line Docker server by a
small Caddy container of its own, on port 48445 of your Godspeed's address. If your hosting company has a firewall in
front of the server, open port 48445 there, as you did for 80 and 443. It is never a public link: without your
Godspeed sign-in it sends you to Godspeed's own sign-in, and after signing in you open the page's address again. Its
times are UTC, with the server's own time zone beside them when that is not UTC (`timedatectl set-timezone` changes it).

**Where things live.** Code `/opt/godspeed-crypto-bot`; configuration `/etc/godspeed-crypto-bot/config.toml`; your strategy files
`/etc/godspeed-crypto-bot/strategies/`; state
`/var/lib/godspeed-crypto-bot`; logs `journalctl -u 'godspeed-crypto-bot-*'`. The names come from
`godspeed_crypto_bot/names.py` and one variable in the installer.

| When (UTC) | Unit | What |
|---|---|---|
| 00:02 daily | `godspeed-crypto-bot-daily` | read yesterday's close, decide, trade, record |
| every 5 min | `...-guard` | holdings, peg, drawdown, margin |
| 00:45 daily | `...-deadline` | tell you if the daily run did not finish; key expiry |
| Mon 01:00 | `...-report` | weekly comparison with the backtest's assumptions |
| Mon 06:00 | `...-terms` | whether Hyperliquid's terms of use changed |
| every minute | `...-relay` | root: messages, the key reminder, the page's address |

## For developers

`uv sync --locked`, `uv run pytest`, `uv run ruff check .`. The installer is tested end to end on a throwaway Linux
machine with systemd by `tests/test_install.sh` (see its header); the workflow `.github/workflows/install-scenario.yml` runs it
on a clean Ubuntu server every night and on demand, and opens an issue labelled `scenario-failure` when it fails. `tests/test_private_details.py` runs
`scripts/check-private-details.sh`, which fails on anything of a maintainer's own setup. Regenerating the parity
fixture needs the private research engine and is not part of normal development.

## Licence

MIT, see [LICENSE](LICENSE).
