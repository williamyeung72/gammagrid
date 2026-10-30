# Ticker HTTP API

一份安裝只有一張 `watchlist`。這個 API 改的就是那張表。Sidebar 和 worker 讀到的列，與這裡讀到的列，是同一批主鍵。

## Caller usage

程序聽 `127.0.0.1:8741`。呼叫端帶 `Authorization: Bearer`。Token 放在 header。Query string 不接受 token，因為 request line 會進 log。

建立。`aapl` 會被收成 `AAPL`。第一次寫入回 201。`added_at` 由資料庫填。`pinned` 是 false。接著 `db.get_watchlist` 含有 `AAPL`。`tracked_contracts` 不增加列。

```bash
curl -sS -D - http://127.0.0.1:8741/tickers \
  -H "Authorization: Bearer $GAMMAGRID_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"ticker":"aapl"}'
```

```http
HTTP/1.1 201 Created
Content-Type: application/json
Connection: close

{"ticker":"AAPL","pinned":false,"added_at":"2026-10-30T05:12:03"}
```

同一支 POST 再送一次，回 200，body 仍是那一列。`added_at` 不刷新。`pinned` 維持當下的值。已在清單上的 ticker 不會再問 Yahoo。

Pin，以及 unpin。路徑上的大小寫會收成同一個主鍵。兩次 pin 都是 `pinned: true`。Unpin 是同一個路徑，body 改成 false。缺了這一列時回 404，而且不會順便建立。

```bash
curl -sS -D - -X PATCH http://127.0.0.1:8741/tickers/aapl \
  -H "Authorization: Bearer $GAMMAGRID_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"pinned":true}'
```

```http
HTTP/1.1 200 OK
Content-Type: application/json
Connection: close

{"ticker":"AAPL","pinned":true,"added_at":"2026-10-30T05:12:03"}
```

讀整份清單。順序是 ticker 升冪，和 `db.get_watchlist` 相同。每一列帶自己的 `pinned`。未 pin 的 ticker 仍在陣列裡。

```bash
curl -sS http://127.0.0.1:8741/tickers \
  -H "Authorization: Bearer $GAMMAGRID_API_TOKEN"
```

```http
HTTP/1.1 200 OK
Content-Type: application/json
Connection: close

[{"ticker":"AAPL","pinned":true,"added_at":"2026-10-30T05:12:03"}]
```

刪除只拿掉 `watchlist` 那一列。快照、collection run、`tracked_contracts` 都留著。列已經不在時，再刪一次仍是 204。

```bash
curl -sS -D - -X DELETE http://127.0.0.1:8741/tickers/AAPL \
  -H "Authorization: Bearer $GAMMAGRID_API_TOKEN"
```

```http
HTTP/1.1 204 No Content
Connection: close
```

Token 不對時，不合法的 JSON 也回 401，清單不變。同一個 body 配上對的 token 才回 400。401 的 log 行是 `POST /tickers 401`。呈現出來的 secret 不會出現在那一行。

```bash
curl -sS -D - -X POST http://127.0.0.1:8741/tickers \
  -H "Authorization: Bearer wrong" \
  -H "Content-Type: application/json" \
  -d "not-json"
```

```http
HTTP/1.1 401 Unauthorized
Content-Type: application/json
Connection: close

{"error":"unauthorized"}
```

Sidebar 改走同一個寫入函數。它不持有 API token。

```python
from app.watchlist import Add, Remove, apply, members

result = apply(conn, Add(typed))
if isinstance(result, Rejected):
    st.error(result.message)
else:
    st.rerun()

apply(conn, Remove(ticker))
for member in members(conn):
    label = f"{member.ticker.value} pinned" if member.pinned else member.ticker.value
```

Worker 的呼叫點維持現狀。`app/worker.py` 不改。Closed-market checker 替換的就是 `db.get_watchlist`。

```python
tickers = db.get_watchlist(conn)
```

## Types and signatures

`pinned` 是 `watchlist` 上的非空布林欄位。沒有會員資格就沒有 pin。既有列的預設是 false。遷移不改寫既有的 ticker 文字。

