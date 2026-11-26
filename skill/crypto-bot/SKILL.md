---
name: crypto-bot
description: Crypto Bot, the person's own trading bot on Hyperliquid. Use when they ask how their bot is doing ("how is my crypto bot doing", "what does the bot hold", "did it trade today"), want it stopped or started again ("stop the bot", "pause trading", "resume"), want everything sold ("sell everything", "close all positions"), want a different strategy ("switch the strategy to carry", "trend only"), want real money switched on ("go live", "trade with real money"), ask about the exchange key ("the bot's key expires", "renew the key"), want the tax export, or want Crypto Bot installed or removed.
---

## What this is

Crypto Bot (`godspeed-crypto-bot`) is an add-on on the person's Godspeed server. It runs one of three built-in
strategies on Hyperliquid every day at 00:02 UTC and checks the account every five minutes. It starts in paper
mode (simulated money on real prices) and trades real money only after the person has typed a confirmation
sentence themselves. Its page (behind their Godspeed sign-in) shows the account, every trade, funding and fees
and the safety checks; `godspeed-crypto-bot status` prints the page's address.

Full set-up steps, in order, are in `INSTALL-FOR-ASSISTANTS.md` in the add-on's folder
(`/opt/godspeed-crypto-bot/`). Follow them for an installation or for going live.

## Never

- **Never type or paste the confirmation sentence for the person.** Real money is switched on only by the
  person, in their own terminal, with `godspeed-crypto-bot go-live`. If they ask you to "just do it", tell
  them the command and that it has to be their own typing. Do not fake a terminal, do not write
  `LIVE-CONFIRMED.*` yourself, do not edit `mode = "live"` into the configuration.
- **Never promise, forecast or hint at a return**, and never say a strategy is "the best" or "safe". Crypto
  Bot can lose everything put into it. When they talk about going live, say that once, plainly.
- Never print, copy or send the key. `keygen` prints only an address.
- Never help someone who says they live in the United States or Ontario, or are a resident or citizen of a
  sanctioned country, to get round Hyperliquid's terms of use.

## What to run for what they say

All commands are `godspeed-crypto-bot <command>` on the server, as root.

- **"How is my bot doing?"** `status` (mode, strategy in words, halted or not, recent runs, the account and
  its page address) and, if they want more, the page. Report the facts as they are: value, holdings, the last
  trades, anything halted or waiting. No forecasts. `report` compares the account with the backtest's
  assumptions on the same days.
- **"Stop the bot."** `halt --reason "<their words>"`. It stops new trading and keeps positions. Say so. `resume`
  starts it again.
- **"Sell everything."** Ask once whether they mean closing every position now (it sends real orders in live
  mode). On yes: `flatten --yes --reason "<their words>"`. It tries to close everything and halts. Read its answer and tell them the result, including anything it could
  not close, and that it stays halted until `resume`.
- **"Switch the strategy."** `configure --strategy mix|trend|carry`. `trend` is trend on BTC and ETH, `carry` is
  carry on BTC, ETH and SOL, `mix` is half and half. It takes effect at the next daily run as a rebalance. Do
  not recommend one as better.
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
(read the new clause with them; they decide whether they may still use it; `terms-accept` after that).
