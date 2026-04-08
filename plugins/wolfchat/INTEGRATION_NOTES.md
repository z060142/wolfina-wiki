# Wolf Chat ↔ Wolfina Wiki Integration Notes

## 狀態：部分完成（原型）

---

## 已完成的部分

### Plugin 結構
```
plugins/wolfchat/
  __init__.py        — Python package marker
  config.json        — 可修改的設定檔（bot名稱、快取時間等）
  plugin.py          — BasePlugin 子類別，管理快取與事件匯流排
  router.py          — FastAPI 路由，定義所有 HTTP endpoint
  INTEGRATION_NOTES.md — 本文件
```

### HTTP Endpoints（已可使用）

| Method | Path | 說明 |
|--------|------|------|
| `GET`  | `/wolfchat/user/{username}` | 查詢用戶資料，快取 10 分鐘 |
| `POST` | `/wolfchat/query` | 即時查詢（無快取），供 LLM 自主呼叫 |
| `POST` | `/wolfchat/conversation` | 接收對話紀錄並推入 wiki pipeline |
| `GET`  | `/wolfchat/status` | 診斷資訊（快取狀態、設定快照）|
| `POST` | `/wolfchat/webhook` | 預留，尚未實作（回傳 501）|

### GET /wolfchat/user/{username}
**Wolf Chat 呼叫此 endpoint 取得用戶的 wiki 記憶。**

Response:
```json
{
  "username": "SherefoxUwU",
  "summary": "...(最多500字的用戶摘要)...",
  "sources": ["page-slug-1", "page-slug-2"],
  "cached": true,
  "cache_age_seconds": 42.1
}
```

- 查詢指令已在 `config.json` 的 `query_summary_instruction` 中定義
- 查不到資料時，summary 會明確說 "No data found for this user."
- 快取 TTL 由 `query_cache_ttl_seconds`（預設 600 秒）控制

### POST /wolfchat/query
**供 Wolf Chat 的 LLM 自主呼叫，即時查詢 wiki（不使用快取）。**

適用場景：LLM 在對話中途需要動態查詢特定用戶或主題的最新資料（例如工具呼叫）。

Request body:
```json
{
  "query": "SherefoxUwU",
  "summary_instruction": "（選填）覆蓋 config 中的摘要指令",
  "max_words": 500
}
```

Response:
```json
{
  "query": "SherefoxUwU",
  "summary": "...(即時查詢結果)...",
  "sources": ["page-slug-1"]
}
```

- `query` 不限於用戶名稱，可以是任何主題或問題
- 每次呼叫都會執行完整的 quick_query pipeline，不讀快取也不寫快取
- `summary_instruction` 留空時使用 `config.json` 中的預設指令

---

### POST /wolfchat/conversation
**Wolf Chat 對話結束後呼叫此 endpoint 推送對話紀錄。**

Request body（兩種格式擇一）：

**方式 A — 傳原始文字（raw_log）：**
```json
{
  "raw_log": "[2026-01-30 00:56:13] User (SherefoxUwU): wolf removv my baafff\n[2026-01-30 00:56:13] Bot (Wolfhart) Thoughts: ...\n[2026-01-30 00:56:13] Bot (Wolfhart) Dialogue: ...",
  "session_id": "optional-session-id"
}
```

**方式 B — 傳結構化 turns：**
```json
{
  "turns": [
    {
      "timestamp": "2026-01-30 00:56:13",
      "username": "SherefoxUwU",
      "user_message": "wolf removv my baafff",
      "bot_thoughts": "Fox buddy is back...",
      "bot_dialogue": "Okayyy baafff officially yeeted~"
    }
  ],
  "session_id": "optional-session-id"
}
```

Response:
```json
{
  "window_id": "uuid",
  "messages_added": 6,
  "flush_triggered": true
}
```

### 對話格式轉換規則
| Wolf Chat 原始格式 | Wolfina Wiki 格式 |
|---|---|
| `User (SherefoxUwU): text` | `role: user`, content: `[player: SherefoxUwU] [timestamp]\ntext` |
| `Bot (Wolfhart) Thoughts: text` | 合併到 `role: assistant` 訊息中 |
| `Bot (Wolfhart) Dialogue: text` | 合併到 `role: assistant` 訊息中 |

Assistant 訊息格式：
```
[chatbot: Wolfina Vally] [timestamp]
[Thoughts] ...
[Dialogue] ...
```

---

## 尚未完成（需要在 Wolf Chat 那側完成）

### 1. Wolf Chat 呼叫 User Query API
Wolf Chat 在開始回應用戶前，需要：
```
GET http://{wolfina-wiki-host}:8000/wolfchat/user/{username}
```
將回傳的 `summary` 注入到 bot 的 system prompt 或 context 中。

### 2. Wolf Chat 推送對話紀錄
Wolf Chat 在每段對話結束後，需要：
```
POST http://{wolfina-wiki-host}:8000/wolfchat/conversation
Content-Type: application/json

{ "raw_log": "...", "session_id": "..." }
```

### 3. Bot 名稱 vs Wolfhart
目前 parser 已忽略 bot 的原始名稱（Wolfhart），統一替換為 `config.json` 中的 `bot_display_name`（預設 "Wolfina Vally"）。Wolf Chat 那側不需要做任何調整。

---

## 設定檔（plugins/wolfchat/config.json）

| 欄位 | 預設值 | 說明 |
|------|--------|------|
| `bot_display_name` | `"Wolfina Vally"` | wiki 中顯示的 bot 名稱 |
| `bot_role_label` | `"chatbot"` | 標記 bot 的標籤 |
| `player_role_label` | `"player"` | 標記用戶的標籤 |
| `query_cache_ttl_seconds` | `600` | 用戶查詢快取時間（秒）|
| `query_max_words` | `500` | 查詢摘要的最大字數 |
| `conversation_window_source_id` | `"wolfchat"` | ConversationWindow 的 source 標識 |
| `query_summary_instruction` | 見 config | 傳給 LLM 的摘要指令 |

---

## 架構備注

- **快取**：in-process dict，server 重啟後清空。若需持久化快取，可改用 Redis 或 SQLite。
- **Flush**：依 wiki 自身的 scheduler 節奏決定（message count / char count / 時間門檻），不強制 flush。對話推進 window 後交給系統自己整理。
- **ConversationWindow source_id**：格式為 `wolfchat:{session_id}`（若有提供 session_id），方便追蹤來源。
- **WolfChatPlugin class**：目前未在 plugin registry 中自動載入（Wolfina Wiki 的 plugin 系統需手動註冊）。Router 已直接掛載到 app.py，功能可用。

---

## 測試方式

啟動 server 後：

```bash
# 查詢用戶資料
curl http://localhost:8000/wolfchat/user/SherefoxUwU

# 查看 plugin 狀態
curl http://localhost:8000/wolfchat/status

# 即時查詢（無快取）
curl -X POST http://localhost:8000/wolfchat/query \
  -H "Content-Type: application/json" \
  -d '{"query": "SherefoxUwU", "max_words": 300}'

# 推送對話紀錄
curl -X POST http://localhost:8000/wolfchat/conversation \
  -H "Content-Type: application/json" \
  -d '{
    "raw_log": "[2026-01-30 00:56:13] User (SherefoxUwU): wolf removv my baafff\n[2026-01-30 00:56:13] Bot (Wolfhart) Thoughts: Fox buddy is back.\n[2026-01-30 00:56:13] Bot (Wolfhart) Dialogue: Okayyy yeeted~",
    "session_id": "test-001"
  }'
```