```sql
ALTER TABLE watchlist
    ADD COLUMN pinned BOOLEAN NOT NULL DEFAULT false;
```

`import_sqlite.py` 只插入 `ticker` 欄。新欄的預設值蓋住舊檔，那個腳本不用改。

```python
# app/watchlist.py
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Union

import psycopg


class Ticker:
    """A stored watchlist primary key.

    Constructed from a row or from a symbol that apply has already
    normalized. The constructor rejects an empty string.
    """

    __slots__ = ("value",)

    def __init__(self, value: str) -> None:
        if not value:
            raise ValueError("empty ticker")
        self.value = value


@dataclass(frozen=True)
class Member:
    """One watchlist row. Order of members() is ticker ascending."""

    ticker: Ticker
    pinned: bool
    added_at: dt.datetime


@dataclass(frozen=True)
class Add:
    """Raw text from a caller. apply strips and uppercases it."""

    ticker: str


@dataclass(frozen=True)
class Pin:
    """Set pinned true. The row must already exist."""

    ticker: str


@dataclass(frozen=True)
class Unpin:
    """Set pinned false. The row must already exist."""

    ticker: str


@dataclass(frozen=True)
class Remove:
    """Delete the watchlist row only. History stays."""

    ticker: str


Command = Union[Add, Pin, Unpin, Remove]


@dataclass(frozen=True)
class Stored:
    member: Member
    created: bool


@dataclass(frozen=True)
class Removed:
    ticker: Ticker


@dataclass(frozen=True)
class Rejected:
    """Admission refused. message is the sentence suggestions already builds."""

    ticker: str
    message: str


@dataclass(frozen=True)
class Missing:
    """Pin or unpin named a symbol that is not a row."""

    ticker: Ticker


Applied = Union[Stored, Removed, Rejected, Missing]


def members(conn: psycopg.Connection) -> tuple[Member, ...]:
    """Every watchlist row, ticker ascending.

    No pinned filter and no second sort. db.get_watchlist is the symbol
    projection of this tuple, so the worker cannot drift onto a subset.
    """
    raise NotImplementedError


def apply(conn: psycopg.Connection, command: Command) -> Applied:
    """The only write.

    Add of an existing row returns Stored(created=False) and does not
    refresh added_at or pinned, and does not call the provider.
    Add of a new row rejects a blocking suggestions.propose, and rejects
    underlying_has_options is False. None from that check still inserts.
    Pin and Unpin do not call the provider and do not insert.
    Remove does not call the provider. A second Remove of the same
    normalized symbol returns Removed.
    Snapshots, collection runs, and tracked_contracts are untouched.
    Calls providers.get_provider with no arguments. The API token is not
    a provider token.
    SQL parameters are bound with %s.
    """
    # TODO: symbol = command.ticker.strip().upper()
    # TODO: if symbol is empty, return Rejected(command.ticker, "Ticker is empty.")
    # TODO: Add: select the row. If present, return Stored(created=False).
    # TODO: Add: provider = get_provider(). If propose(..., in_catalogue=catalogue.symbols(conn),
    #            unsupported=provider.unsupported_symbols, provider=provider.name) is blocking,
    #            return Rejected with that message.
    # TODO: Add: has = underlying_has_options(symbol) when the method exists, else None.
    # TODO: Add: if has is False, return Rejected with source_refusal when the symbol is in the
    #            catalogue, otherwise suggestions.refusal. Match dashboard.py's button.
    # TODO: Add: INSERT ... ON CONFLICT DO NOTHING. created is rowcount == 1.
    # TODO: Add: re-read the row and return Stored. A concurrent insert loses the race and
    #            comes back created=False with the winner's added_at and pinned.
    # TODO: Pin: UPDATE watchlist SET pinned = true WHERE ticker = %s RETURNING ...
    # TODO: Unpin: the same statement with false.
    # TODO: Pin or Unpin with no RETURNING row returns Missing. Do not insert.
    # TODO: Remove: DELETE FROM watchlist WHERE ticker = %s, then return Removed.
    raise NotImplementedError
```

