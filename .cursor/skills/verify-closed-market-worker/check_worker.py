"""Exhaustive check of one collector-worker cycle, executed from app.worker.

Runs the real run_forever, collect_once, sleep_seconds, closed_market_slot_taken,
and take_closed_market_slot against a finite environment. The network, the
database, and the maintenance helpers are stubbed. collect_watchlist is not
executed: each cycle picks one of the return shapes that function actually has.

See SKILL.md in this directory for the property, the known finding, and the
bounds. From the repo root:

    python .cursor/skills/verify-closed-market-worker/check_worker.py

The checker loads app/worker.py with the sibling modules stubbed, so it does
not import pandas, psycopg, or the rest of the application.
"""

from __future__ import annotations

import importlib.util
import logging
import subprocess
import sys
import types
from collections import deque
from dataclasses import dataclass
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SYNC_COMMIT = "2d4e6f6900fe182ceb065008242519326add6825"
COVERED = ("app/worker.py",)

# Two market days are enough to see the slot survive a rollover and to see a
# new day get its own slot. 2026-10-23 is a Friday.
DAYS = (date(2026, 10, 23), date(2026, 10, 24))
INTERVALS = (0, 15)
OUTCOMES = ("store", "bare", "raise")
MARKETS = ("open", "closed")

# The one property the current code breaks. Any other name is a new failure.
KNOWN = frozenset({"slot_taken_without_a_stored_snapshot"})

PROPERTIES = {
    "slot_taken_without_a_stored_snapshot": (
        "A closed day spends last_closed_market_collection only after a snapshot was stored."
    ),
    "stored_without_taking_the_slot": (
        "A closed day that stored a snapshot spends last_closed_market_collection."
    ),
    "second_closed_pass": (
        "Once last_closed_market_collection is set for a market day, a later closed cycle "
        "that day does not call collect_once."
    ),
    "collected_while_off": (
        "collector_interval_minutes of 0 does not collect and does not spend the slot."
    ),
    "open_market_skipped": (
        "An open market still collects when the watchlist is non-empty, even if today's "
        "closed-day slot is already set."
    ),
    "maintenance_skipped_after_slot": (
        "After the closed-day slot is spent, catch_up_own_iv, refresh_catalogue_if_due, and "
        "fill_missing_day_summaries still run."
    ),
    "exception_spent_the_slot": (
        "A collect_watchlist exception does not spend last_closed_market_collection."
    ),
    "sleep_not_capped_by_the_interval": (
        "With the provider and the clock in agreement, time.sleep waits the collection interval. "
        "With the interval at 0, it waits IDLE_POLL_SECONDS."
    ),
    "disagreement_did_not_shorten_the_wait": (
        "When the provider says closed and state_from_clock says open, time.sleep waits "
        "DISAGREEMENT_POLL_SECONDS."
    ),
    "cycle_did_not_sleep": "Every cycle reaches time.sleep exactly once.",
}

MAINTENANCE = frozenset({
    "catch_up_own_iv",
    "refresh_catalogue_if_due",
    "fill_missing_day_summaries",
})


class EndCycle(BaseException):
    """Leaves run_forever. BaseException so the worker's except Exception does not swallow it."""

    def __init__(self, reason: str) -> None:
        self.reason = reason


@dataclass(frozen=True)
class World:
    day: int
    slot: str | None


@dataclass(frozen=True)
class Action:
    day: int
    interval: int
    watchlist: bool
    market: str
    outcome: str
    clock: str


@dataclass(frozen=True)
class Arrival:
    previous: World
    lines: tuple[str, ...]


class FakeConn:
    def close(self) -> None:
        return None


class FakeDB:
    """The settings map run_forever actually reads. Mirrors get_collector_interval's contract."""

    def __init__(self) -> None:
        self.settings: dict[str, str] = {}
        self.watchlist: list[str] = []

    def get_connection(self) -> FakeConn:
        return FakeConn()

    def get_collector_interval(self, conn: FakeConn) -> int:
        raw = self.settings.get("collector_interval_minutes")
        minutes = int(raw) if raw is not None else 0
        if minutes <= 0:
            return 0
        # app.db.get_collector_interval floors every positive value at
        # PROVIDER_MIN_INTERVAL_MINUTES. This checker only uses 0 and 15, and 15
        # is that floor, so the floor does not change a result here.
        return minutes

    def get_watchlist(self, conn: FakeConn) -> list[str]:
        return list(self.watchlist)

    def get_setting(self, conn: FakeConn, key: str, default: str | None = None) -> str | None:
        return self.settings.get(key, default)

    def set_setting(self, conn: FakeConn, key: str, value: str) -> None:
        self.settings[key] = str(value)


