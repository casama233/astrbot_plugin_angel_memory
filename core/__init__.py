"""
核心模块初始化文件

导出核心类供其他模块使用。
"""

from .deepmind import DeepMind
from .config import MemoryConfig, MemoryConstants, MemoryCapacityConfig
from .session_memory import SessionMemoryManager, SessionMemory, MemoryItem
from .initialization_manager import InitializationManager, InitializationState
from .background_initializer import BackgroundInitializer
from .plugin_manager import PluginManager
from .security_guard import install_security_guards
from .p0_vector_scope_guard import install_vector_scope_guards
from .p0_note_api_guard import install_note_api_containment
from .p1_scoped_identity import install_scoped_identity_writes
from .p1_scoped_backup import install_scoped_backup_replay
from .p1_scoped_notes import install_scoped_notes
from .p1_scoped_notes_hardening import install_scoped_notes_hardening

# 安装顺序不可调换：
# 1. P0 中央记忆隔离与旧笔记工具停用；
# 2. P0 向量旧链路补齐 fail-closed 与精确 scope 修改权限；
# 3. P0.1 封锁无法解析人格/会话 scope 的 Notes Web API；
# 4. P1 统一中央库 scope+judgment 身份，并移除写入 API 的隐式 public；
# 5. P1 安全实现替换 P0 的旧备份回灌停用入口；
# 6. P1 scoped notes 只恢复能够建立可信人格/会话 scope 的笔记链路；
# 7. P1 hardening 保证 scope 失败不打断聊天，并封住超长单行读取。
install_security_guards()
install_vector_scope_guards()
install_note_api_containment()
install_scoped_identity_writes()
install_scoped_backup_replay()
install_scoped_notes()
install_scoped_notes_hardening()

__all__ = [
    "DeepMind",
    "MemoryConfig",
    "MemoryConstants",
    "MemoryCapacityConfig",
    "SessionMemoryManager",
    "SessionMemory",
    "MemoryItem",
    "InitializationManager",
    "InitializationState",
    "BackgroundInitializer",
    "PluginManager",
]
