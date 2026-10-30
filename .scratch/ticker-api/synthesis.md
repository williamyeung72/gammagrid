# Synthesis

Base is candidate-1. The cross-judge scored it pass on all six checks and scored candidate-2 fail on a second write path and on rename. This read agrees.

## Grafts from candidate-2

Token handling. `Gate.from_env` pops `GAMMAGRID_API_TOKEN` and keeps only a sha256 digest. `__repr__` and `__str__` of the gate and the presented token return `redacted`. Comparison stays `hmac.compare_digest` on the two digests.

Body bound. Read at most 4097 bytes from the socket even when `Content-Length` is missing or smaller than the bytes that follow. Over the limit is 400 and `apply` is not called.

## Rejected from candidate-2

Rename, `Conflict`, and any `UPDATE` of the ticker primary key. History tables have no foreign key. The worker would collect the new symbol while snapshots stay on the old one.

Leaving `db.add_ticker` and `db.remove_ticker` in place. Tests and the sidebar call `apply`.

`submit(conn, payload)` taking a raw dict. Commands stay `Add`, `Pin`, `Unpin`, `Remove`. JSON stops in `app/ticker_http.py`.

## One decision past the sketch

The api process is a compose profile named `api`, not a service in the default `docker compose up`. A missing token exits the process. Putting that process in the default stack would crash-loop installs that never set `GAMMAGRID_API_TOKEN`.

## Contract

Follow `.scratch/ticker-api/candidate-1/design.md` plus the grafts and the profile decision above.
