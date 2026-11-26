# Adding Crypto Bot: steps for an AI assistant

Your person said something like "add the crypto bot", "install godspeed-crypto-bot" or "I want a trading bot".
Do every technical step yourself. They do three things only: answer the exchange's eligibility question, make the
exchange's own steps that are theirs (a deposit, approving the bot's key), and type the confirmation sentence that
switches on real money. **Never type that sentence for them, never run `go-live` in a way that fakes a
terminal, never write the confirmation file yourself, and never promise or forecast a return.** Crypto Bot can
lose everything put into it; say so once, plainly, before real money, and do not soften it.

The steps below are the same for any exchange. What belongs to one exchange (its eligibility question, its
account and key steps, its terms) is in that exchange's own section at the end: today only Hyperliquid.

## 1. Check that it fits

- Godspeed Mission Control is installed on a **Linux server with systemd**, and you can act as root there.
  A Windows or Mac computer cannot run it: the bot decides every day at 00:02 UTC and checks every five
  minutes, so it needs a server that stays on. If that is the situation, say so in one sentence and stop.
- You need a shell on the server as root. Godspeed's own assistant on the one-line Docker server lives in its
  container and has none: there, tell the person each command to run, or work over SSH if they give you access.
  Their own strategy is the exception: Godspeed's assistant brings it to the bot through files on both installs
  (section 4).
- Find Godspeed's folder on the server (the one that holds `.godspeed/`): `/opt/godspeed-mission-control` on a
  server set up with Godspeed's one-line server installer, or the `godspeed` folder in the home of the account
  Godspeed's assistant runs as on a Linux install of Godspeed itself. Godspeed must be version 2.0.0-beta.6 or
  newer; the installer checks and says so.
- `godspeed-crypto-bot status` answers if Crypto Bot is already there; running the install line again is
  the update.

## 2. The eligibility question

Ask it before installing, in the exchange's words (see the exchange's section). If the answer means they may not
use the exchange, stop: tell them in one sentence that Crypto Bot cannot be set up for them. Do not look for a way
around it. Otherwise go on. The installer records the answer with its date and does not ask again.

## 3. Install (paper mode)

As root, with the Godspeed folder and the person's answer:

```bash
curl -fsSL https://raw.githubusercontent.com/MichaelZelbel/godspeed-crypto-bot/v0.1.1/install.sh | bash -s -- --godspeed <their Godspeed Mission Control folder> --restricted no
```

It starts in **paper** mode: simulated fills on the live order book, nothing signed, 2,000 USDC of pretend
money, strategy `mix`. It finishes with a self-check and one paper run, and prints the page's address. Give
them that address: the page shows the account, every trade, funding and fees and the safety checks, behind
their Godspeed sign-in. On the one-line Docker server it is on port 48445: if their hosting company has a firewall
in front of the server, they open that port there, as for 80 and 443. If the page shows Godspeed's sign-in, they
sign in and open the page's address again. Alerts come to them through their Godspeed's own Telegram bot, only when
they must act or something broke. If the install says their Godspeed has no Telegram connected, help them connect
it in Godspeed (messages wait until then). Then run `godspeed-crypto-bot test-alert` and ask them whether the test
message arrived.

If it refuses, it says why in one sentence; fix that and run the line again (it changes nothing when it
refuses, except that a "yes" to the eligibility question is kept, with its date). To install from a local copy of the repository instead, add `--source <folder or .tar.gz of the repository>`.

## 4. Choosing a strategy

Three built-in choices, one setting: `godspeed-crypto-bot configure --strategy mix|trend|carry`.
`trend` bets on BTC and ETH rising or falling; `carry` holds a coin and bets the same amount on it falling to
collect funding; `mix` (the default) is half of each. A change takes effect at the next daily run. Say what
they do in plain words and do not say which is "best": none of them promises a result.

**Their own strategy** is a file of settings from a fixed menu, `docs/strategy-files.md` in the add-on's folder (the
recipe carries a copy as `strategy-files.md`). Nothing in it is ever run as a program; a setting the menu lacks is
refused, so say plainly what their idea needs that the menu does not have, and never invent a setting.

