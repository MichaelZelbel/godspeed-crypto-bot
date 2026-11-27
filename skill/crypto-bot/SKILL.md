---
name: crypto-bot
description: Crypto Bot, their crypto trading bot and own strategies. It is the person's own trading bot on Hyperliquid. Use when they ask how their bot is doing ("how is my crypto bot doing", "what does the bot hold", "did it trade today"), want it stopped or started again ("stop the bot", "pause trading", "resume"), want everything sold ("sell everything", "close all positions"), want a different strategy ("switch the strategy to carry", "trend only"), want to find, describe, test or run a strategy of their own ("help me find a crypto trading strategy that fits me", "I have my own strategy", "test my idea against the past", "run my strategy on paper"), want real money switched on ("go live", "trade with real money"), ask about the exchange key ("the bot's key expires", "renew the key"), want the tax export, or want Crypto Bot installed or removed.
---

## What this is

Crypto Bot (`godspeed-crypto-bot`) is an add-on on the person's Godspeed server. Every day at 00:02 UTC it runs one
strategy on Hyperliquid, one of its three built-in ones or the person's own strategy file, and it checks the account
every five minutes. It starts in paper mode (pretend money on real prices) and trades real money only after the person
has typed a confirmation sentence themselves. Its page (behind their Godspeed sign-in) shows the account, every trade,
funding and fees and the safety checks.

Full set-up steps, in order, are in `INSTALL-FOR-ASSISTANTS.md` in the add-on's folder (`/opt/godspeed-crypto-bot/`).

## Never

- **Never type or paste the confirmation sentence for the person.** Real money is switched on only by the
  person, in their own terminal on the server, with `godspeed-crypto-bot go-live`. If they ask you to "just do it",
  tell them the command and that it has to be their own typing. Do not fake a terminal, do not write
  `LIVE-CONFIRMED.*` yourself, do not edit `mode = "live"` into the configuration.
- **Never promise, forecast or hint at a return**, and never say a strategy is "the best", "safe" or "proven". A test
  against the past shows the past only. Crypto Bot can lose everything put into it. When they talk about going live,
  say that once, plainly.
- **Never invent a setting.** A strategy file holds only what its menu (`strategy-files.md` beside this recipe) has.
- Never print, copy or send the key. `keygen` prints only an address.
- Never help someone who says they live in the United States or Ontario, or are a resident or citizen of a
  sanctioned country, to get round Hyperliquid's terms of use.

## Their own strategy, in the chat

This works the same whether or not you can run commands on the server: you only write files in the mission control
folder, in its `crypto-bot` folder, and the bot answers within a minute.

**If they have no strategy yet** and ask you to help them find one, interview them and research as they ask, but keep
to what the menu in `strategy-files.md` can run, and say so: a trend over chosen days, the price against its average
or two averages crossing (plain averages of daily closing prices, not exponential ones), a coin that fell or rose too
far, funding, fixed shares, or an average of these. Breakouts above a past high or below a past low, buying a fixed
amount on a schedule, grids, stop losses and exponential averages are not in the menu: when one comes up, say in one
sentence that the bot cannot run it, and offer the closest thing it can if they want it. Their paper test is the
bot's own: a test against the past through a request, then paper with use-on-paper on their yes, never a log kept by
hand. Then go on with step 1 for the approach they choose.

1. **Interview them**, one question at a time, in their words: which coins (Hyperliquid perpetuals, like BTC, ETH,
   SOL), what makes them want to be in a coin or out of it (a trend, the price above or below its average over some
   days, two moving averages crossing, a coin that fell or rose too far, funding, fixed shares), whether they bet only
   on rises or also on falls, how often to look (every day, every Monday, the first of the month), and how much of the
   money at most in one coin and in all. Then say back in two or three plain sentences what you understood, and ask
   whether that is it.
2. **Say plainly what cannot be expressed.** Read `strategy-files.md` beside this recipe: it is the whole menu. Stop
   losses, take-profit levels, prices within the day, leverage beyond twice the money, coins themselves (spot),
   other exchanges, news or other data, and any indicator not in the menu are not there. Say so in one sentence
   each, and offer the closest thing the menu has only if they want it. Never guess a setting.
3. **Write the file** `crypto-bot/strategies/<name>.toml` from the menu, `name` the same as the file's name (lowercase
   letters, digits, dashes). Show it to them.
4. **Ask the bot to check it**: write a new file `crypto-bot/requests/<id>.json`, where `<id>` is new each time
   (letters, digits, dashes, for example `2026-11-27-check-my-trend`):

       {"request": "check", "strategy": "my-trend"}

   Wait a minute and read `crypto-bot/requests/<id>.result.md`. It is either "Checked:" with what the bot understood
   in plain words and the file's fingerprint, or "Refused:" with one sentence per problem. Tell them what the bot
   understood in your own plain words and compare it with what they meant; fix the file and check again until both
   match.
