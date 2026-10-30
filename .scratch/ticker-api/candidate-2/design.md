# Watchlist command API

## Problem

一份安裝對應 Postgres 裡的一張 `watchlist`。欄位是 `ticker` 與 `added_at`。`db.add_ticker` 做 `upper` 後執行 `INSERT ... ON CONFLICT DO NOTHING`，衝突時不刷新 `added_at`。`db.get_watchlist` 執行 `SELECT ticker FROM watchlist ORDER BY ticker`，回傳 `list[str]`。Sidebar、`worker.collect_once`、`iv_backfill` 與 `day_summary` 都讀這個結果。加入前的拒絕在 `dashboard.py`。`suggestions.propose` 回傳 `blocking` 時 Add 不能按。`underlying_has_options` 回傳 `False` 時不寫入，回傳 `None` 時仍寫入。`remove_ticker` 只刪 `watchlist` 列。`tracked_contracts` 的一列是一個期權合約。這個 repo 的 `requirements.txt` 沒有 HTTP 套件，也沒有 bearer token。`db.get_connection` 使用 `autocommit=True`，需要原子性的寫入要自己開 `conn.transaction()`。Streamlit 在容器內聽 `0.0.0.0:8501`，compose 只把宿主機的 `127.0.0.1:8501` 轉進去。新的 HTTP API 要能建立、讀取、更新、刪除 ticker，並能 pin 或 unpin。Pin 的對象是 ticker 列。Token 要在任何寫入之前檢查，而且不能進日誌。Sidebar 與 worker 要繼續看到同一批列。

## Usage (caller's view)

Process 聽 `8502`。Compose 把宿主機的 `127.0.0.1:8502` 轉進容器。呼叫端在 header 放 `Authorization: Bearer`，後面是安裝自己的 token。Token 來自呼叫端的環境變數 `GAMMAGRID_API_TOKEN`。下面的例子都打 `POST` 或 `GET` `/watchlist`。成功時文件就是當時的整份清單，順序與 `ORDER BY ticker` 相同。

加入 `aapl`。伺服器正規化成 `AAPL` 再寫入。新列的 `pinned` 是 `false`。

```bash
curl -sS -H "Authorization: Bearer $GAMMAGRID_API_TOKEN" \
  -d '{"op":"add","ticker":"aapl"}' \
  http://127.0.0.1:8502/watchlist
```

```json
{"tickers":[{"ticker":"AAPL","pinned":false}]}
```

再送一次同一個 add，文件不變。`added_at` 不刷新，`pinned` 也不改。

讀清單。

```bash
curl -sS -H "Authorization: Bearer $GAMMAGRID_API_TOKEN" \
  http://127.0.0.1:8502/watchlist
```

更新是改代號。`AAPL` 改成 `BRK.B` 後，`pinned` 與 `added_at` 留在同一列。

```bash
curl -sS -H "Authorization: Bearer $GAMMAGRID_API_TOKEN" \
  -d '{"op":"rename","ticker":"AAPL","to":"BRK.B"}' \
  http://127.0.0.1:8502/watchlist
```

Pin 與 unpin 各是一個 op。

```bash
curl -sS -H "Authorization: Bearer $GAMMAGRID_API_TOKEN" \
  -d '{"op":"pin","ticker":"BRK.B"}' \
  http://127.0.0.1:8502/watchlist
```

```json
{"tickers":[{"ticker":"BRK.B","pinned":true}]}
```

```bash
curl -sS -H "Authorization: Bearer $GAMMAGRID_API_TOKEN" \
  -d '{"op":"unpin","ticker":"BRK.B"}' \
  http://127.0.0.1:8502/watchlist
```

刪除。

```bash
curl -sS -H "Authorization: Bearer $GAMMAGRID_API_TOKEN" \
  -d '{"op":"remove","ticker":"BRK.B"}' \
  http://127.0.0.1:8502/watchlist
```

```json
{"tickers":[]}
```

再刪一次仍然是 `200` 與同一份文件。列已經不在。

沒有 token 的刪除得到 `401` 與空 body。`BRK.B` 若還在清單上，列還在。

