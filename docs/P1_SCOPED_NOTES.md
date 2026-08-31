# P1：人格分域笔记存储与检索

## 目标

这次修复把笔记从“全局文件注册表 + 全局切片索引 + 全局短 ID”迁移到独立的
scope-aware SQLite 主链路。安全语义固定为：

- `public` 只能读取 `public`；
- 私有人格域（例如 `xiaotai`）可以读取自身与 `public`；
- 私有人格域不能读取其他私有域；
- 新建、修改和删除只允许作用于当前精确 scope；
- 无法确认归属的旧笔记进入 `__quarantine__`，绝不自动归入 `public`。

## 物理目录

新建笔记必须写入 scope 编码目录：

```text
raw/
└── .angel/
    └── scoped/
        └── s-<URL-safe base64(scope)>/
            └── note/
                └── <title>_<timestamp>_<random>.md
```

数据库同时验证“路径中的 scope”与记录的 `memory_scope` 一致。仅修改数据库字段、
伪造相对路径或传入另一个 scope 都不会绕过验证。

## 数据库

新主链路使用：

```text
<index_dir>/scoped_notes/scoped_notes.sqlite3
```

主要约束：

- `note_short_id` 是 scoped repository 内的全局唯一短 ID；
- 文件的稳定身份以物理相对路径为准，不依赖可能在索引重建后重用的旧 `file_id`；
- 切片必须与父文件的短 ID、file ID、scope、路径完全一致；
- 只有 `active` 文件允许拥有切片；
- 搜索 SQL 在加载正文前已经按允许的 scope 过滤，并在返回前再次验证路径编码；
- 当前 P1 不调用外部 reranker，私有候选不会离开本机进程。

若检测到未知数据库 schema 版本或没有版本标识的既有表，系统会拒绝自动写入，
不会将其猜测成当前结构。

## 旧笔记首次扫描

插件首次启动 P1 后会递归扫描 `raw` 中的 `.md` 与 `.txt`：

- 位于规范 scope 编码目录的文件，按目录声明的 scope 建立活动索引；
- 其他旧路径只登记为 `__quarantine__`；
- quarantine 记录不保存标题和正文切片，不参与搜索，也不能通过短 ID 读取；
- 任何文件处理失败时，bootstrap 不会标记完成，下次启动继续重试。

这一步不会移动、删除或自动公开旧文件。后续必须通过单独的人工审查／迁移工具，
把确认属于小白的文件迁入 `public`，把确认属于小太的文件迁入 `xiaotai`。

## 已恢复的入口

P1 只恢复能够从 AstrBot 事件获得可信 scope 的入口：

- 自动笔记召回与注入；
- `angel_note_create`；
- `angel_note_read`；
- `angel_recall` 中的笔记检索；
- 文件扫描器对 scoped 文件的同步与删除。

自动召回使用 `ContextVar` 在每个异步任务中携带 scope，两个并发人格不会共享全局
变量。缺少 scope 时所有读写均 fail closed。

## 仍保持封锁的入口

WebUI Notes API 仍返回 HTTP 403 / `NOTE_SCOPE_REQUIRED`。这些 HTTP 请求目前没有
可信的 AstrBot 人格／会话能力凭据，不能只接受前端自报的 scope。后续只有在加入
服务端签发的 scope capability，并对列表、搜索、读取、修改、删除全部做同等授权后，
才可逐项恢复。

## 部署前后检查

部署前至少备份：

```text
raw/
<index_dir>/
```

首次启动后检查日志中的：

```text
scoped notes 初始扫描完成 scanned=<n> indexed=<n> failed=0
```

不要因为 quarantine 数量较多而把整个旧目录直接移动到 `public`。应先抽样确认内容
归属，再使用后续迁移工具按文件处理。
