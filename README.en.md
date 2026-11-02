# Angel Memory — AI Memory and Cognition System

[简体中文](README.md) · [繁體中文](README.zh-TW.md) · **English**

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)

An AstrBot plugin for memory, active knowledge management and note-taking, coordinating a Soul layer, a background DeepMind layer and the main LLM.

## Architecture and features

- **Soul:** four energy dimensions affect recall depth, impression depth, expression desire and creativity through Tanh-based parameter mapping.
- **DeepMind:** background memory retrieval, organization, sleep consolidation and central index maintenance.
- **LLM:** invokes specialized tools when information needs to be saved or retrieved.

| Tool | Role |
| --- | --- |
| `angel_remember` | Store important information persistently |
| `angel_recall` | Recall core memories and related notes |
| `angel_note_read` | Read paginated note text by short ID |
| `angel_note_create` | Create and index structured Markdown notes |

**Retrieval:** BM25 using Tantivy and CJK 1–2-gram search is always available. Embedding retrieval is optional. Without a reranker, vector and BM25 results may be combined by RRF. A configured rerank provider can rerank candidates **even without embeddings**; the original guidance recommends reranking first.

Knowledge files accept Markdown and text, indexed by chunks in `note_chunks.db`. Prefer **short structured entries of about 100 Chinese characters or less** over blindly injecting long documents or entire books into every conversation.

## Installation

**Requires [Angel Heart](https://github.com/kawayiYokami/astrbot_plugin_angel_heart)**, which provides enhanced conversation-record handling.

```bash
cd plugins
git clone https://github.com/kawayiYokami/astrbot_plugin_angel_heart.git
git clone https://github.com/kawayiYokami/astrbot_plugin_angel_memory.git
cd astrbot_plugin_angel_memory
pip install -r requirements.txt
```

Restart AstrBot and configure the LLM providers in WebUI.

## Key configuration

| Setting | Default | Purpose |
| --- | --- | --- |
| `provider_id` | `""` | LLM for memory processing (reasoning-capable recommended) |
| `retrieval.embedding_provider_id` | `""` | Optional embedding provider |
| `retrieval.rerank_provider_id` | `""` | Recommended reranker |
| `conversation_scope_map` | `"{}"` | Scope mapping by Persona name, then UMO; fallback public |
| `memory_behavior.min_message_length` | `5` | Minimum message length for processing |
| `memory_behavior.sleep_interval` | `3600` | Consolidation period, seconds |
| `enable_soul_system.*` | Per dimension | Soul tuning |

`conversation_scope_map` matches the current Persona name before `unified_msg_origin`:

```json
{"女友":"恋爱","aiocqhttp:group:12345":"家人"}
```

## WebUI memory dashboard

Open AstrBot (default `http://localhost:6185`) → Plugin manager → Angel Memory / 天使之魂 → **memory-dashboard**. The dashboard uses AstrBot Plugin Pages and needs **no separate web server**.

It includes system overview, memory paging/filter/deletion, user profiles, tag debugging, vector search, indexed notes, paginated note reading, JSON import/export and maintenance status.

Export generates a downloadable JSON snapshot; import selects one JSON file. The snapshot contains `records`, `global_tags` and `memory_tag_rel` at the top level. Existing memory IDs are skipped **rather than overwritten**.

## Using the LLM tools

- `angel_remember(judgment, reasoning, tags, strength, memory_type)` saves important preferences or facts.
- `angel_recall(limit=5, query="preferences")` retrieves core memories and note summaries.
- `angel_note_read(note_short_id=12, offset=1, limit=200)` reads pages of note text.
- `angel_note_create(title="Python async", content="...")` creates an indexed Markdown note.

Use short, explicit knowledge entries rather than long, unstructured context dumps.

## Tuning and maintenance

Minimum retrieval configuration needs only `provider_id` and BM25. Add `rerank_provider_id` first for improved retrieval; embeddings are optional. The sleep maintenance pipeline syncs the central index and performs **one daily JSON backup**, retaining up to **three copies**.

Example Soul midpoint values: `recall_depth_mid=7`, `impression_depth_mid=3`, `expression_desire_mid=0.5`, `creativity_mid=0.7`. Note options include `note_assistant.enable_recall=true`, `enable_create=true`, `top_k=3`; short-term memory capacity multiplier defaults to `1`.

## Troubleshooting and development

If startup fails, check Angel Heart. For weak retrieval, configure reranking and shorten note entries. If memory processing does not run, verify `provider_id` and `min_message_length`. For Soul anomalies, verify soft energy limits (−20 to +20) and midpoint values. Plugin logs show initialization, search, Soul state and tool calls.

```bash
pip install -r requirements.txt
ruff format .
```

Read [AGENTS.md](AGENTS.md) for development guidance.

## License and credits

Licensed [GPL-3.0](LICENSE). Forked from [kawayiYokami/astrbot_plugin_angel_memory](https://github.com/kawayiYokami/astrbot_plugin_angel_memory). Preserve upstream copyright and license notices; fork changes are not represented as new upstream authorship. Third-party dependencies retain their licenses. Thanks to [AstrBot](https://github.com/Soulter/AstrBot), [Tantivy](https://github.com/quickwit-oss/tantivy) and all contributors.