```bash
curl -sS -o /dev/null -w "%{http_code}" \
  -d '{"op":"remove","ticker":"BRK.B"}' \
  http://127.0.0.1:8502/watchlist
```

`VIX` 在 `YahooProvider.unsupported_symbols`，`propose` 對它回傳 `blocking`。Add 得到 `422`。

```json
{"error":"refused","ticker":"VIX"}
```

目標代號已經在清單上時，rename 得到 `409`。

```json
{"error":"conflict","ticker":"MSFT"}
```

Pin 一個不在清單上的代號得到 `404`。

```json
{"error":"absent","ticker":"ZZZZ"}
```

Sidebar 走同一個 Python 入口，不帶 token。

```python
from app import watchlist

outcome = watchlist.submit(conn, {"op": "add", "ticker": new_ticker})
rows = watchlist.read(conn)
```

`rows` 的順序與 worker 從 `db.get_watchlist` 拿到的代號順序相同。Pinned 列留在字母序的位置，按鈕文字多一個 `PIN` 前綴。

## Shape

一份安裝只有一張 `watchlist`。Pin 是這張表的 `pinned BOOLEAN NOT NULL DEFAULT FALSE`。一列同時帶著成員資格與 pin，所以「釘住了但不在清單上」寫不出來。讀取的主導路徑是整份清單的字母序。`db.get_watchlist` 與 `watchlist.read` 共用下面這條 SQL，worker 取第一欄，API 與 sidebar 取兩欄。

```sql
SELECT ticker, pinned FROM watchlist ORDER BY ticker
```

五個寫入各是一個 op，最後都回到這條讀取。Add 衝突時不改 `pinned` 與 `added_at`。Rename 只改主鍵。Remove 只刪這一列。Pin 與 unpin 只改 `pinned`。

### Types and signatures

`app/watchlist.py` 的公開入口是 `read` 與 `submit`。HTTP 與 sidebar 都呼叫它們。`_Ticker` 與 `_Command` 只在這個模組裡產生。

