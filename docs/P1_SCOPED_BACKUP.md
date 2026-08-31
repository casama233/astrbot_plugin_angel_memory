# P1：按记忆域恢复 SimpleMemory 回灌

P0 为避免跨人格覆盖，暂时停用了向量记忆回灌。本阶段恢复该功能，但幂等键由全局 `judgment` 改为：

```text
(memory_scope, judgment)
```

同一句内容可以同时存在于 `public` 与 `xiaotai`，任何更新或重复清理都只会发生在完全相同的 scope 内。

没有 `memory_scope` 的旧向量记录会进入 `__quarantine__`，不会被猜成公共记忆。隔离记录需要后续审计重分类。

该修复不会重新启用旧笔记；笔记仍需等文件表、切片库与搜索索引全部带 scope 后再恢复。

启动迁移会先把空 scope 标成 `__quarantine__`，再只在同一 scope 内清理旧重复项，最后建立数据库唯一索引：

```sql
UNIQUE (memory_scope, judgment)
```

这使跨 scope 覆盖不仅被业务代码阻止，也被 SQLite 约束阻止。