`db.get_watchlist` 留在原處，簽名不變。函式本體改成 `members` 的投影。Worker 檔不改，checker 替換這個名字時仍然攔得到。

```python
# app/db.py
def get_watchlist(conn: psycopg.Connection) -> list[str]:
    """Symbols in ticker order. Pin is not a parameter and not a filter."""
    from app.watchlist import members

    return [member.ticker.value for member in members(conn)]
```

`db.add_ticker` 與 `db.remove_ticker` 在同一波刪除。Dashboard、`tests/unit_tests.py`、`tests/smoke_test.py`、`tests/render_views.py` 改呼叫 `apply`。

HTTP 行程的公開函數只有 `serve`。核對成功時 `admit` 做出一個沒有欄位的 `Grant`。Secret 留在區域變數，比完就丟掉。`config` 不新增 token 屬性。

```python
# app/ticker_http.py
from __future__ import annotations

from dataclasses import dataclass


class Grant:
    """Proof that admit accepted the header. Stores nothing.

    Callers cannot construct it. admit builds it with object.__new__.
    """

    def __init__(self) -> None:
        raise RuntimeError("call admit")


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes
    content_type: str


def log_request(method: str, path: str, status: int) -> None:
    """The only log line. There is no header argument and no body argument."""
    raise NotImplementedError


def serve() -> None:
    """Process entry. python -m app.ticker_http.

    Load GAMMAGRID_API_TOKEN first. Unset or blank exits before any
    socket and before any database connection. The exit text does not
    include the value. Then open one connection so migrate.ensure_current
    runs, close it, and listen.
    Default bind is 127.0.0.1 and default port is 8741. Compose sets
    GAMMAGRID_API_BIND=0.0.0.0 and publishes 127.0.0.1:8741:8741.
    Each request uses its own db.get_connection. Connection: close.
    Read the request line and headers. Call admit. On failure, write 401
    and close without parsing the body. A wrong token and a non-JSON body
    is 401. The same body with a good token is 400.
    After a grant, refuse Content-Length above 4096 with 400, then read
    the body, then parse.
    """
    raise NotImplementedError


def _admit(authorization: str | None) -> Grant | None:
    """Bearer scheme, then a SHA-256 digest compared with hmac.compare_digest.

    Missing header, wrong scheme, and wrong secret all return None.
    This function does not log.
    """
    # TODO: expected = the bytes loaded by serve, not config.*
    # TODO: require the prefix "Bearer " and take the remainder as the presented secret.
    # TODO: compare_digest(sha256(presented), sha256(expected)). Equal length digests.
    # TODO: on success, return object.__new__(Grant).
    raise NotImplementedError


def _respond(grant: Grant, method: str, path: str, body: bytes) -> Response:
    """Map one granted request onto members or apply.

    Paths are /tickers and /tickers/{segment}. No trailing slash.
    Decode the segment once. Pass the decoded text through as Command
    ticker text. apply normalizes. Do not build a Ticker here.
    POST /tickers JSON object with only the key ticker, a string.
    PATCH /tickers/{segment} JSON object with only the key pinned, a bool.
    true builds Pin. false builds Unpin. null or a string is 400 and
    apply is not called.
    GET /tickers calls members and returns a JSON array.
    DELETE /tickers/{segment} builds Remove.
    Other methods on a known path are 405. Any other path is 404.
    Stored created True is 201. Stored created False is 200.
    Pin and Unpin Stored are 200. Removed is 204 with an empty body.
    Rejected is 422. Missing is 404 with error missing.
    Error bodies are {"error": "<code>", "message": "<text>"}.
    unauthorized, bad_request, rejected, missing, not_found.
    JSON objects use utf-8. Member keys are ticker, pinned, added_at.
    added_at is datetime.isoformat with sep T and timespec seconds,
    and no offset, because the column is timestamp without time zone.
    """
    # TODO: isinstance(grant, Grant) so a bare object fails closed.
    raise NotImplementedError
```

狀態對照，供呼叫端分支。