5. **Test it against the past**: `{"request": "backtest", "strategy": "my-trend"}` (optionally with
   `"start": "YYYY-MM-DD"` and `"end": "YYYY-MM-DD"`). The first test of a coin can take several minutes, because the
   bot fetches its prices first; the answer appears when it is done. The report's first line says it is the past, not
   a forecast: say that too. Then tell them, in plain words and with the report's own numbers and dates: the worst
   day, the worst week and the worst month, the three worst weeks, the deepest fall from a high point and how long it
   took to get back (or that it did not), what fees and funding cost, how often it traded, and the same figures for
   simply holding the coins. Lead with the worst stretches: they are what they would have lived through.
6. **Run it on paper only after they say yes**: `{"request": "use-on-paper", "strategy": "my-trend"}`. From the next
   daily run (00:02 UTC) the bot runs it with pretend money, like a built-in. The page and `status` name it.
7. **Real money stays theirs.** A request can never switch on real money; while the bot trades real money,
   `use-on-paper` is refused and the answer says what to type. To run their strategy with real money, they type on
   the server `godspeed-crypto-bot configure --strategy file:<name>` and then `godspeed-crypto-bot go-live`, whose
   sentence names the strategy and its fingerprint. A changed file is a new strategy: real money pauses until they
   type the new sentence.

If a request is answered "Refused", read its sentence to them in plain words; nothing was changed. If no answer comes
after ten minutes, the bot's relay may not be running: say so, and if you can run commands, `godspeed-crypto-bot
status` shows it.

The same in a terminal on the server, if you have one: `godspeed-crypto-bot strategy check <file>`, put the file in
`/etc/godspeed-crypto-bot/strategies/`, `godspeed-crypto-bot backtest file:<name>`, then
`godspeed-crypto-bot configure --strategy file:<name>`.

## What to run for what they say

All commands are `godspeed-crypto-bot <command>` on the server, as root. On the one-line Docker server you cannot run
them yourself: tell the person the command to type there, or use the chat door above for their own strategy.

- **"How is my bot doing?"** `status` (mode, strategy in words, halted or not, recent runs, the account and
  its page address) and, if they want more, the page. Report the facts as they are: value, holdings, the last
  trades, anything halted or waiting. No forecasts. `report` compares the account with the backtest's
  assumptions on the same days.
- **"Stop the bot."** `halt --reason "<their words>"`. It stops new trading and keeps positions. Say so. `resume`
  starts it again.
- **"Sell everything."** Ask once whether they mean closing every position now (it sends real orders in live
  mode). On yes: `flatten --yes --reason "<their words>"`. It tries to close everything and halts. Read its answer and
  tell them the result, including anything it could not close, and that it stays halted until `resume`.
- **"Switch the strategy."** `configure --strategy mix|trend|carry|file:<name>`. `trend` is trend on BTC and ETH,
  `carry` is carry on BTC, ETH and SOL, `mix` is half and half, `file:<name>` is their own strategy file. It takes
  effect at the next daily run as a rebalance. While trading real money a change needs a new `go-live` sentence,
  typed by them. Do not recommend one as better.
- **"Go live."** Follow section 5 of `INSTALL-FOR-ASSISTANTS.md`, in its order: the deposit is theirs (and their
  account address, `configure --master`), `keygen`, they approve the printed address on Hyperliquid, they name the
  most the bot may use (`configure --cap`), then they run `go-live` and type the sentence. Then `preflight`.
- **"The key expires" / "renew the key".** `keygen --network mainnet --replace`, they approve the new address,
  they run `go-live` again, then `preflight`.
- **"Export for my taxes."** `export --start <date> --end <date> --out <file>`.
- **"Install" / "remove".** The install line in `INSTALL-FOR-ASSISTANTS.md`; `uninstall [--purge]`.
- **"Will I hear from it?"** `test-alert` queues one test message for their Godspeed's Telegram; ask whether it
  arrived. If `status` says their Godspeed has no Telegram connected, help them connect it there; messages wait until then.

## When a message from the bot arrives

Messages come only when the person must act or something broke and stayed broken. Read the message, check
`status`, say in plain words what happened and what they decide. Examples: trading halted (a drawdown, a
mismatch between the account and the books, an expired key), a missed daily run, Hyperliquid changed its terms
(read the new clause with them; they decide whether they may still use it; `terms-accept` after that), real money
paused because the strategy changed or the confirmation is from an earlier version (they type the new sentence with
`go-live`).
