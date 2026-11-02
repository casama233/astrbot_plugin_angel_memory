# Angel Memory — AI 記憶與認知系統

[简体中文](README.md) · **繁體中文** · [English](README.en.md)

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)

AstrBot 記憶與主動筆記插件。以靈魂層、潛意識層與主意識層協作，讓模型主動保留重要資訊、回憶內容、建立與閱讀筆記。

## 架構與功能

- **Soul 靈魂層**：四維能量控制回憶深度、印象深度、表達慾望、創造力；使用 Tanh 映射參數。
- **DeepMind 潛意識層**：背景記憶檢索、整理、睡眠鞏固及中央索引維護。
- **LLM 主意識層**：按需呼叫四個工具。

| 工具 | 功能 |
| --- | --- |
| `angel_remember` | 永久記錄有價值的重要資訊 |
| `angel_recall` | 檢索核心記憶與相關筆記 |
| `angel_note_read` | 依短 ID 與分頁讀取筆記 |
| `angel_note_create` | 主動整理並儲存 Markdown 筆記 |

**檢索策略：** BM25（Tantivy＋CJK 1–2 gram）預設可用；可選向量嵌入。沒有重排時，向量與 BM25 以 RRF 融合；設定重排 Provider 後可對候選結果重排，**即使沒有向量**也可使用。原作者建議優先設定重排，其提升可能比向量更明顯。

知識庫接受 Markdown／純文字，按段落切分成 `note_chunks.db` 索引。**建議使用 100 字內的結構化短筆記**，避免把整本書或長文直接當作每回合自動注入的 RAG。

## 安裝

**必須先安裝 [Angel Heart](https://github.com/kawayiYokami/astrbot_plugin_angel_heart)**，它負責增強聊天紀錄管理。

```bash
cd plugins
git clone https://github.com/kawayiYokami/astrbot_plugin_angel_heart.git
git clone https://github.com/kawayiYokami/astrbot_plugin_angel_memory.git
cd astrbot_plugin_angel_memory
pip install -r requirements.txt
```

然後重新啟動 AstrBot，並在 WebUI 配置模型提供者。

## 核心設定

| 設定 | 預設 | 用途 |
| --- | --- | --- |
| `provider_id` | `""` | 記憶整理 LLM（建議有推理能力） |
| `retrieval.embedding_provider_id` | `""` | 可選向量模型 |
| `retrieval.rerank_provider_id` | `""` | 建議使用的重排模型 |
| `conversation_scope_map` | `"{}"` | 依人格名優先、會話 ID 其次分類；未命中為 public |
| `memory_behavior.min_message_length` | `5` | 最短處理訊息長度 |
| `memory_behavior.sleep_interval` | `3600` | 記憶鞏固間隔（秒） |
| `enable_soul_system.*` | 各維度預設 | 靈魂參數 |

`conversation_scope_map` 先比對目前 Persona 名，再比對 `unified_msg_origin`：

```json
{"女友":"恋爱","aiocqhttp:group:12345":"家人"}
```

## WebUI 記憶管理

AstrBot 預設 `http://localhost:6185` → 插件管理 → 天使之魂 → **memory-dashboard**。使用 AstrBot Plugin Pages，**不必另外啟動服務**。

包含總覽、記憶瀏覽與刪除、使用者畫像、Tags 調試、向量檢索、筆記索引、筆記分頁讀取、匯入匯出與維護狀態。

匯出可在頁面產生並下載 JSON 快照；匯入選擇單一 JSON 檔。快照頂層包含 `records`、`global_tags`、`memory_tag_rel`；已存在的記憶按 `id` 跳過，**不覆寫原紀錄**。

## 工具使用與筆記

- `angel_remember(judgment, reasoning, tags, strength, memory_type)` 記錄使用者重要偏好。
- `angel_recall(limit=5, query="偏好")` 主動回憶記憶和相關筆記摘要。
- `angel_note_read(note_short_id=12, offset=1, limit=200)` 按行數分頁讀取完整筆記。
- `angel_note_create(title="Python 非同步", content="...")` 新建筆記並建立索引。

盡量以短、明確、可維護的知識條目代替不分段的大篇幅資料，以降低上下文雜訊。

## 調優與維護

檢索最小配置只需要 `provider_id`（BM25）；建議增加 `rerank_provider_id`，向量模型為可選。睡眠維護同步中央索引，**每日 JSON 備份一次，最多保留 3 份**。

靈魂設定示例：`recall_depth_mid=7`、`impression_depth_mid=3`、`expression_desire_mid=0.5`、`creativity_mid=0.7`。筆記增強可設 `note_assistant.enable_recall=true`、`enable_create=true`、`top_k=3`，短期記憶容量倍率預設 `1`。

## 排障與開發

無法啟動先檢查 Angel Heart；檢索品質差可嘗試重排，並確認筆記夠短。記憶沒有處理時，確認 `provider_id` 和 `min_message_length`；靈魂能量異常時核對軟限制（−20 至 +20）及 mid 值。可從插件日誌查看初始化、檢索、狀態與工具呼叫。

```bash
pip install -r requirements.txt
ruff format .
```

開發指南：[AGENTS.md](AGENTS.md)。

## 授權及致謝

專案採 [GPL-3.0](LICENSE)，是 [kawayiYokami/astrbot_plugin_angel_memory](https://github.com/kawayiYokami/astrbot_plugin_angel_memory) 的 fork。保留原作者版權聲明，不把 fork 修改冒認為上游原作；其他依賴保留各自授權。感謝 [AstrBot](https://github.com/Soulter/AstrBot)、[Tantivy](https://github.com/quickwit-oss/tantivy) 和貢獻者。