```python
from __future__ import annotations

import psycopg

class _Ticker:
    """已經 strip 並且 upper 的代號。空字串進不了這個型別。"""

    __slots__ = ("value",)

    def __init__(self, raw: str):
        # TODO: value = raw.strip().upper()。空字串拋 ValueError，解析層把它收成 Malformed。
        raise NotImplementedError

    @classmethod
    def from_stored(cls, value: str) -> _Ticker:
        """資料庫裡的原文。read 用這條。"""
        # TODO: 用 __new__ 填 value。不要走會 strip 的 __init__。
        raise NotImplementedError

    def __repr__(self) -> str:
        return f"_Ticker({self.value})"


class Row:
    """清單上的一列。ticker 已經正規化。pinned 與成員資格在同一列。"""

    __slots__ = ("ticker", "pinned")

    def __init__(self, ticker: _Ticker, pinned: bool):
        self.ticker = ticker
        self.pinned = pinned


class Listed:
    __slots__ = ("rows",)

    def __init__(self, rows: tuple[Row, ...]):
        self.rows = rows


class Refused:
    """Admission 拒絕。列沒有寫入。"""

    __slots__ = ("ticker",)

    def __init__(self, ticker: _Ticker):
        self.ticker = ticker


class Absent:
    """Pin、unpin 或 rename 的來源不在清單上，而且目標狀態也還沒達成。"""

    __slots__ = ("ticker",)

    def __init__(self, ticker: _Ticker):
        self.ticker = ticker


class Conflict:
    """Rename 的目標代號已經是另一列。沒有寫入。"""

    __slots__ = ("ticker",)

    def __init__(self, ticker: _Ticker):
        self.ticker = ticker


class Malformed:
    """五個 op 以外的 payload。欄位不齊或多鍵也進這裡。沒有寫入。"""

    __slots__ = ()


Outcome = Listed | Refused | Absent | Conflict | Malformed


def read(conn: psycopg.Connection) -> tuple[Row, ...]:
    """回傳與 _WATCHLIST_SQL 相同的列。"""
    # TODO: 每個 ticker 用 _Ticker.from_stored，保留資料庫裡的原文。
    raise NotImplementedError


def submit(conn: psycopg.Connection, payload: object) -> Outcome:
    """解析 payload，必要時做 admission，然後寫一列。

    payload 是 usage 裡的 JSON 物件。合法形狀只有這五個。
    {"op":"add","ticker":...}
    {"op":"rename","ticker":...,"to":...}
    {"op":"remove","ticker":...}
    {"op":"pin","ticker":...}
    {"op":"unpin","ticker":...}
    多一個鍵就是 Malformed。Add 不接受 pinned。

    # TODO: 先把 payload 收成 _Command。ticker 與 to 必須是 str。失敗回 Malformed，不碰資料庫。
    # TODO: add 時若代號已在清單，直接 Listed，不跑 admission，不刷新 added_at，不改 pinned。
    # TODO: add 時若代號不在，_admit 拒絕則 Refused，通過則 INSERT ON CONFLICT DO NOTHING。
    # TODO: remove 一律 DELETE，不論列在不在，都回 Listed。
    # TODO: pin 與 unpin 是 UPDATE pinned。rowcount 0 回 Absent，否則 Listed。再送一次停在同一個布林值。
    # TODO: rename 在同一個 conn.transaction() 裡做。
    # TODO: 來源與目標相同，回 Listed，不寫。
    # TODO: 來源在、目標不在，UPDATE ticker。pinned 與 added_at 留在該列。
    # TODO: 兩邊都在，回 Conflict。
    # TODO: 來源不在、目標在，回 Listed。這是已經改完的狀態。
    # TODO: 兩邊都不在，回 Absent。
    # TODO: with conn.transaction() 包住 UPDATE。UniqueViolation 在 with 外面接住，交易先回滾，再回 Conflict。
    # TODO: 成功的寫入最後都回 Listed(read(conn))。read 用 _Ticker.from_stored。
    """
    raise NotImplementedError


def _admit(ticker: _Ticker) -> bool:
    """True 表示可以成為新的一列。

    # TODO: provider = providers.get_provider()
    # TODO: unsupported = getattr(provider, "unsupported_symbols", {}) or {}
    # TODO: proposal = suggestions.propose(ticker.value, unsupported=unsupported, provider=provider.name)
    # TODO: proposal 有值且 proposal.blocking 時回 False。
    # TODO: check = getattr(provider, "underlying_has_options", None)
    # TODO: check 是 None 時回 True。check(ticker.value) is False 時回 False。其餘回 True。
    """
    raise NotImplementedError
```

`app/db.py` 仍是唯一寫 SQL 的地方。新的兩個函式信任呼叫端已經正規化，參數進 SQL，不組字串。

```python
_WATCHLIST_SQL = "SELECT ticker, pinned FROM watchlist ORDER BY ticker"


def get_watchlist(conn: psycopg.Connection) -> list[str]:
    """Worker 的入口。回傳型別維持 list[str]，順序來自 _WATCHLIST_SQL。"""
    # TODO: return [ticker for ticker, _pinned in conn.execute(_WATCHLIST_SQL)]
    raise NotImplementedError


def watchlist_rows(conn: psycopg.Connection) -> list[tuple[str, bool]]:
    """與 get_watchlist 同一條 SQL。第二欄是 pinned。"""
    raise NotImplementedError


def set_watchlist_pinned(conn: psycopg.Connection, ticker: str, pinned: bool) -> int:
    """UPDATE watchlist SET pinned = %s WHERE ticker = %s。回傳 rowcount。"""
    raise NotImplementedError


def rename_watchlist_ticker(conn: psycopg.Connection, source: str, dest: str) -> None:
    """UPDATE watchlist SET ticker = %s WHERE ticker = %s。

    撞上主鍵時拋 psycopg.errors.UniqueViolation。不改 pinned，不改 added_at。
    """
    raise NotImplementedError
```

`app/migrations/0010_watchlist_pinned.sql` 只有一句。Ledger 保證它跑一次。

```sql
ALTER TABLE watchlist ADD COLUMN pinned BOOLEAN NOT NULL DEFAULT FALSE;
```

