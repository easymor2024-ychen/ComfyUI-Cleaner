"""ComfyUI日志清理插件"""
from .cleanup_daemon import start_cleanup_daemon

# ComfyUI插件需要的映射（即使不提供节点也需要基本结构）
NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}

# 启动清理守护程序
start_cleanup_daemon()

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]

"""
文件放入ComfyUI/custom_nodes/ComfyUILogCleaner/目录
可通过环境变量配置清理参数：
    CLEANUP_DIRECTORIES：监控目录，逗号分隔
    RETENTION_DAYS：保留时间（支持 s/m/h/d 单位，如 "7d"）
    MAX_FILES_PER_DIR：最大文件数
    MAX_DISK_SIZE_MB：最大磁盘占用 (MB)
    SCAN_INTERVAL：清理扫描间隔 (秒)
    MONITOR_INTERVAL：线程监控间隔 (秒)，默认 300
    CPU_THRESHOLD: 低于多少利用率执行 80%
    STATE_FILE: 清理-扫描记录文件 默认 /root/.comfyui/cleanup_daemon_state.json
启动 ComfyUI 时会自动加载并启动清理程序，无需额外操作
"""