class Calendar:
    OPEN = "open"
    CLOSED = "closed"

    def __init__(self) -> None:
        self.day = DAYS[0]
        self.provider = "closed"
        self.clock = "closed"

    def state(self, provider: object = None) -> tuple[str, str]:
        return self.provider, "check"

    def market_day(self) -> date:
        return self.day

    def state_from_clock(self, moment: object = None) -> str:
        return self.clock

    def seconds_until_open(self, moment: object = None) -> float:
        # Longer than any interval this checker uses, so a sleep that returns the
        # interval has been capped by sleep_seconds and not by the clock.
        return 8 * 60 * 60


class Providers:
    def get_provider(self) -> object:
        return object()


class Collector:
    def __init__(self) -> None:
        self.outcome = "store"
        self.events: list[str] = []

    def collect_watchlist(self, conn: FakeConn, tickers: list[str], provider: object = None) -> dict[str, str]:
        self.events.append("collect")
        if self.outcome == "raise":
            raise RuntimeError("provider unreachable")
        if self.outcome == "store":
            self.events.append("stored")
            return {ticker: "success" for ticker in tickers}
        # bare: collect_watchlist returned without insert_snapshot. In the real
        # function that is a per-ticker failure, a cooldown, or a closed-market
        # ZeroOpenInterestChain. collect_once ignores the dict and returns len(tickers).
        self.events.append("bare")
        return {ticker: "failed: unreachable" for ticker in tickers}


def _actions(world: World) -> list[Action]:
    days = [world.day]
    if world.day + 1 < len(DAYS):
        days.append(world.day + 1)
    found: list[Action] = []
    for day in days:
        for interval in INTERVALS:
            for watchlist in (False, True):
                for market in MARKETS:
                    for outcome in OUTCOMES:
                        # The agreeing clock first, so the shortest printed failure is the
                        # plain closed day and not the disagreement poll.
                        clocks = (market,) + tuple(item for item in MARKETS if item != market)
                        for clock in clocks:
                            found.append(Action(day, interval, watchlist, market, outcome, clock))
    return found


def _install(worker, db: FakeDB, calendar: Calendar, collector: Collector, called: set[str], sleeps: list[float]) -> None:
    worker.db = db
    worker.market_calendar = calendar
    worker.providers = Providers()
    worker.collector = collector

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        raise EndCycle("sleep")

    worker.time.sleep = sleep
    worker.archive_if_due = lambda conn: 0
    worker.rebuild_stale_volume_stats = lambda conn: None

    def catch_up(conn: FakeConn) -> int:
        called.add("catch_up_own_iv")
        return 0

    def catalogue(conn: FakeConn) -> int:
        called.add("refresh_catalogue_if_due")
        return 0

    def summaries(conn: FakeConn) -> int:
        called.add("fill_missing_day_summaries")
        return 0

    worker.catch_up_own_iv = catch_up
    worker.refresh_catalogue_if_due = catalogue
    worker.fill_missing_day_summaries = summaries