`app/watchlist_http.py` 是網路邊界。它檢查 token，解 JSON，呼叫 `submit` 或 `read`，再把 `Outcome` 編成狀態碼。

```python
import hashlib
import hmac

_MAX_BODY = 4096


class PresentedToken:
    """Header 的 digest。物件上不留原始 header。"""

    __slots__ = ("digest",)

    def __init__(self, authorization: str | None):
        # TODO: authorization 的型別超出 str 與 None 時，digest 為 None。
        # TODO: 去掉一個不分大小寫的 "Bearer " 前綴。剩下的位元組做 sha256。
        # TODO: 沒有這個前綴，或剩下空字串，digest 為 None。
        # TODO: 這個函式不拋例外，訊息裡也不放 header。
        raise NotImplementedError

    def __repr__(self) -> str:
        return "PresentedToken(redacted)"

    def __str__(self) -> str:
        return "redacted"


class Gate:
    """預期 token 的 sha256。建構之後呼叫端丟棄原始字串。"""

    __slots__ = ("_digest",)

    def __init__(self, digest: bytes):
        self._digest = digest

    @classmethod
    def from_env(cls) -> Gate | None:
        """讀 GAMMAGRID_API_TOKEN，pop 掉環境變數，空白則回 None。"""
        # TODO: raw = os.environ.pop("GAMMAGRID_API_TOKEN", "").strip()
        # TODO: raw 是空的就回 None。否則 Gate(hashlib.sha256(raw.encode()).digest())。
        raise NotImplementedError

    def allow(self, presented: PresentedToken) -> bool:
        """Digest 等長才比較。缺 token 回 False。"""
        # TODO: presented.digest 是 None 時回 False。
        # TODO: return hmac.compare_digest(self._digest, presented.digest)
        raise NotImplementedError

    def __repr__(self) -> str:
        return "Gate(redacted)"


class HttpResult:
    __slots__ = ("status", "body")

    def __init__(self, status: int, body: bytes):
        self.status = status
        self.body = body


def handle(
    gate: Gate,
    method: str,
    path: str,
    authorization: str | None,
    body: bytes,
    conn: psycopg.Connection,
) -> HttpResult:
    """Token 比對是第一個分支。不通過就回 401，不呼叫 submit，也不呼叫 read。

    # TODO: presented = PresentedToken(authorization)
    # TODO: gate.allow(presented) 失敗時回 HttpResult(401, b"")
    # TODO: path 與 /watchlist 不同時回 404 與空 body。
    # TODO: GET 回 200 與 encode(read(conn))。忽略 body。
    # TODO: POST 的 body 長度過 _MAX_BODY 時回 400，不呼叫 submit。
    # TODO: json.loads 失敗時回 400，不呼叫 submit。
    # TODO: 其餘 POST 呼叫 watchlist.submit，再依 Outcome 編成狀態碼。
    # TODO: 其他 method 回 405 與空 body。
    # TODO: 401 的日誌只有 method、path、status。通過之後才加上已解析的 op。不記 header，不記 body，不記 digest。
    """
    raise NotImplementedError


def _status(outcome: Outcome) -> HttpResult:
    """Listed 200，Malformed 400，Absent 404，Conflict 409，Refused 422。"""
    raise NotImplementedError


def encode(rows: tuple[Row, ...]) -> bytes:
    """{"tickers":[{"ticker": value, "pinned": bool}, ...]}。順序與 rows 相同。"""
    raise NotImplementedError


def main() -> None:
    """Token 缺失則 exit 1，訊息是 GAMMAGRID_API_TOKEN is missing。

    # TODO: gate = Gate.from_env()。None 則記那一行並 SystemExit(1)。
    # TODO: bind 只允許 127.0.0.1 與 0.0.0.0，預設 127.0.0.1。port 預設 8502。
    # TODO: 每個請求自己 db.get_connection()，finally 裡 close。
    # TODO: 讀 body 最多 _MAX_BODY + 1 個位元組，然後呼叫 handle。
    """
    raise NotImplementedError
```

狀態碼與 `Outcome` 的對應由 `_status` 單獨決定。