- 201，新列，body 是 member。
- 200，既有列，或 pin 更新後的 member。
- 204，刪除，body 為空。列本來就不在，也是 204。
- 401，沒有 grant。Body 是否為 JSON 不影響這個狀態。
- 400，有 grant，JSON 形狀不對，或 body 超過 4096 bytes。
- 404，路徑沒有對應的路由，或 pin 的 ticker 在 `watchlist` 沒有列。`error` 分別是 `not_found` 與 `missing`。
- 405，路徑認得，方法不在上面四個裡面。沒有單獨的 GET 一列。
- 422，`Rejected`。`message` 沿用 `suggestions` 的句子。

## Module map

`app/watchlist.py` 擁有清單的列與 ticker pin。公開的是 `members`、`apply`，以及 `Add`、`Pin`、`Unpin`、`Remove`。SQL 寫在這個檔的私有函數。全表掃描用 `SELECT ticker, pinned, added_at FROM watchlist ORDER BY ticker`。單列寫入用主鍵。行程之間不放快取。

`app/ticker_http.py` 擁有 token、HTTP 與 JSON。公開的是 `serve`。Wire 型別停在 `_respond`。它把 body 收成 `Command` 再呼叫 `apply`。

`app/migrations/0010_watchlist_pin.sql` 是上面的 `ALTER TABLE`。

`app/db.py` 的 `get_watchlist` 改成投影。刪除 `add_ticker` 與 `remove_ticker`。

`app/dashboard.py` 的 Add 與移除改呼叫 `apply`。按鈕上的 `underlying_has_options` 分支刪掉，拒絕文句改顯示 `Rejected.message`。按鍵預覽仍可呼叫 `suggestions.propose`。那一行是給人看的提示。能不能寫入由 `apply` 決定。清單改讀 `members`。`pinned` 為真時，按鈕文字在 ticker 後面加 `pinned`。合約按鈕維持呼叫 `db.add_tracked_contract`。Ticker pin 只寫 `watchlist.pinned`。`st.session_state["selected_ticker"]` 仍是這次瀏覽器選到的分析對象。

`app/worker.py` 不改。`app/day_summary.py` 與 `app/iv_backfill.py` 仍呼叫 `get_watchlist`。`docker-compose.yml` 加一個 `api` 服務，同一張 image，命令是 `python -m app.ticker_http`，環境變數是 `DATABASE_URL` 與 `GAMMAGRID_API_TOKEN`，以及 bind `0.0.0.0`。Host 埠只發佈在 `127.0.0.1:8741`。`requirements.txt` 不加套件。`SECURITY.md` 要補上這一個埠。Dashboard 仍是 localhost 上沒有登入的畫面。Ticker API 另要環境變數裡的 bearer secret，這個 secret 不進資料庫，也不進 repo。

呼叫鏈是兩段。HTTP 從 `serve` 到 `apply`。Sidebar 從按鈕到 `apply`。Worker 從 `get_watchlist` 到 `members`。

## Problem

清單是 `0001_baseline.sql` 的全域表，主鍵 `ticker`，加上 `added_at`。一份資料庫就是一份清單。今天只有 sidebar 會寫它。`add_ticker` 做 `INSERT ... ON CONFLICT DO NOTHING` 並 `upper()`，不 strip。Dashboard 在呼叫前 `strip().upper()`。重複加入看起來像成功，`added_at` 不刷新。`get_watchlist` 是 `ORDER BY ticker`，worker 的 `collect_once`、volume stats、day summary、IV backfill 都靠它。空清單表示這一輪不收集。`remove_ticker` 只刪清單列。

VIX 這類 source refusal，以及 `underlying_has_options is False`，都寫在 `dashboard.py` 的按鈕裡。`None` 仍會加入。Repo 沒有 HTTP，也沒有可當 bearer token 的憑證。`providers.get_provider` 的 `token` 參數目前被忽略。`SECURITY.md` 寫明本地版的 dashboard 故意沒有 auth。合約 pin 在 `tracked_contracts`，鍵是 ticker、expiry、strike、option_type。Ticker pin 是 `watchlist.pinned`。

Worker 檔被 closed-market checker 以 `db.get_watchlist` 這個名字打樁。設計必須讓 worker 繼續呼叫那個函數，否則 checker 描述的路徑會和行程分開。

