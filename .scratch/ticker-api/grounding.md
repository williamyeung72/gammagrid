# Grounding: ticker list and pin

GammaGrid 的 ticker 清單是單一 Postgres 資料庫裡的一張全域表 `watchlist`。成員資格只有加入與刪除，讀出來依 ticker 字母排序。沒有使用者、沒有排序欄位，也沒有 ticker 層級的 pin。今天唯一會改這張表的互動路徑是 Streamlit sidebar。合約 pin 寫在另一張表 `tracked_contracts`，一列是一個期權合約，和 watchlist 沒有外鍵。這個 repo 沒有 HTTP API、沒有登入、也沒有可當 bearer token 的憑證。

## Key concepts

`watchlist` 的欄位是 `ticker TEXT PRIMARY KEY` 與 `added_at TIMESTAMP NOT NULL DEFAULT now()`，定義在 `app/migrations/0001_baseline.sql`。後面的 migration 沒有改過這張表。一個資料庫就是一份清單。快照與收集紀錄用 ticker 字串對上這份清單，沒有外鍵。

唯一持久化的 pin 是 `tracked_contracts`。一列是 `(ticker, expiry, strike, option_type)`，四欄 UNIQUE，`option_type` 只允許 `call` 與 `put`。這不是「這檔 ticker 被釘在清單上」。

Sidebar 與主畫面共用 `st.session_state["selected_ticker"]`。那是瀏覽器 session，不是 pin。

暫停標記來自 `db.unresolvable_tickers`，不是 watchlist 欄位。

`providers.get_provider` 的 `token` 參數被忽略。Yahoo 沒有 token。`SECURITY.md` 寫明本地版沒有 auth layer，而且是故意的。`.streamlit` 的 theme token 是顏色。compose 的資料庫密碼不是 API token。

## Behavior that a design must keep

`add_ticker` 是 `INSERT ... ON CONFLICT DO NOTHING`，再 `upper()`，不 strip。Dashboard 在呼叫前 `strip().upper()`。重複加入回傳 None，畫面上像成功。`added_at` 衝突時不刷新，讀取路徑也不用它。

`get_watchlist` 是 `SELECT ticker FROM watchlist ORDER BY ticker`。呼叫者包括 sidebar、`worker.collect_once`、volume stats、day summary、IV backfill。空清單表示這一輪不收集。

`remove_ticker` 只刪 watchlist 列。快照、collection run、`tracked_contracts` 都留著。

加入前的拒絕在 `dashboard.py`，不在 `db.py`。VIX 這類 `suggestions.propose` 的 source refusal 會停用 Add。Yahoo `underlying_has_options is False` 會拒絕寫入。`None` 仍寫入。

唯一的 web process 是 Streamlit，宿主機 `127.0.0.1:8501`。Postgres 不對宿主機發佈。

## Product request

加一組 HTTP API。呼叫者出示 token。API 可以對 ticker 清單做 create、read、update、delete，並且可以 pin 一個 ticker。Pin 的對象是 ticker，不是期權合約。