| Outcome | 狀態碼 | Body |
| --- | --- | --- |
| Listed | 200 | `encode` 的清單 |
| Malformed | 400 | `{"error":"malformed"}` |
| Absent | 404 | `{"error":"absent","ticker":...}` |
| Conflict | 409 | `{"error":"conflict","ticker":...}` |
| Refused | 422 | `{"error":"refused","ticker":...}` |
| Token 失敗 | 401 | 空 |
| 路徑不對 | 404 | 空 |
| Method 不對 | 405 | 空 |

第二欄的 ticker 是正規化之後的代號。Conflict 放目標代號。Absent 放來源代號。

### Module map

`app/watchlist.py` 擁有清單的寫入規則。`read` 與 `submit` 是產品入口。`_admit` 只被 `submit` 呼叫。

`app/watchlist_http.py` 擁有 token、HTTP 狀態碼與 JSON。它從 `app.watchlist` 引入 `read`、`submit`、`Row` 與五個 `Outcome` 型別。

`app/db.py` 擁有 SQL。`get_watchlist` 改為共用 `_WATCHLIST_SQL` 的第一欄。`add_ticker` 與 `remove_ticker` 留著，測試仍直接寫列。產品的 Add 與 Remove 改走 `submit`。

`app/migrations/0010_watchlist_pinned.sql` 擁有 `pinned` 欄。

`app/dashboard.py` 的 Add 與 Remove 改呼叫 `submit`。清單改讀 `watchlist.read`，用來畫 `PIN` 前綴。`suggestions.propose` 仍負責按鈕上方的句子。`st.session_state["selected_ticker"]` 維持瀏覽器 session。

`app/worker.py` 繼續呼叫 `db.get_watchlist`。

`docker-compose.yml` 加一個 `api` service。`command` 是 `python -m app.watchlist_http`。`ports` 是 `127.0.0.1:8502:8502`。`GAMMAGRID_API_BIND` 在這個 service 設為 `0.0.0.0`，因為容器內的 loopback 接不到 compose 的 port 轉發。`DATABASE_URL` 與 app service 同一行。`GAMMAGRID_API_TOKEN` 只放在這個 service。App service 與 worker service 的 environment 不加這個變數。

一次 POST 的路徑是 `handle`，然後 `submit`，然後 `db` 的一個語句。三次就到底。

### Load-bearing decisions

資料結構依照 foundational-thinking，先定列再定路徑。依照 model-the-domain，pin 是 `watchlist` 列上的布林值，五個 op 是一組封閉命令。依照 type-system-discipline，`_Ticker` 只代表正規化後的代號，`Outcome` 的五個型別各自帶自己的欄位，`PresentedToken` 與 `Gate` 的字串形式是 `redacted`。

Token 依照 boundary-discipline 留在 `handle` 的第一個分支。`submit` 的參數是 `conn` 與 `payload`。Sidebar 呼叫它時只帶這兩個。JSON 解碼失敗停在 `handle`。代號規則、Yahoo 檢查與 `blocking` 檢查都在 `submit` 與 `_admit`。`handle` 拿到的是已經分好類的 `Outcome`。

Token 的保存依照 encode-lessons-in-structure 寫進型別與 compose。`from_env` 先 `pop` 環境變數，再把 strip 後的字串做成 sha256。`allow` 用 `hmac.compare_digest` 比較兩個 digest。通過檢查之後的日誌是 method、path、status、op。Compose 把 `GAMMAGRID_API_TOKEN` 只放在 `api` service。

重複執行依照 make-operations-idempotent。Add 第二次維持原列。Remove 第二次維持刪除後的清單。Pin 第二次維持 `true`。Unpin 第二次維持 `false`。Rename 在來源已消失且目標已在時回 `200`，因為那就是這個 op 的目標狀態。每個寫入是一個 SQL 語句，放在 `conn.transaction()` 裡。語句失敗則整筆回滾，下一次從表上的列重新判斷。

共享狀態依照 separate-before-serializing-shared-state。一份安裝要一份清單，所以 sidebar、API 與 worker 寫同一張 `watchlist`。每個 HTTP 請求自己開一條連線並在結束時關掉。Postgres 的語句是寫入的序列點。每次讀取都打 `_WATCHLIST_SQL`。

