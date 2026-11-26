# Working in this repository

Read README.md first. Rules for any change:

- `godspeed_crypto_bot/strategy.py` is the arithmetic of the research that chose the strategy, vendored. Do not "improve" it: any change must keep
  `tests/test_parity.py` green (bit-identical targets on all 1,016 hold-out days, equity curve to the cent).
- Exchange specifics live in `godspeed_crypto_bot/venue/` only. The strategy never imports from there.
- Nothing may sign or send outside `mode = "live"` with the live gate passed (`keys.live_problems`).
- Never print, log or commit a key. Never create the live confirmation file.
- Alerts go through `notify.Notifier` (the outbox, told through Godspeed by the relay) only, and only when the owner must act or something stayed broken.
- Names live in `godspeed_crypto_bot/names.py` (technical name, display name "Crypto Bot") and nowhere else.
- `uv run pytest` and `uv run ruff check .` before every push. Regenerate `deploy/requirements.txt` with
  `uv export --locked --no-dev --no-emit-project --output-file deploy/requirements.txt` after a dependency change.
