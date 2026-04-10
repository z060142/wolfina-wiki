# Agent 改進任務清單

> 狀態：`[ ]` 待實作 ／ `[x]` 已完成

---

## 高優先

- [x] **修復 Relation Flush 找不到目標頁面的問題**
  - 位置：`core/services/agent_service.py` `run_flush_pipeline()`
  - 做法：在 relation_msg 裡直接附上本批次新建/更新的 page slug/title 清單，不依賴 agent 自己用 batch_id 查

---

## 中優先

- [x] **加入 Tool 失敗 SOP**
  - 位置：`core/services/prompt_blocks.py` `BLOCK_AGENT_TASK_WORKFLOW`
  - 做法：加入規則——Tool 回傳 error 時分析原因；403 Role Violation 立即停止並回報；禁止不改參數重複呼叫失敗的工具

- [x] **Revise loop 加入終止條件**
  - 位置：`core/services/agent_service.py` `_REVIEWER_PROMPT`，`AFTER REJECTION` 區塊
  - 做法：Reviewer 建立 revise_and_resubmit 任務時，從 context_json 讀取 retry_count；超過 2 次改為放棄，標記 failed 並說明原因

- [x] **Proposer 加入記錄過濾標準**
  - 位置：`core/services/agent_service.py` `_PROPOSER_PROMPT`，FLUSH MODE 區塊
  - 做法：加入明確條件——只記錄事實性、非暫時性、非假設性的資訊；閒聊、假設、被否定的說法不記錄

- [x] **Proposer 語氣規範：對話口語 → 百科書面**
  - 位置：`core/services/prompt_blocks.py` `BLOCK_PROPOSAL_GUIDELINES`
  - 做法：加入語氣要求——將對話中的口語轉化為客觀、百科式的書面陳述；保留事實，去除感嘆詞與社交客套

- [x] **Proposer 強化重複頁面的意識**
  - 位置：`core/services/prompt_blocks.py` `BLOCK_PROPOSAL_GUIDELINES`
  - 做法：改用「同步優先於創建」框架，強調重複建立相同主題的頁面是系統污染

- [x] **Reviewer 加入多維度檢核清單**
  - 位置：`core/services/agent_service.py` `_REVIEWER_PROMPT`，REVIEW DECISION CRITERIA 區塊
  - 做法：將模糊標準替換為具體檢查項目——內容衝突、Markdown 格式、Slug 合規、是否遺漏應有的關聯

- [x] **Reviewer 加入充分性評判**
  - 位置：同上
  - 做法：加入「內容是否充分」為審核維度，避免 stub 頁面通過審核

---

## 低優先

- [x] **Orchestrator 任務過期意識**
  - 位置：`core/services/agent_service.py` `_ORCHESTRATOR_PROMPT`
  - 做法：加入指引——若同類任務已有 3+ 筆積壓，不繼續堆疊，改在 instruction 中說明情況
  - 備註：prompt 層表達有限，可能最終需要工具層支援

---

## 待追加

_（保留給後續新增項目）_