## Usage (caller's view)

上面的 HTTP 呼叫是規格。型別從那些呼叫推出來。Python 側的呼叫端是 sidebar，它 import `apply`、`members`、`Add`、`Remove`。Worker 繼續呼叫 `db.get_watchlist`，拿回 `list[str]`。

對的 token 加上新 ticker，回來一個 member，隨後 `get_watchlist` 看得到它。再送一次，回來同一個 `added_at`。PATCH `pinned` 回來更新後的 member，`get_watchlist` 的集合不變。GET 的陣列與 `members()` 同序。DELETE 之後符號從 `get_watchlist` 消失，歷史表仍在。錯的 token 加上無法解析的 body，回來 401，表不變。

## Shape

一列就是一個會員。`pinned BOOLEAN NOT NULL DEFAULT false` 把 pin 放在會員列上。Pin 不能在沒有 ticker 的時候存在，也不能寫進 `tracked_contracts`。讀取的記憶體形狀是一次請求裡的 `tuple[Member, ...]`，按 ticker 排序。寫入是主鍵上的單句 SQL。不另建 dict，也不在行程之間做快取。清單很小，三個行程以 Postgres 的列為準。per foundational-thinking。per model-the-domain。

三個行程寫的是同一張表。這是不變條件，所以不拆成每行程一份清單再合併。同時插入撞上主鍵，`ON CONFLICT DO NOTHING` 留下一列。同時把同一個布林寫成同一個值，結果相同。先 pin 再被 sidebar 刪掉，UPDATE 影響零列，API 得到 `Missing`。per separate-before-serializing-shared-state。

`Command` 是 `Add`、`Pin`、`Unpin`、`Remove`。沒有「可有可無的 pinned 欄位」，所以 `null` 進不了 `apply`。輸入的 ticker 是原始字串，正規化只發生在 `apply`。讀出來的 `Ticker` 才是主鍵。per type-system-discipline。

Token 核對在 `serve` 讀 body 之前。失敗一律 401，所以壞 JSON 不會先變成 400。`Grant` 沒有欄位，`apply` 的簽名上也沒有 secret。Log 函數只有 method、path、status。per boundary-discipline。per encode-lessons-in-structure。

`apply` 是唯一的寫入。Strip、upper、blocking proposal、`underlying_has_options is False`、衝突時保留 `added_at` 與 `pinned`，都在這個函數裡面。Sidebar 不再自己決定能不能寫。HTTP 只負責把 JSON 收成 `Command`。per redesign-from-first-principles。per laziness-protocol。

公開的領域函數是 `members` 與 `apply`。公開的網路函數是 `serve`。SQL、Yahoo 回傳的 bool 或 None、digest 比較，以及 `tracked_contracts`，都停在 `apply` 或 `admit`。JSON dict 停在 `ticker_http`。寫入進入 `apply` 一次。讀取進入 `members` 一次。設計不增加 GET 單列、`/pin` 或 list id。那三條路徑重複的是同一組 `Command`。

冪等。Add 兩次收斂到同一列，不刷新 `added_at`。Pin 兩次停在 true。Unpin 兩次停在 false。Remove 兩次都是沒有那一列。當機點只有單句 SQL，沒有「pin 表寫到一半」。per make-operations-idempotent。

`get_watchlist` 是 `members` 的符號投影，所以 worker 的順序和集合跟 API 的列是同一份。投影多一個 `pinned` 參數就會讓 checker 以外的讀者開始篩選。簽名上沒有那個參數。per minimize-reader-load。

同一波改完呼叫端，刪掉 `add_ticker` 與 `remove_ticker`。留下它們，下一個改檔的人會直接呼叫 SQL，跳過 `apply`。per migrate-callers-then-delete-legacy-apis。

測試打 socket。錯 token 加壞 body 必須是 401，而且 `get_watchlist` 不變。好 token 加壞 body 必須是 400。Pin 之後 `tracked_contracts` 的列數不變，`members` 裡該 ticker 的 `pinned` 為真。這些是呼叫端看得到的結果。per test-behavior-not-implementation。

