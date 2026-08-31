# P0.1：未分域 Notes Web API 封锁

## 风险

旧版 `NotesAPI` 不接收 AstrBot 事件、人格名称或会话标识，因此无法调用
`resolve_memory_scope_from_event()`。它仍可通过 WebUI：

- 浏览全局笔记注册表；
- 搜索全局 Tantivy 切片；
- 按短 ID 读取任意已注册笔记；
- 枚举 `raw` 下全部 Markdown/TXT 文件；
- 按相对路径直接读取文件；
- 暴露全局切片统计。

这些入口会绕过 P0 在 `NoteService` 与 LLM 工具层设置的停用保护。

## 临时安全行为

`core/p0_note_api_guard.py` 在插件初始化时把以上六个端点替换为统一的
HTTP 403 响应：

```json
{
  "code": "NOTE_SCOPE_REQUIRED",
  "scope_required": true,
  "retryable": false
}
```

原始可调用对象仅保存在进程内的类属性中，供后续 scoped Web API 迁移使用；
P0.1 不会调用它们，也不会读取、移动或删除任何现有笔记文件。

## 恢复条件

WebUI 笔记功能只能在同时满足下列条件后逐项恢复：

1. 请求携带由 AstrBot 可信会话产生的 scope capability，而不是用户自报 scope；
2. public 只能读取 public，私有 scope 只能读取自身与 public；
3. 新建、修改、删除必须要求精确 scope 所有权；
4. 文件注册表、短 ID、切片 SQLite 与搜索索引都把 scope 纳入主键或过滤；
5. 过滤发生在正文加载和外部 rerank 之前；
6. 未分域旧笔记进入 `__quarantine__`，不得自动归入 public；
7. 有跨域读取、路径遍历、短 ID 越权与旧索引迁移回归测试。