- **Through the chat door** (Godspeed's own assistant, on both installs): the installer made a `crypto-bot` folder in
  the mission control folder the assistant works in. Write the file as `crypto-bot/strategies/<name>.toml`, then one
  request per step as a new file `crypto-bot/requests/<id>.json`: `{"request": "check", "strategy": "<name>"}`,
  `{"request": "backtest", "strategy": "<name>"}` (optionally `"start"` and `"end"`, `YYYY-MM-DD`) and, after the
  person said yes, `{"request": "use-on-paper", "strategy": "<name>"}`. The bot's relay answers within a minute in
  `crypto-bot/requests/<id>.result.md` (and `.result.json`); a first backtest can take a few minutes while it fetches
  prices. A malformed request is answered with one sentence and changes nothing. `use-on-paper` works only in paper or
  dry-run mode; the door never reaches real money, the cap, the account or the key.
- **In a terminal**: `godspeed-crypto-bot strategy check <file>`, copy it to
  `/etc/godspeed-crypto-bot/strategies/<name>.toml` (readable by everyone), `godspeed-crypto-bot backtest
  file:<name>`, then `godspeed-crypto-bot configure --strategy file:<name>`.

Explain a backtest from its report, worst stretches first (the worst day, week and month, the deepest fall from a
high point and how long it lasted), then costs and how often it traded, next to simply holding the coins. Its first
line says it is the past, not a forecast; say so too.

## 5. Going live (only if they ask, in this order)

First tell them once: real money can be lost entirely, nothing here is investment, tax or legal advice, and
whether they may use the exchange where they live is theirs to check.

1. **Deposit** on the exchange: their own step. They give you their account address (the exchange's section says
   what that is): `godspeed-crypto-bot configure --master <address>`.
2. **The bot's own key.** `godspeed-crypto-bot keygen --network mainnet` makes it on this server and prints
   only its address. The key can trade for their account but can never withdraw or send money out of it.
3. **They approve that address** on the exchange as a trading key for the bot (the exchange's section says
   where and what). Describe what they will see rather than naming buttons; do not give tap-by-tap steps for a
   screen you have not seen.
4. **The most the bot may use.** Ask them how much, in USDC, the bot may use at most. There is no default for
   the amount and you never pick it for them: `godspeed-crypto-bot configure --cap <USDC>`.
5. **They type the confirmation.** Tell them to run `godspeed-crypto-bot go-live` in their own terminal on the
   server. It checks everything first, prints one sentence (the network, their account, the key, the cap and the
   strategy, with the fingerprint of a strategy file), and asks them to type it exactly. Only their typed line switches on real money; it refuses input that is not
   a terminal. If it refuses, read what it says, fix that, and ask them to run it again.
6. `godspeed-crypto-bot preflight` shows ready. From the next daily run (00:02 UTC) real orders are placed,
   with positions sized from the cap they named.

## 6. Later

- **How is it doing:** `godspeed-crypto-bot status`, and the page. Give the facts (account value, what it holds,
  recent trades, anything halted). Never predict.
- **Stop the bot:** `godspeed-crypto-bot halt` (no new trading; positions stay). `resume` lifts it.
- **Sell everything:** `godspeed-crypto-bot flatten --yes` tries to close every position and halts. Ask first; it
  sends real orders in live mode. Read its answer: if something could not be closed it says what, and tells the person.
- **Tax export:** `godspeed-crypto-bot export --start YYYY-MM-DD --end YYYY-MM-DD --out <file>` (fills, fees,
  funding and transfers in UTC; the euro column is blank).
- **Real money paused after a change:** the confirmation names the strategy, so switching strategy or changing the
  strategy file while live pauses real money, and so does an update from version 0.1 (its confirmation named no
  strategy). `preflight` and the bot's message say so; they run `go-live` and type the new sentence themselves.
- **Renew the key** (the Godspeed reminder says when): `godspeed-crypto-bot keygen --network mainnet --replace`,
  they approve the new address on the exchange, they run `go-live` again (the sentence names the key), then
  `preflight`.
- **The exchange changed its terms:** the weekly check stops new positions and tells them what the clause now
  says. Read it with them; they decide whether they may still use the exchange. `terms-accept` adopts the new
  terms after that.
- **Take it out:** `godspeed-crypto-bot uninstall` (keeps records and settings; `--purge` removes them too). It
  refuses while the account holds positions unless `--yes`; positions stay open at the exchange unless
  `flatten --yes` ran first, and the key stays approved there until they remove it. A reminder to renew the key
  stays among their Godspeed reminders until they close it there (the uninstall says so).

## Hyperliquid (the exchange-specific section; another exchange adds its own beside it)

Everything that names Hyperliquid in the steps above is here. Its code lives in `godspeed_crypto_bot/venue/`
and `godspeed_crypto_bot/terms.py`.

- **The eligibility question** (section 2). Ask: "Do you live in the United States or Ontario (Canada), or are you
  a resident or citizen of a country under international sanctions?" Hyperliquid's terms of use do not allow
  those people to use it. Yes: stop. No: pass `--restricted no` to the install line. The weekly terms check
  tells the person when Hyperliquid's list changes.
- **Deposit** (going live, step 1). On Hyperliquid, with the person's own wallet. The wallet address is the
  account: that is the `--master` address (a public address, 0x followed by 40 characters).
- **Approving the key** (going live, step 3). On Hyperliquid's API page (app.hyperliquid.xyz/API) the person
  approves the address `keygen` printed as an API wallet, with a name they recognise. An API wallet can trade
  but cannot withdraw; it expires after at most 180 days, and once the key is live the bot keeps a reminder for
  the renewal among the person's Godspeed reminders.
- **Terms.** Hyperliquid's terms of use (clause "Restricted Persons") are checked weekly; see "The exchange
  changed its terms" above.
- **The practice exchange.** `network = "testnet"` runs the same code on Hyperliquid's testnet; `selftest` there
  proves live signing, the order paths and that the bot's key cannot withdraw or send.
