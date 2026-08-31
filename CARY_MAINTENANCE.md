# Cary 客製維護基線

本倉庫的 `master` 保持為上游同步線；實際部署與後續客製修復使用 `cary/1.6.1`。

## 版本來源

- 上游：`kawayiYokami/astrbot_plugin_angel_memory`
- 上游基線提交：`8244368eee545adabbc3200c21da5c106cbca3a8`
- 客製版本：`1.6.1+cary.1`
- 正式維護倉庫：`casama233/astrbot_plugin_angel_memory`

部署時應固定到已通過 CI 的不可變 commit SHA 或正式 tag，不要直接追蹤浮動分支。

## 記憶域安全語義

- `public` 只能讀取與修改 `public`。
- 私有人格域（例如 `xiaotai`）可以讀取自身與繼承的 `public`。
- 私有人格域只能修改自身；繼承的 `public` 對它是唯讀。
- 任一私有人格域都不能讀取或修改其他私有人格域。
- 無法可靠判定人格／會話 scope 時一律 fail closed，不會隱式回落到 `public`。
- 缺少 scope 的歷史記憶與舊筆記進入 `__quarantine__`，不得自動公開。
- 舊 Notes Web API 因沒有可信 AstrBot 事件與 scope capability，維持 HTTP 403 封鎖。

建議明確配置：

```json
{
  "小白": "public",
  "小太": "xiaotai"
}
```

只有確定所有未映射會話都應公開時，才可明確加入 `"__default__": "public"`。

## 此基線包含的修復

1. 記憶 scope 解析改為 fail closed，並以人格映射優先於會話映射。
2. FTS／向量候選在正文組裝及外部 rerank 前先按 scope 過濾。
3. 短 ID 的 update／merge 必須通過精確 scope 所有權驗證。
4. 禁止跨 scope 合併與回饋寫入。
5. SimpleMemory 回灌以 `(memory_scope, judgment)` 作為唯一身份。
6. 缺少 scope 的舊向量記憶進入隔離域。
7. 筆記文件、短 ID、切片與搜尋索引使用 scope-aware SQLite 主鏈路。
8. scoped note 路徑與資料庫 scope 雙重驗證，拒絕路徑穿越、scope 偽造及跨域短 ID 讀取。
9. 非同步召回使用 task-local `ContextVar`，避免並發人格互相污染。
10. 筆記讀取強制字元上限，超長單行也不能繞過限制。

## 上線前

完整備份以下資料後再部署：

- 中央記憶 SQLite；
- 向量索引／collection；
- 原始筆記目錄；
- scoped notes 索引目錄；
- 插件配置。

先在測試會話建立帶唯一標記的公共與私有記憶，確認：小太可讀 public，小白不可讀 xiaotai，小太不可修改繼承的 public，未映射人格不會寫入任何正常域。

## 回滾

程式可回滾到上一個已知正常提交，但不要把 `__quarantine__` 批量改回 `public`。資料層回滾只能使用部署前完整備份。