入口依照 laziness-protocol 與 minimize-reader-load 收成兩個 HTTP method、一個路徑、兩個 Python 函式。殼用標準庫的 `http.server`。`get_watchlist` 的回傳型別維持 `list[str]`。

Admission 依照 redesign-from-first-principles 放進 `submit`。新的門與 sidebar 的按鈕用同一個 `_admit`。Dashboard 現有的 `is False` 才拒絕、`None` 仍寫入，成為這個函式的規則。

公開入口藏起的東西是正規化、`_admit`、`ON CONFLICT`、`pinned` 欄、rename 的交易，以及 token 的保存方式。呼叫端要準備的是 header、路徑，以及五個 op 的 JSON。Python 呼叫端還要分支 `Outcome`。再多一個路徑或一個寫入函式，呼叫端就要自己維持規則。

## Synthesis decision

這一份是並行探索裡的一個候選。Arena 比較完兩個候選後才填這一段，寫下哪一份當基底、從另一份取了什麼、以及什麼被退回。這一份不在這裡選自己。

## Tradeoffs accepted

- 接受 rename 的成功重試，與「來源本來就不在、目標已經在」收到同一種 `200`。換來不另做 mutation id。
- 接受有人在 rename 成功後又把舊代號加回去時，重試會看到兩列並回 `409`，兩列都留著。換來不做改名日誌。
- 接受 pin 不改變 `ORDER BY ticker`。換來 sidebar 與 worker 共用同一條 SQL 的順序。
- 接受 `db.add_ticker` 仍可跳過 `_admit`。測試在用它種資料。產品的 Add 改走 `submit`。
- 接受新代號的 add 會在請求裡呼叫 Yahoo，呼叫結束前這個執行緒不返回。換來與 dashboard 同一條放行規則，不在 API 裡做第二份快取。
- 接受成功的 POST 回傳整份清單。換來呼叫端只記一種成功文件。
- 接受路徑錯誤與 `Absent` 都用 `404`。空 body 是路徑，帶 `error` 的 JSON 是缺列。
- 接受容器內綁 `0.0.0.0`。換來 compose 的 `127.0.0.1:8502` 轉發接得到 process。宿主機直接跑模組時，預設綁 `127.0.0.1`。

## Alternatives considered

REST 六個路徑，pin 再占一個子路徑。呼叫端要同時看 path 上的代號與 body 裡的代號，還要知道 pin 是另一個資源。入口變大，藏起來的規則仍是同一組 admission。沒有採用。

另建 `pinned_tickers` 表。成員與 pin 變成兩個寫入點，sidebar 要自己拼回同一列。沒有採用。

用 PUT 送出整份清單。呼叫端每次都要持有完整文件。Sidebar 同時加進一列時，後寫入的文件會蓋掉那一列。合併責任落在呼叫端。沒有採用。

Rename 時連 `option_snapshots`、`collection_runs`、`tracked_contracts` 的 ticker 字串一起改。呼叫端仍只送一個 rename，跨表交易被藏在裡面。那三張表沒有外鍵，`remove_ticker` 今天也不連動。改寫會把兩段收集變成同一檔的歷史。沒有採用。

把 token 放進 `submit` 的參數。Sidebar 所在的 Streamlit process 就必須持有 token。沒有採用。

## Open questions and risks

Rename 要讓 `option_snapshots`、`collection_runs` 與 `tracked_contracts` 留在舊代號嗎？

Pinned 列要維持字母序，還是改到清單最前面，並讓 worker 的收集順序一起改？

`_admit` 裡的 Yahoo 呼叫要設逾時嗎？

風險是 compose 網路裡的其他容器打得到 `api` 的 `8502`。它們的 environment 沒有 token，`handle` 仍會先回 `401`。

## Next implementation step

先加 `app/migrations/0010_watchlist_pinned.sql`，並讓 `db.get_watchlist` 與 `watchlist.read` 共用 `SELECT ticker, pinned FROM watchlist ORDER BY ticker`，用現有測試確認回傳的代號序列與今天相同。
