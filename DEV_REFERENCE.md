# Wolfina Wiki — 開發參考文件

> 本文件記錄系統實際運作的流程、已知問題與修復，供後續開發參考。

---

## 1. 系統架構速覽

```
對話輸入（chat_demo.py）
    │
    ▼
ConversationWindow（累積訊息）
    │ flush 條件觸發（訊息數 / 字元數 / 時間）
    ▼
Flush Pipeline（agent_service.run_flush_pipeline）
    │
    ├── Proposer Agent  → 分析對話，建立 EditProposal（新頁或編輯）
    ├── Reviewer Agent  → 審核 proposals，approve / reject
    ├── Executor Agent  → apply 已 approved 的 proposals
    └── Relation Agent  → 為新/更新的頁面建立知識圖關聯

排程維護（scheduler_service）
    └── Maintenance Pipeline
        ├── Orchestrator → 評估 wiki 狀態，建立 AgentTask
        └── 各 Specialist → 依任務類型執行（research/proposer/reviewer/executor/relation）
```

---

## 2. 已確認的 Bug 與修復

### Bug 1：Ollama tool_call 回傳訊息格式錯誤（400 Bad Request）

**症狀**：Pipeline 跑了，debug 視窗看到 agent 在動，但 wiki 完全沒有任何條目。
查 log 會看到：

```
HTTP/1.1 400 Bad Request
Ollama chat error: Client error '400 Bad Request'
Agent proposer: LLM returned error on iteration 1
```

**根本原因**：`llm_service.py` 的 `run_tool_loop()` 在把 tool_call 記回 `messages` 時，
把 `arguments` 用 `json.dumps()` 轉成字串。Ollama API 期望 `arguments` 是 **dict**，
OpenAI API 才需要字串。這導致第一次 tool_call 成功、第二次呼叫（帶 tool result）就 400。

**修復位置**：`core/services/llm_service.py` — tool call 訊息重建區段
```python
# 修復後：根據 provider 決定 arguments 格式
is_ollama = settings.llm_provider != "openai_compat"
"arguments": tc.arguments if is_ollama else json.dumps(tc.arguments),
```

---

### Bug 2：Reviewer Agent 使用 `"approved"` 而非 `"approve"`

**症狀**：
```
WARNING: Tool review_proposal raised ValueError: 'approved' is not a valid ReviewDecision
```

**根本原因**：`ReviewDecision` 的 enum 值是 `"approve"` / `"reject"`，但 LLM 很自然地用
`"approved"` / `"rejected"`，即使 tool definition 已標注 `enum`。

**修復位置**：`core/tools/handlers.py` — `_review_proposal` 函式
```python
# 正規化常見 LLM 錯誤
raw_decision = inp["decision"].strip().lower()
if raw_decision == "approved":
    raw_decision = "approve"
elif raw_decision == "rejected":
    raw_decision = "reject"
```

---

## 3. 關鍵流程說明

### 3.1 Flush Pipeline 完整流程

```
run_flush_pipeline(conversation_text, batch_id, db)
```

1. **Proposer**（`wiki-proposer`）
   - 收到原始對話文字
   - `search_pages` 確認是否已有相關頁面
   - 無則 `propose_new_page`，有則 `propose_page_edit`
   - 每個 proposal 帶 `batch_id` 供後續追蹤

2. **Reviewer**（`wiki-reviewer`）
   - `list_proposals(batch_id=...)` 取得本批次 pending proposals
   - 可 `get_page` 查閱現有頁面內容
   - `review_proposal(decision="approve"|"reject")`
   - 角色分離：reviewer ≠ proposer

3. **Executor**（`wiki-executor`）
   - `list_proposals(status="approved", batch_id=...)`
   - `apply_proposal(executor_agent_id=...)` 實際寫入頁面
   - 角色分離：executor ≠ proposer ≠ reviewer

4. **Relation**（`wiki-relation`）
   - 為本批次新增/更新的頁面建立知識圖關聯
   - 關聯類型：`parent` / `child` / `related_to` / `references`

---

### 3.2 Proposal 狀態機

```
pending → approved → applied
        ↘ rejected
```

- `min_reviewers`（預設 1）：達到此數量的 approve 才變 `approved`
- 任何一票 `reject` 立即變 `rejected`
- `apply_proposal` 使用 `SELECT ... FOR UPDATE` 避免並發 apply

---

### 3.3 Agent ID 設定

`.env` 或 `settings.py` 中：

| 設定項 | 預設值 | 說明 |
|--------|--------|------|
| `PROPOSER_AGENT_ID` | `wiki-proposer` | |
| `REVIEWER_AGENT_ID` | `wiki-reviewer` | |
| `EXECUTOR_AGENT_ID` | `wiki-executor` | |
| `RELATION_AGENT_ID` | `wiki-relation` | |

這些 ID 用於角色分離檢查，不得相同。

---

## 4. LLM 提供商設定

