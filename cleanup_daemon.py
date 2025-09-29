#!/usr/bin/env python3
"""
日志清理守护程序（线程安全版）
功能：按时间、文件数、磁盘大小清理日志，独立线程运行，含心跳检测
"""

import os
import sys
import time
import logging
import signal
import psutil
from pathlib import Path
from typing import List, Dict, Tuple, Optional
from dataclasses import dataclass
import json
from datetime import datetime, timedelta
import threading
from threading import Thread

# 环境变量默认配置
DEFAULT_CONFIG = {
    "CLEANUP_DIRECTORIES": "/var/log",
    "RETENTION_DAYS": "3",
    "MAX_FILES_PER_DIR": "10000",
    "MAX_DISK_SIZE_MB": "10240",
    "SCAN_INTERVAL": "300",  # 清理扫描间隔（秒）
    "HEARTBEAT_INTERVAL": "300",  # 心跳检测间隔（秒，默认5分钟）
    "CPU_THRESHOLD": "80.0",
    "STATE_FILE": "/tmp/cleanup_daemon_state.json",
    "HEARTBEAT_TIMEOUT": "600"  # 心跳超时时间（秒，默认10分钟）
}


@dataclass
class FileInfo:
    """文件信息类"""
    path: str
    size: int
    mtime: float
    relative_path: str


@dataclass
class CleanupConfig:
    """清理配置类"""
    directories: List[str]
    retention_seconds: int
    max_files_per_dir: int
    max_disk_size_bytes: int
    scan_interval: int
    heartbeat_interval: int
    cpu_threshold: float
    state_file: str
    heartbeat_timeout: int


