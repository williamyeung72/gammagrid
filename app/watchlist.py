"""The only write to a watchlist row.

There is no rename. History tables have no foreign key, so a new symbol
would be collected beside snapshots that stay on the old one.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import psycopg


class Ticker:
    __slots__ = ("value",)

    def __init__(self, value: str) -> None:
        if not value:
            raise ValueError("empty ticker")
        self.value = value


@dataclass(frozen=True)
class Member:
    ticker: Ticker
    pinned: bool
    added_at: dt.datetime


@dataclass(frozen=True)
class Add:
    ticker: str


@dataclass(frozen=True)
class Pin:
    ticker: str


@dataclass(frozen=True)
class Unpin:
    ticker: str


@dataclass(frozen=True)
class Remove:
    ticker: str


Command = Add | Pin | Unpin | Remove


@dataclass(frozen=True)
class Stored:
    member: Member
    created: bool


@dataclass(frozen=True)
class Removed:
    ticker: Ticker


@dataclass(frozen=True)
class Rejected:
    ticker: str
    message: str


@dataclass(frozen=True)
class Missing:
    ticker: Ticker


Applied = Stored | Removed | Rejected | Missing


def members(conn: psycopg.Connection) -> tuple[Member, ...]:
    rows = conn.execute(
        "SELECT ticker, pinned, added_at FROM watchlist ORDER BY ticker"
    ).fetchall()
    return tuple(_member(row) for row in rows)


def apply(conn: psycopg.Connection, command: Command) -> Applied:
    if not isinstance(command, (Add, Pin, Unpin, Remove)):
        raise TypeError("apply takes Add, Pin, Unpin, or Remove")
    symbol = command.ticker.strip().upper()
    if not symbol:
        return Rejected(command.ticker, "Ticker is empty.")
    if isinstance(command, Add):
        return _add(conn, symbol)
    if isinstance(command, Pin):
        return _set_pinned(conn, symbol, True)
    if isinstance(command, Unpin):
        return _set_pinned(conn, symbol, False)
    return _remove(conn, symbol)


def _member(row) -> Member:
    return Member(Ticker(row[0]), bool(row[1]), row[2])


def _one(conn: psycopg.Connection, symbol: str):
    return conn.execute(
        "SELECT ticker, pinned, added_at FROM watchlist WHERE ticker = %s",
        (symbol,),
    ).fetchone()


def _add(conn: psycopg.Connection, symbol: str) -> Applied:
    existing = _one(conn, symbol)
    if existing is not None:
        return Stored(_member(existing), created=False)
    refused = _admit_new(conn, symbol)
    if refused is not None:
        return refused
    created = (
        conn.execute(
            "INSERT INTO watchlist (ticker) VALUES (%s) ON CONFLICT DO NOTHING",
            (symbol,),
        ).rowcount
        == 1
    )
    row = _one(conn, symbol)
    if row is None:
        return Missing(Ticker(symbol))
    return Stored(_member(row), created=created)


def _admit_new(conn: psycopg.Connection, symbol: str) -> Rejected | None:
    from app import catalogue, providers, suggestions

    provider = providers.get_provider()
    unsupported = getattr(provider, "unsupported_symbols", None) or {}
    catalogue_symbols = catalogue.symbols(conn)
    proposal = suggestions.propose(
        symbol,
        in_catalogue=catalogue_symbols.__contains__,
        unsupported=unsupported,
        provider=getattr(provider, "name", "our data source"),
    )
    if proposal is not None and proposal.blocking:
        return Rejected(symbol, proposal.message)
    check = getattr(provider, "underlying_has_options", None)
    has = None if check is None else check(symbol)
    if has is False:
        if symbol in catalogue_symbols:
            message = suggestions.source_refusal(
                symbol, unsupported.get(symbol), provider.name
            )
        else:
            known = [member.ticker.value for member in members(conn)]
            message = suggestions.refusal(symbol, known, catalogue_symbols.__contains__)
        return Rejected(symbol, message)
    return None


def _set_pinned(conn: psycopg.Connection, symbol: str, pinned: bool) -> Applied:
    row = conn.execute(
        """UPDATE watchlist SET pinned = %s WHERE ticker = %s
           RETURNING ticker, pinned, added_at""",
        (pinned, symbol),
    ).fetchone()
    if row is None:
        return Missing(Ticker(symbol))
    return Stored(_member(row), created=False)


def _remove(conn: psycopg.Connection, symbol: str) -> Removed:
    conn.execute("DELETE FROM watchlist WHERE ticker = %s", (symbol,))
    return Removed(Ticker(symbol))