這個設計故意不做的事：不重新命名 ticker，不為 pin 另排一個順序，不讓 pin 變成收集篩選，不在 sidebar 加 pin 按鈕，不加 web framework，不把 token 放進資料庫，不開 CORS，不用 cookie。

## Synthesis decision

Arena 填這一段。這份候選不挑選 base，也不合併另一份候選。

## Tradeoffs accepted

- 接受清單永遠按 ticker 升冪，換來 API、sidebar、`get_watchlist` 只有一種順序。Pin 是列上的記號。
- 接受多個 ticker 同時 pinned，換來不需要名次，也不需要「唯一的那一檔」。
- 接受 sidebar 沒有 pin 按鈕，換來 pin 只有 API 這個寫入端。Sidebar 仍看得到 `Member.pinned`。
- 接受刪除一個已經不在的 ticker 回 204，換來重試會收斂。接受 pin 一個不在的 ticker 回 404，換來 pin 不會變成第二種加入方式。
- 接受本地 sidebar 仍不檢查 API token，換來 dashboard 維持 `SECURITY.md` 裡故意不做的 auth。新的埠才是被 token 擋住的邊界。
- 接受非 blocking 的 proposal 仍然可以加入，換來 API 和 sidebar 共用 `apply` 裡的同一條准入。Blocking 的 source refusal，以及 `underlying_has_options is False`，兩邊都會拒絕。`None` 仍加入。
- 接受更換 token 要重啟 API 行程，換來 secret 不進資料庫，也不成為 `config` 上可以 import 的屬性。
- 接受不加依賴、每請求 `Connection: close`，換來 log 函數沒有 header 參數。標準庫的 access log 不被拿來用。

## Alternatives considered

**把 pin 寫進 `tracked_contracts`。** 那張表的一列是合約，呼叫端得湊出 expiry、strike、option_type 才釘得了一個 ticker。公開介面變成合約寫入，ticker pin 的規則藏不住，還和真正的合約 pin 混在同一個函數。

**另建 `pinned_tickers` 表。** 若讀取端要自己合併兩張表，介面變成 `members` 再加 `pins`。若仍用一個 `members` 把 join 藏起來，介面大小和欄位方案一樣，但另一個寫入端仍可只插入其中一張，pin 就能沒有會員資格。欄位讓那個狀態無法表示。

**FastAPI 路由加上 Pydantic 模型，驗證寫在路由上，sidebar 維持自己的按鈕判斷。** 公開型別就是 JSON。四條路由各自碰 token。Dashboard 留下第二份准入。呼叫端得同時懂 wire 欄位，以及 Yahoo 回傳的 bool 或 None。`serve` 加 `apply` 把那些留在被呼叫的一側。

**PATCH 改 ticker 主鍵。** 快照、collection run、合約表都用字串對上清單，沒有外鍵。重新命名要嘛把歷史留在舊符號上，要嘛改寫每一張表。`remove_ticker` 今天選擇留下歷史。更新的意義是 pin 旗標。

**每個行程一份清單，讀的時候再合併。** 安裝的不變條件是一份清單。合併會把衝突丟回呼叫端。Postgres 的這一張表已經是共享的列。

## Open questions and risks

遠端機器上的呼叫端是否屬於這份安裝的工作。現在 host 只把 8741 發佈在 `127.0.0.1`，和 dashboard 的 8501 相同。若答案是遠端也要呼叫，發佈位址要改，token 檢查仍然留著。

既有資料裡若有未 strip 的 ticker，遷移不會改寫那些主鍵。新的寫入會 strip。要不要另做一次清理，是資料修正，不在這個 API 的第一筆遷移裡。

## Next implementation step

先寫 `app/migrations/0010_watchlist_pin.sql` 和 `app/watchlist.py` 的 `members` 與 `apply`，讓 `db.get_watchlist` 投影過去，再把 dashboard 和測試從 `add_ticker`、`remove_ticker` 移走並刪掉那兩個函數。Socket 等 sidebar 和 worker 已經共用新的列之後再加。