class LogCleanupDaemon:
    def __init__(self):
        self._setup_logging()
        self.config = self._load_config()
        self.file_registry: Dict[str, List[FileInfo]] = {}
        self.running = True
        self.state_lock = threading.Lock()
        self.heartbeat_lock = threading.Lock()
        self.last_heartbeat = time.time()  # 心跳时间戳
        self.cleanup_thread: Optional[Thread] = None
        self.heartbeat_thread: Optional[Thread] = None
        self._load_initial_state()

    def _setup_logging(self):
        """设置日志"""
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            handlers=[
                logging.StreamHandler(sys.stdout),
                logging.FileHandler('/var/log/cleanup-daemon.log')
            ]
        )
        self.logger = logging.getLogger("ComfyUILogCleaner")

    def _parse_retention_time(self, time_str: str) -> int:
        """解析保留时间字符串为秒"""
        units = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}
        time_str = str(time_str).strip().lower()

        if not time_str:
            self.logger.warning("保留时间为空，使用默认3天")
            return 3 * 86400

        try:
            num_str = []
            unit = 'd'
            for c in time_str:
                if c.isdigit() or c == '.':
                    num_str.append(c)
                else:
                    if c in units:
                        unit = c
                    break

            if not num_str:
                raise ValueError("未找到有效数字")

            return int(float(''.join(num_str)) * units[unit])
        except Exception as e:
            self.logger.warning(f"解析保留时间失败: {e}，使用默认3天")
            return 3 * 86400

    def _format_seconds(self, seconds: int) -> str:
        """将秒数转换为易读格式"""
        if seconds >= 86400:
            return f"{seconds // 86400}天"
        elif seconds >= 3600:
            return f"{seconds // 3600}小时"
        elif seconds >= 60:
            return f"{seconds // 60}分钟"
        else:
            return f"{seconds}秒"

    def _load_config(self) -> CleanupConfig:
        """从环境变量加载配置"""

        # 读取环境变量，无配置时使用默认值
        def get_env(key: str) -> str:
            return os.getenv(key, DEFAULT_CONFIG[key])

        return CleanupConfig(
            directories=get_env("CLEANUP_DIRECTORIES").split(','),
            retention_seconds=self._parse_retention_time(get_env("RETENTION_DAYS")),
            max_files_per_dir=int(get_env("MAX_FILES_PER_DIR")),
            max_disk_size_bytes=int(get_env("MAX_DISK_SIZE_MB")) * 1024 * 1024,
            scan_interval=int(get_env("SCAN_INTERVAL")),
            heartbeat_interval=int(get_env("HEARTBEAT_INTERVAL")),
            cpu_threshold=float(get_env("CPU_THRESHOLD")),
            state_file=get_env("STATE_FILE"),
            heartbeat_timeout=int(get_env("HEARTBEAT_TIMEOUT"))
        )

    def _load_initial_state(self):
        """加载初始文件状态"""
        if os.path.exists(self.config.state_file):
            try:
                with open(self.config.state_file, 'r') as f:
                    state_data = json.load(f)

                with self.state_lock:
                    for dir_path, files_data in state_data.items():
                        self.file_registry[dir_path] = [
                            FileInfo(
                                path=fd['path'],
                                size=fd['size'],
                                mtime=fd['mtime'],
                                relative_path=fd['relative_path']
                            ) for fd in files_data
                        ]
                self.logger.info(f"已从 {self.config.state_file} 加载状态数据")
            except Exception as e:
                self.logger.warning(f"加载状态文件失败: {e}")

        # 初始扫描所有目录
        for directory in self.config.directories:
            self._scan_directory(directory)
        self._save_state()

    def _scan_directory(self, directory: str):
        """扫描目录并更新文件注册表"""
        dir_path = Path(directory)
        if not dir_path.exists():
            self.logger.warning(f"目录不存在: {directory}")
            return

        current_files = {}
        try:
            for file_path in dir_path.rglob('*'):
                if file_path.is_file():
                    try:
                        stat = file_path.stat()
                        relative_path = str(file_path.relative_to(dir_path))
                        current_files[str(file_path)] = FileInfo(
                            path=str(file_path),
                            size=stat.st_size,
                            mtime=stat.st_mtime,
                            relative_path=relative_path
                        )
                    except (OSError, ValueError) as e:
                        self.logger.debug(f"无法访问文件 {file_path}: {e}")
        except Exception as e:
            self.logger.error(f"扫描目录 {directory} 失败: {e}")
            return

        with self.state_lock:
            self.file_registry[directory] = list(current_files.values())

    def _is_cpu_busy(self) -> bool:
        """检查CPU是否忙碌"""
        try:
            return psutil.cpu_percent(interval=1) > self.config.cpu_threshold
        except Exception as e:
            self.logger.warning(f"获取CPU使用率失败: {e}")
            return False

    def _cleanup_by_time(self, directory: str) -> int:
        """按时间清理文件"""
        cutoff_time = time.time() - self.config.retention_seconds
        retention_str = self._format_seconds(self.config.retention_seconds)
        files_removed = 0

        with self.state_lock:
            files = self.file_registry.get(directory, [])
            to_remove = [f for f in files if f.mtime < cutoff_time]
            to_keep = [f for f in files if f not in to_remove]

            if to_remove:
                self.logger.info(f"目录 {directory} 有 {len(to_remove)} 个文件超过保留时间（{retention_str}）")
                for f in to_remove:
                    try:
                        Path(f.path).unlink()
                        files_removed += 1
                        self.logger.info(f"按时间清理: {f.path}")
                    except Exception as e:
                        self.logger.warning(f"删除失败 {f.path}: {e}")

            self.file_registry[directory] = to_keep

        return files_removed

    def _cleanup_by_count(self, directory: str) -> int:
        """按文件数量清理"""
        with self.state_lock:
            files = self.file_registry.get(directory, [])
            current_count = len(files)
            max_count = self.config.max_files_per_dir

            if current_count <= max_count:
                return 0

            need_remove = current_count - max_count
            files.sort(key=lambda x: x.mtime)  # 按修改时间排序（旧→新）
            to_remove = files[:need_remove]
            to_keep = files[need_remove:]

            self.logger.info(
                f"目录 {directory} 文件数超限（当前{current_count}，上限{max_count}），"
                f"清理最旧的{need_remove}个文件"
            )

            files_removed = 0
            for f in to_remove:
                try:
                    Path(f.path).unlink()
                    files_removed += 1
                    self.logger.info(f"按数量清理: {f.path}")
                except Exception as e:
                    self.logger.warning(f"删除失败 {f.path}: {e}")

            self.file_registry[directory] = to_keep

        return files_removed

    def _cleanup_by_size(self, directory: str) -> int:
        """按磁盘大小清理"""
        with self.state_lock:
            files = self.file_registry.get(directory, [])
            total_size = sum(f.size for f in files)
            max_size = self.config.max_disk_size_bytes

            if total_size <= max_size:
                return 0

            # 按修改时间排序（旧→新）
            files.sort(key=lambda x: x.mtime)
            current_size = total_size
            to_remove = []

            for f in files:
                if current_size <= max_size:
                    break
                to_remove.append(f)
                current_size -= f.size

            self.logger.info(
                f"目录 {directory} 大小超限（当前{total_size / 1024 / 1024:.2f}MB，"
                f"上限{max_size / 1024 / 1024:.2f}MB），清理最旧的{len(to_remove)}个文件"
            )

            files_removed = 0
            for f in to_remove:
                try:
                    Path(f.path).unlink()
                    files_removed += 1
                    self.logger.info(f"按大小清理: {f.path}")
                except Exception as e:
                    self.logger.warning(f"删除失败 {f.path}: {e}")

            self.file_registry[directory] = files[len(to_remove):]

        return files_removed

    def _perform_cleanup(self):
        """执行清理操作"""
        if self._is_cpu_busy():
            self.logger.info("CPU使用率超限，跳过本次清理")
            return

        self.logger.info("开始清理扫描...")
        for directory in self.config.directories:
            self._scan_directory(directory)
            time_removed = self._cleanup_by_time(directory)
            count_removed = self._cleanup_by_count(directory)
            size_removed = self._cleanup_by_size(directory)

            self.logger.info(
                f"目录 {directory} 清理完成: "
                f"时间[{time_removed}] 数量[{count_removed}] 大小[{size_removed}]"
            )

        self._save_state()
        # 更新心跳时间戳
        with self.heartbeat_lock:
            self.last_heartbeat = time.time()

    def _save_state(self):
        """保存状态到文件"""
        try:
            state_dir = os.path.dirname(self.config.state_file)
            if not os.path.exists(state_dir):
                os.makedirs(state_dir, exist_ok=True)

            with self.state_lock:
                state_data = {
                    dir_path: [
                        {
                            'path': f.path,
                            'size': f.size,
                            'mtime': f.mtime,
                            'relative_path': f.relative_path
                        } for f in files
                    ]
                    for dir_path, files in self.file_registry.items()
                }

            with open(self.config.state_file, 'w') as f:
                json.dump(state_data, f, indent=2)
        except Exception as e:
            self.logger.error(f"保存状态文件失败: {e}")

    def _heartbeat_monitor(self):
        """心跳检测线程逻辑"""
        self.logger.info(f"心跳检测线程启动（间隔{self._format_seconds(self.config.heartbeat_interval)}）")
        while self.running:
            try:
                current_time = time.time()
                with self.heartbeat_lock:
                    timeout = current_time - self.last_heartbeat > self.config.heartbeat_timeout

                if not self.cleanup_thread or not self.cleanup_thread.is_alive():
                    self.logger.warning("清理线程未运行，尝试重启...")
                    self._start_cleanup_thread()
                elif timeout:
                    self.logger.warning(
                        f"清理线程心跳超时（超过{self._format_seconds(self.config.heartbeat_timeout)}），重启...")
                    self._stop_cleanup_thread()
                    self._start_cleanup_thread()
                else:
                    self.logger.debug(f"心跳正常（上次活动: {datetime.fromtimestamp(self.last_heartbeat)}）")

                time.sleep(self.config.heartbeat_interval)
            except Exception as e:
                self.logger.error(f"心跳检测出错: {e}")
                time.sleep(60)

    def _start_cleanup_thread(self):
        """启动清理线程"""
        self.cleanup_thread = Thread(target=self._cleanup_loop, daemon=True)
        self.cleanup_thread.start()
        self.logger.info("清理线程已启动")

    def _stop_cleanup_thread(self):
        """停止清理线程"""
        if self.cleanup_thread and self.cleanup_thread.is_alive():
            self.logger.info("正在停止清理线程...")
            # 触发线程退出
            self.running = False
            self.cleanup_thread.join(timeout=10)
            if self.cleanup_thread.is_alive():
                self.logger.warning("清理线程强制终止")

    def _cleanup_loop(self):
        """清理线程主循环"""
        while self.running:
            try:
                self._perform_cleanup()
                # 等待下次扫描
                for _ in range(self.config.scan_interval):
                    if not self.running:
                        break
                    time.sleep(1)
            except Exception as e:
                self.logger.error(f"清理循环出错: {e}")
                time.sleep(60)

    def start(self):
        """启动守护程序（清理线程+心跳线程）"""
        self.running = True
        self._start_cleanup_thread()
        self.heartbeat_thread = Thread(target=self._heartbeat_monitor, daemon=True)
        self.heartbeat_thread.start()
        self.logger.info("日志清理守护程序已启动")

    def stop(self):
        """停止守护程序"""
        self.logger.info("正在停止守护程序...")
        self.running = False
        if self.cleanup_thread:
            self.cleanup_thread.join(timeout=10)
        if self.heartbeat_thread:
            self.heartbeat_thread.join(timeout=10)
        self._save_state()
        self.logger.info("守护程序已停止")


def start_cleanup_daemon():
    """启动清理守护程序（供外部调用）"""
    daemon = LogCleanupDaemon()
    daemon.start()
    return daemon


def main():
    """命令行入口"""
    daemon = LogCleanupDaemon()
    try:
        daemon.start()
        # 主线程等待中断信号
        signal.pause()
    except KeyboardInterrupt:
        daemon.stop()


if __name__ == "__main__":
    main()