### Ollama（本機）

```env
LLM_PROVIDER=ollama
OLLAMA_HOST=http://localhost:11434
DEFAULT_MODEL=kimi-k2.5:cloud
```

- 需確認模型支援 tool_call（function calling）
- `kimi-k2.5:cloud` 已驗證可用，但是 cloud 模型會有偶發 502

### OpenAI-compat（OpenRouter / LM Studio）

```env
LLM_PROVIDER=openai_compat
OPENAI_COMPAT_BASE_URL=https://openrouter.ai/api/v1
OPENAI_COMPAT_API_KEY=sk-or-v1-xxx
```

- 切換 provider 後 `arguments` 格式自動調整（已修復）

---

## 5. 手動觸發 Pipeline

### 方法 A：透過 Python 腳本

```python
import asyncio
from core.db.session import AsyncSessionLocal
from core.services.agent_service import run_flush_pipeline

CONVERSATION = """
User: ...
Assistant: ...
"""

async def main():
    async with AsyncSessionLocal() as db:
        await run_flush_pipeline(CONVERSATION, batch_id="manual-001", db=db)

asyncio.run(main())
```

### 方法 B：透過 API（需要先啟動伺服器）

```bash
# 啟動伺服器
uv run uvicorn api.app:app --reload

# 建立對話視窗
curl -X POST http://localhost:8000/conversations/windows \
  -H "Content-Type: application/json" \
  -H "X-Agent-ID: user" \
  -d '{"external_source_id": "my-session"}'

# 新增訊息
curl -X POST http://localhost:8000/conversations/windows/{window_id}/messages \
  -H "Content-Type: application/json" \
  -H "X-Agent-ID: user" \
  -d '{"role": "user", "content": "..."}'

# 手動 flush
curl -X POST http://localhost:8000/conversations/windows/{window_id}/flush \
  -H "X-Agent-ID: user"
```

### 方法 C：chat_demo.py

```bash
uv run python chat_demo.py --persona chatexample/persona.json
# 在對話中輸入 /flush 強制觸發
```

---

## 6. 查看 Wiki 條目

```bash
# 透過 API（需要伺服器執行中）
curl http://localhost:8000/pages/search?q=python -H "X-Agent-ID: user"

# 直接查 SQLite
sqlite3 wolfina.db "SELECT title, slug FROM pages"

# 在瀏覽器查看 Swagger UI
# http://localhost:8000/docs
```

---

## 7. Debug 工具

### 即時事件串流（SSE）

```bash
curl -N http://localhost:8000/debug/stream
```

會即時輸出 agent 動作：

```
event: agent_start        # agent 開始
event: agent_thinking     # 每次 LLM 呼叫
event: agent_tool_call    # 呼叫工具（含參數預覽）
event: agent_tool_result  # 工具回傳（含結果預覽）
event: proposal_created   # proposal 建立
event: proposal_reviewed  # proposal 審核
event: proposal_applied   # proposal 套用
event: agent_done         # agent 結束
```

### 關鍵 Log 訊息

| Log | 意義 |
|-----|------|
| `Flush pipeline start` | pipeline 開始 |
| `Flush pipeline: proposer done` | proposer 完成（不代表 proposal 成功） |
| `400 Bad Request` | tool_call 格式錯誤（見 Bug 1） |
| `'approved' is not a valid ReviewDecision` | 見 Bug 2 |
| `Agent X reached max_iterations` | LLM 迴圈超過 20 次，強制停止 |

---

## 8. 已驗證的 Wiki 條目

截至 2026-04-06，透過手動 pipeline 觸發建立：

- `python-asyncio-basics` — Python asyncio 基礎
- `python-asyncio-gather` — asyncio.gather() 並發執行
- `python-asyncio-event-loop` — Event Loop 概念
- `fastapi` — FastAPI 框架
- `flask` — Flask 框架
- `pydantic` — Pydantic 資料驗證
- `sqlalchemy-async` — SQLAlchemy Async 用法

---

## 9. 後續開發建議

### 近期（可立即執行）

1. **LLM retry 機制**：目前 502/503 等暫時性錯誤直接讓 pipeline 失敗，加入指數退避重試
2. **Proposer 去重改善**：proposer 有時會對同主題建立重複頁面，可在 prompt 中加強搜尋指示
3. **Executor 容錯**：executor 目前若找不到 approved proposal 會靜默通過，可加入警告

### 中期

4. **Maintenance pipeline 測試**：orchestrator → specialist 流程尚未在真實環境驗證
5. **Plugin 開發**：`plugins/example/` 有範例，可依 `BasePlugin` 接入外部資料來源
6. **前端介面**：目前只有 API + Swagger UI，可考慮簡單的 web 前端

### 長期

7. **向量搜尋**：目前 `search_pages` 是 SQLite FTS 或 LIKE，可加入 embedding-based 搜尋
8. **多語言頁面**：schema 尚未考慮 i18n