def _describe(
    action: Action,
    before: str | None,
    after: str | None,
    events: list[str],
    called: set[str],
    sleeps: list[float],
) -> tuple[str, ...]:
    today = DAYS[action.day].isoformat()
    lines: list[str] = []
    if action.interval <= 0:
        lines.append(
            "collector_interval_minutes is 0, so run_forever sleeps IDLE_POLL_SECONDS and does not collect."
        )
    else:
        lines.append(f"collector_interval_minutes is {action.interval}.")
        lines.append(
            f"market_calendar.state returns {action.market} for market day {today}. "
            f"state_from_clock returns {action.clock}."
        )
        was = "unset" if before is None else before
        lines.append(f"last_closed_market_collection was {was}.")
        lines.append("The watchlist is SPY." if action.watchlist else "The watchlist is empty.")
        # The slot test in run_forever happens before collect_once looks at the watchlist.
        if action.market == "closed" and before == today and "collect" not in events:
            lines.append(
                "closed_market_slot_taken is true for this market day, so collect_once is not called."
            )
        elif not action.watchlist:
            lines.append("collect_once returns 0, and collect_watchlist is not called.")
        elif action.outcome == "raise":
            lines.append(
                "collect_watchlist raises RuntimeError before storing. "
                "run_forever's except handler sets state to None."
            )
        elif action.outcome == "bare":
            lines.append(
                "collect_watchlist returns without insert_snapshot. "
                "collect_once still returns len(watchlist), which is 1."
            )
        else:
            lines.append("collect_watchlist stores one snapshot. collect_once returns 1.")
        if after != before:
            lines.append(f"take_closed_market_slot writes last_closed_market_collection = {after}.")
        else:
            lines.append("last_closed_market_collection is unchanged.")
        if called == MAINTENANCE:
            lines.append(
                "catch_up_own_iv, refresh_catalogue_if_due, and fill_missing_day_summaries all ran."
            )
        elif called:
            lines.append("Maintenance that ran: " + ", ".join(sorted(called)) + ".")
        else:
            lines.append(
                "catch_up_own_iv, refresh_catalogue_if_due, and fill_missing_day_summaries did not run."
            )
    if sleeps:
        lines.append(f"time.sleep is called with {sleeps[0]}.")
    return tuple(lines)


def _cycle(worker, db: FakeDB, calendar: Calendar, collector: Collector, world: World, action: Action):
    """Run one real cycle. Returns the successor world, the sentences, and any broken properties."""
    key = worker.CLOSED_DAY_KEY
    if world.slot is None:
        db.settings.pop(key, None)
    else:
        db.settings[key] = world.slot
    db.settings["collector_interval_minutes"] = str(action.interval)
    db.watchlist = ["SPY"] if action.watchlist else []
    calendar.day = DAYS[action.day]
    calendar.provider = action.market
    calendar.clock = action.clock
    collector.outcome = action.outcome
    collector.events = []
    called: set[str] = set()
    sleeps: list[float] = []
    _install(worker, db, calendar, collector, called, sleeps)

    reason = "sleep"
    limit = {"n": 0}
    code = worker.run_forever.__code__

    def trace(frame, event, arg):
        if frame.f_code is code and event == "line":
            limit["n"] += 1
            if limit["n"] > 10000:
                raise EndCycle("no-sleep")
        return trace

    sys.settrace(trace)
    try:
        worker.run_forever()
        reason = "returned"
    except EndCycle as stop:
        reason = stop.reason
    finally:
        sys.settrace(None)

    before = world.slot
    after = db.settings.get(key)
    today = DAYS[action.day].isoformat()
    events = collector.events
    raised = action.outcome == "raise" and "collect" in events
    took = before != today and after == today
    broken: list[str] = []

    def fail(name: str) -> None:
        if name not in broken:
            broken.append(name)

    if reason != "sleep" or len(sleeps) != 1:
        fail("cycle_did_not_sleep")
    if took and "stored" not in events:
        fail("slot_taken_without_a_stored_snapshot")
    if (
        action.interval > 0
        and action.market == "closed"
        and "stored" in events
        and before != today
        and after != today
    ):
        fail("stored_without_taking_the_slot")
    if action.interval > 0 and action.market == "closed" and before == today and "collect" in events:
        fail("second_closed_pass")
    if action.interval <= 0 and ("collect" in events or called or after != before):
        fail("collected_while_off")
    if (
        action.interval > 0
        and action.market == "open"
        and action.watchlist
        and action.outcome != "raise"
        and "collect" not in events
    ):
        fail("open_market_skipped")
    if action.interval > 0 and action.market == "closed" and before == today and called != MAINTENANCE:
        fail("maintenance_skipped_after_slot")
    if raised and after != before:
        fail("exception_spent_the_slot")
    if sleeps and action.interval > 0 and not raised:
        expected_cap = action.interval * 60
        if action.market == "closed" and action.clock == "open":
            if sleeps[0] != min(expected_cap, worker.DISAGREEMENT_POLL_SECONDS):
                fail("disagreement_did_not_shorten_the_wait")
        elif sleeps[0] != expected_cap:
            fail("sleep_not_capped_by_the_interval")
    if sleeps and action.interval <= 0 and sleeps[0] != worker.IDLE_POLL_SECONDS:
        fail("sleep_not_capped_by_the_interval")

    successor = World(action.day, after)
    lines = _describe(action, before, after, events, called, sleeps)
    return successor, lines, broken


