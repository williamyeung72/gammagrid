# Security Policy

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Email **[hello@gammagrid.io](mailto:hello@gammagrid.io)** with a description
of the issue and steps to reproduce it. We'll acknowledge receipt and follow
up as soon as we can.

## Scope

GammaGrid is a self-hosted dashboard intended to run on `localhost` (the
default `docker-compose.yml` publishes the port on `127.0.0.1` only, not the
local network). The dashboard has no authentication layer, stores no credentials, and
its only outbound calls are to Yahoo Finance (via `yfinance`), Cboe (the public
directory of symbols with listed options, downloaded weekly to fill the
Add-ticker search box — it sends nothing about you or your watchlist), and, in
the UI, an embedded TradingView chart widget. Relevant reports include anything
that would let a snapshot's data, the database, or the host running the
container be compromised — not "the app has no login screen," which is a
known, intentional property of a single-user local tool.

The ticker API listens on `127.0.0.1:8741`. It does not start with a default
`docker compose up`. Start it with the `api` compose profile. Every request
requires `GAMMAGRID_API_TOKEN`. The dashboard on port 8501 stays without a
login.

## Supported versions

Pre-1.0, only the latest released version is supported. There is no formal
LTS branch at this stage.
