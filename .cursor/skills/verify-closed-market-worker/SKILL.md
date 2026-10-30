---
name: verify-closed-market-worker
description: >-
  Checks the collector worker's closed-market slot by running app.worker.run_forever
  over every environment in a small bound. Use when a change touches run_forever,
  collect_once, sleep_seconds, closed_market_slot_taken, or take_closed_market_slot,
  or when a closed day collects twice or spends its one snapshot without storing one.
---

# Verify the closed-market worker

The collector worker in `app/worker.py` takes one pass on a closed market day, and it spends `last_closed_market_collection` for that day only after a snapshot was stored. A second closed pass the same day does not call `collect_once`. Switching the interval to 0 collects nothing. An open market still collects when that slot is already set. After the slot is spent, `catch_up_own_iv`, `refresh_catalogue_if_due`, and `fill_missing_day_summaries` still run. An exception from `collect_watchlist` does not spend the slot. `time.sleep` waits the collection interval, or `IDLE_POLL_SECONDS` when the interval is 0, and it waits `DISAGREEMENT_POLL_SECONDS` when the provider says closed while `state_from_clock` says open.

A break stores a duplicate of a frozen chain, or it burns the only closed-day attempt on a pass that saved nothing. Duplicate closed-day rows are what OI Delta, the Unusual Activity baseline, and the volatility chart misread. A missed attempt is a closed day with no second chance.

## When a change touches this

Run the check when a diff touches any of these in `app/worker.py`:

- `run_forever`
- `collect_once`
- `sleep_seconds`
- `closed_market_slot_taken`
- `take_closed_market_slot`
- `CLOSED_DAY_KEY`, `IDLE_POLL_SECONDS`, or `DISAGREEMENT_POLL_SECONDS`

A change that only edits `archive_if_due`, `rebuild_stale_volume_stats`, `catch_up_own_iv`, `refresh_catalogue_if_due`, or `fill_missing_day_summaries` does not change this check. Those calls are stubbed. What the check sees is whether `run_forever` calls them.

## Run

From the repo root. The standard library is enough. The checker loads `app/worker.py` and stubs the sibling modules, so it does not install `requirements.txt` and it does not open a database.

```bash
python .cursor/skills/verify-closed-market-worker/check_worker.py
```

Exit 0 means every property held. Exit 1 means the only failure is the known finding below. Exit 2 means some other property failed. Exit 2 is a new bug, or the checker no longer matches the code. Do not treat exit 1 as a pass. The known finding is a real bug in the current code.

If stderr says `app/worker.py` differs from the sync commit, read that diff before trusting the result. Move the sync commit only after you confirm the checker still describes the code, using the rule at the end.

## Known finding

`slot_taken_without_a_stored_snapshot`. The current code breaks it. Do not weaken the property or the checker to make this pass, and do not change `app/worker.py` unless the user asks.

`collect_once` returns `len(tickers)` whenever the watchlist is non-empty. It does not look at what `collect_watchlist` stored. `collect_watchlist` catches a per-ticker failure, returns immediately on a cooldown, and returns after a closed-market `ZeroOpenInterestChain`, all without `insert_snapshot`. `run_forever` then does `if closed and collected: take_closed_market_slot`. The comment above that line says the slot is spent only once something was stored. The condition spends it whenever `collect_once` returns a positive count.

Shortest path the checker prints, one cycle:

`collector_interval_minutes` is 15. `market_calendar.state` returns closed for 2026-10-23, and `state_from_clock` returns closed. `last_closed_market_collection` was unset. The watchlist is SPY. `collect_watchlist` returns without `insert_snapshot`. `collect_once` returns 1. `take_closed_market_slot` writes `last_closed_market_collection` = 2026-10-23. The three maintenance helpers still run. `time.sleep` is called with 900, which is 15 minutes.

The next closed cycle that same market day sees `closed_market_slot_taken` and does not call `collect_once`. Nothing was stored, and that day is not tried again. That follow-up is what `second_closed_pass` guards, and on the current code it holds.

The checker's `bare` outcome is that return shape. It stands for a per-ticker failure, a cooldown, and a closed-market zero-OI refusal. Those three are the same at `run_forever`, because `collect_once` throws the result dict away.

## What a pass covers

The checker runs the real `run_forever` once per environment and cuts the infinite loop at `time.sleep`. One sleep ends the cycle. The next cycle starts from the settings the previous cycle wrote.

Bounds, all of which are enumerated:

- Market days 2026-10-23 and 2026-10-24. The day stays or moves forward one day. It does not go backward.
- `collector_interval_minutes` is 0 or 15. 0 is the product default. 15 is `PROVIDER_MIN_INTERVAL_MINUTES`, so `get_collector_interval`'s floor does not change it.
- Watchlist empty, or exactly SPY.
- `collect_watchlist` either stores one snapshot, returns without storing, or raises `RuntimeError` before storing.
- The provider answer and `state_from_clock` are each open or closed.
- `seconds_until_open` is 8 hours, which is longer than a 15 minute interval, so a sleep of 900 seconds is the interval cap in `sleep_seconds`.

A pass covers those bounds on this model of one worker. It does not cover two workers, a real Postgres, a real provider, holidays, half-days, or an interval other than 0 or 15.

`collect_watchlist` itself is not executed. A change inside `app/collector.py` that stores a row on a path the checker calls `bare`, or that raises where it used to return, will not move this check until the outcome list is updated to match.

`app/db.py`, `app/market_calendar.py`, and the bodies of the maintenance helpers are not executed. `get_collector_interval` in the checker implements the same 0-versus-positive contract as `app.db.get_collector_interval` and nothing else.

## How to read a failure

A property name is a promise, not a suggestion. The sentences under it are one execution of `run_forever`, in order.

It is a bug in the product when the sentences can happen with the real database and the real `collect_watchlist`: the interval was read from `app_settings`, the market day is a real `market_day()`, and the outcome is one of the three return shapes above.

It is a modeling error when a sentence depends on a stub the code does not have. The stubs are: no network, settings stored in a dict, maintenance helpers recorded and not run, `seconds_until_open` fixed at 8 hours, and one `time.sleep` ending the cycle. If the code now sleeps twice in one cycle, or no longer sleeps, `cycle_did_not_sleep` fires. That may be a product change or a stub that no longer matches. Read `run_forever` before editing the checker.

Exit 2 after an intentional change means the checker should change too. Update the outcome list or the property only when the new control flow is what the product should do. Then re-run. A property that was true and is now false is a finding. Do not delete it to get exit 0.

## How to update the verification

The sync commit is `2d4e6f6900fe182ceb065008242519326add6825`. The covered file is `app/worker.py`, functions `run_forever`, `collect_once`, `sleep_seconds`, `closed_market_slot_taken`, and `take_closed_market_slot`.

When `app/worker.py` changes, the checker prints a stderr line naming that commit. Re-read the five functions against this skill. If they still do what the properties say, run the check, then put the new commit hash in this file and in `SYNC_COMMIT` inside `check_worker.py`. Move the hash only after that run. If the known finding is gone because `collect_once` now reports snapshots stored, delete it from `KNOWN` and from the known-finding section, and say so in the hand-off.

After a change that did not re-run this check, use `/maintain-formal-verification-skill`.
