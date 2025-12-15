# flet-rss-feed (RSS 閱讀器)

## 執行方式
 
 安裝/同步依賴：
 
 ```bash
 uv sync
 ```
 
 啟動 App：
 
 ```bash
 uv run main.py
 ```
 
## 快取（SQLAlchemy + SQLite 檔案）

App 會把 feed 的 metadata 與文章列表快取到資料庫裡，重新整理時只需要比對差異。
 
 - 預設快取資料庫檔案：`./rss_cache.sqlite3`
 - 可用環境變數指定路徑（建議用絕對路徑）：
 
 ```bash
 RSS_CACHE_DB="/absolute/path/to/rss_cache.sqlite3" uv run main.py
 ```
 
 重新整理文章時，會嘗試使用 HTTP 條件式請求（ETag / Last-Modified）。
如果伺服器回 `304 Not Modified`，就會直接從快取讀取文章。

### ETag / Last-Modified 是做什麼用的？

這兩個都是 HTTP 的快取驗證機制，用來判斷「同一個 URL 的內容有沒有更新」。

- **ETag**
  - 伺服器回應可能會帶 `ETag: "..."`
  - 下次抓取時帶 `If-None-Match: "..."`
  - 若內容沒變，伺服器可回 `304 Not Modified`
- **Last-Modified**
  - 伺服器回應可能會帶 `Last-Modified: ...`
  - 下次抓取時帶 `If-Modified-Since: ...`
  - 若內容沒變，伺服器可回 `304 Not Modified`

注意：要看 RSS 來源站台有沒有支援。若不支援也不影響功能，只是比較省流量/省下載的效果會比較差。

## 抓取與快取更新流程（手動模式）

目前 App 採用「使用者手動抓取」的方式：

1. **切換訂閱列表（左側清單）**
   - 不會打網路
   - 只會從快取資料庫讀取該 feed 的文章並顯示
2. **按下 `更新此訂閱`**
   - 才會打網路去抓該 feed 的 RSS
   - 會把上次存的 `ETag` / `Last-Modified` 帶在 request header（如果有）
   - 伺服器若回 `304 Not Modified`
     - 代表內容沒變
     - 直接從快取資料庫讀取文章並顯示
   - 伺服器若回 `200 OK`
     - 解析 RSS
     - 把新文章寫入快取資料庫（只新增資料庫裡沒有的項目）
     - 再從快取資料庫讀取文章並顯示
   - 若網路錯誤或 HTTP 非 2xx
     - 會改用快取資料庫內容顯示（如果快取裡有）
3. **按下 `更新全部`**
   - 會在背景啟動多執行緒，同時更新所有訂閱的 RSS。
   - 更新完畢後自動重新整理文章列表。

## feed.toml（訂閱清單）

預設讀取 `./feed.toml`。

`feeds` 裡的 `id` 欄位可以省略。若未提供，程式會用 `url` 自動推導一個穩定的 ID（同一個 URL 會得到同一個 ID）。

你也可以用環境變數指定來源（例如 GitHub 的 raw 檔案）：

```bash
 FEED_TOML="https://raw.githubusercontent.com/<owner>/<repo>/<branch>/feed.toml" uv run main.py
 ```
 
## 在 App 內編輯 feed.toml
 
 工具列有 `編輯訂閱源` 按鈕，可直接在 App 內修改 TOML。
 
 - 如果 `FEED_TOML` 是本機檔案：可以 `Validate` / `Save`，存檔後會自動重新載入。
 - 如果 `FEED_TOML` 是遠端 URL：會以唯讀模式開啟（無法直接寫回）。

## 待辦事項

- [ ] 把 feed.toml 更新到 GitHub 的功能
- [x] 更好的介面來更新 feed.toml (已內建編輯器、語法驗證)
- [x] 拉取全部的 feeds (支援背景多執行緒更新)
- [ ] 自動定期更新 feeds (要考慮到 Android 的使用)
- [x] 手機 RWD (支援側邊欄抽屜、SafeArea、適應性佈局)
- [x] 正體中文介面