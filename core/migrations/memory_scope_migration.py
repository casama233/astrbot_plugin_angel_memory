"""记忆 scope 字段迁移。

历史记录缺少 ``memory_scope`` 时无法可靠判断其人格归属，因此只能放入
隔离域；绝不能自动公开。隔离记录不会被正常 public/private 检索命中，
后续应由人工审计或专门迁移工具重新归类。
"""

import asyncio


QUARANTINE_SCOPE = "__quarantine__"


class MemoryScopeMigration:
    """将缺失 memory_scope 的历史向量记录移入隔离域。"""

    def __init__(self, logger):
        self.logger = logger

    async def migrate_missing_memory_scope(
        self,
        collection,
        batch_size: int = 300,
        sleep_seconds: float = 0.05,
    ) -> None:
        if collection is None:
            self.logger.warning("memory_scope 迁移跳过：collection 不可用。")
            return

        scanned = 0
        quarantined = 0
        offset = 0
        failed_update_batches = []

        while True:
            results = collection.get(
                limit=batch_size,
                offset=offset,
                include=["metadatas"],
            )
            ids = results.get("ids", []) if results else []
            metadatas = results.get("metadatas", []) if results else []

            if not ids:
                break

            scanned += len(ids)
            to_update_ids = []
            to_update_metas = []

            for idx, mem_id in enumerate(ids):
                try:
                    meta = (
                        metadatas[idx]
                        if idx < len(metadatas) and metadatas[idx]
                        else {}
                    )
                    scope = (
                        str(meta.get("memory_scope", "")).strip()
                        if isinstance(meta, dict)
                        else ""
                    )
                    if scope:
                        continue

                    if not isinstance(meta, dict):
                        meta = {}
                    new_meta = dict(meta)
                    new_meta["memory_scope"] = QUARANTINE_SCOPE
                    to_update_ids.append(mem_id)
                    to_update_metas.append(new_meta)
                except Exception as exc:
                    self.logger.warning(
                        "memory_scope 迁移跳过异常记录 id=%s: %s",
                        mem_id,
                        exc,
                    )

            if to_update_ids:
                try:
                    collection.update(ids=to_update_ids, metadatas=to_update_metas)
                    quarantined += len(to_update_ids)
                except Exception as exc:
                    self.logger.error(
                        "memory_scope 迁移批次隔离失败: %s; ids=%s",
                        exc,
                        to_update_ids,
                    )
                    failed_update_batches.append((to_update_ids, to_update_metas))

            self.logger.info(
                "[memory_scope迁移] 已扫描=%s 已隔离=%s 当前批次=%s",
                scanned,
                quarantined,
                len(ids),
            )

            offset += len(ids)
            await asyncio.sleep(sleep_seconds)

        failed_update_ids = []
        for ids_batch, metas_batch in failed_update_batches:
            try:
                collection.update(ids=ids_batch, metadatas=metas_batch)
                quarantined += len(ids_batch)
                self.logger.info(
                    "[memory_scope迁移] 重试成功，隔离=%s",
                    len(ids_batch),
                )
            except Exception as retry_error:
                failed_update_ids.extend(ids_batch)
                self.logger.error(
                    "memory_scope 迁移重试失败: %s; ids=%s",
                    retry_error,
                    ids_batch,
                )

        self.logger.info(
            "[memory_scope迁移] 完成，总扫描=%s，总隔离=%s，隔离域=%s",
            scanned,
            quarantined,
            QUARANTINE_SCOPE,
        )
        if failed_update_ids:
            self.logger.error(
                "[memory_scope迁移] 仍有未隔离记录，失败ID数量=%s，ids=%s",
                len(failed_update_ids),
                failed_update_ids,
            )