def _path(parents: dict[World, Arrival | None], world: World, lines: tuple[str, ...]) -> list[tuple[str, ...]]:
    chain: list[tuple[str, ...]] = []
    current: World | None = world
    while current is not None:
        arrival = parents.get(current)
        if arrival is None:
            break
        chain.append(arrival.lines)
        current = arrival.previous
    chain.reverse()
    chain.append(lines)
    return chain


def _warn_if_covered_code_moved() -> None:
    try:
        diff = subprocess.run(
            ["git", "diff", "--quiet", SYNC_COMMIT, "--", *COVERED],
            cwd=REPO,
            check=False,
        )
    except OSError as exc:
        print(f"Could not compare {COVERED[0]} with {SYNC_COMMIT}: {exc}", file=sys.stderr)
        return
    if diff.returncode != 0:
        print(
            f"{COVERED[0]} differs from the sync commit {SYNC_COMMIT}. "
            "Read that diff before treating a pass as current.",
            file=sys.stderr,
        )


def _load_worker():
    """Load the real app/worker.py without importing the rest of the application.

    run_forever only needs the sibling modules as names it calls. Those calls are
    stubbed per cycle. Loading catalogue, collector, or db would pull in pandas
    and psycopg, which this check does not execute.
    """
    app = types.ModuleType("app")
    app.__path__ = [str(REPO / "app")]
    app.__package__ = "app"
    sys.modules["app"] = app
    for name in (
        "catalogue",
        "collector",
        "config",
        "day_summary",
        "db",
        "iv_backfill",
        "market_calendar",
        "providers",
    ):
        module = types.ModuleType(f"app.{name}")
        sys.modules[f"app.{name}"] = module
        setattr(app, name, module)
    path = REPO / "app" / "worker.py"
    spec = importlib.util.spec_from_file_location("app.worker", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["app.worker"] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    logging.disable(logging.CRITICAL)
    worker = _load_worker()

    _warn_if_covered_code_moved()
    db = FakeDB()
    calendar = Calendar()
    collector = Collector()
    start = World(0, None)
    parents: dict[World, Arrival | None] = {start: None}
    queued = {start}
    queue: deque[World] = deque([start])
    seen: set[tuple[World, Action]] = set()
    found: dict[str, list[tuple[str, ...]]] = {}
    cycles = 0

    while queue:
        world = queue.popleft()
        for action in _actions(world):
            if (world, action) in seen:
                continue
            seen.add((world, action))
            successor, lines, broken = _cycle(worker, db, calendar, collector, world, action)
            cycles += 1
            if successor not in queued:
                parents[successor] = Arrival(world, lines)
                queued.add(successor)
                queue.append(successor)
            for name in broken:
                found.setdefault(name, _path(parents, world, lines))

    print(f"Checked {COVERED[0]} against sync commit {SYNC_COMMIT}.")
    print(f"Worlds {len(queued)}, cycles {cycles}.")
    print(
        "Bounds: market days "
        + " and ".join(day.isoformat() for day in DAYS)
        + ", interval 0 or 15, watchlist empty or SPY, "
        "collect_watchlist outcome store, bare, or raise, "
        "provider and clock each open or closed. Day only moves forward."
    )

    unexpected = [name for name in found if name not in KNOWN]
    known = [name for name in found if name in KNOWN]
    if not found:
        print("Every listed property held.")
        return 0

    def show(title: str, names: list[str]) -> None:
        if not names:
            return
        print()
        print(title)
        for name in names:
            print(name)
            print(PROPERTIES[name])
            for index, cycle in enumerate(found[name], start=1):
                print(f"Cycle {index}.")
                for sentence in cycle:
                    print(sentence)

    show("Known finding.", known)
    show("Unexpected.", unexpected)
    if unexpected:
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
