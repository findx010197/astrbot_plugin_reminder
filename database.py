"""
AstrBot 日程提醒插件 - 数据库管理模块
使用 SQLite 进行日程数据的持久化存储
"""

import sqlite3
import os
import time
from datetime import datetime
from typing import Dict, List, Optional
from dataclasses import dataclass, asdict
from enum import Enum
from contextlib import contextmanager
import threading
from astrbot.api import logger


class ReminderStatus(Enum):
    """提醒状态枚举"""

    PENDING = "pending"  # 等待触发
    TRIGGERED = "triggered"  # 已触发
    CANCELLED = "cancelled"  # 已取消


@dataclass
class ScheduleItem:
    """日程数据结构"""

    id: str  # 唯一ID（完整标识符）
    short_id: str  # 短ID（用于用户交互，如 "R0001"）
    unified_msg_origin: str  # 会话标识
    sender_id: str  # 发送者ID（创建者）
    sender_name: str  # 发送者名称
    event_content: str  # 事件内容
    trigger_time: float  # 触发时间戳
    created_at: float  # 创建时间戳
    status: str = "pending"  # 状态
    raw_time_str: str = ""  # 原始时间描述
    search_info: str = ""  # 网络搜索获取的信息
    target_id: str = ""  # 目标用户ID（为空则默认为sender_id）
    target_name: str = ""  # 目标用户名称

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_row(cls, row: tuple) -> "ScheduleItem":
        """从数据库行创建对象"""
        return cls(
            id=row[0],
            short_id=row[1],
            unified_msg_origin=row[2],
            sender_id=row[3],
            sender_name=row[4],
            event_content=row[5],
            trigger_time=row[6],
            created_at=row[7],
            status=row[8],
            raw_time_str=row[9] or "",
            search_info=row[10] or "",
            target_id=row[11] or "",
            target_name=row[12] or "",
        )


class ScheduleDatabase:
    """日程数据库管理类"""

    # 数据库版本，用于未来的迁移
    DB_VERSION = 1

    def __init__(self, db_path: str):
        """
        初始化数据库

        Args:
            db_path: 数据库文件路径
        """
        self.db_path = db_path
        self._local = threading.local()

        # 确保目录存在
        db_dir = os.path.dirname(db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)

        # 初始化数据库结构
        self._init_database()

    def _get_connection(self) -> sqlite3.Connection:
        """获取线程本地的数据库连接"""
        if not hasattr(self._local, "connection") or self._local.connection is None:
            self._local.connection = sqlite3.connect(
                self.db_path, check_same_thread=False
            )
            self._local.connection.row_factory = sqlite3.Row
        return self._local.connection

    @contextmanager
    def _get_cursor(self):
        """获取数据库游标的上下文管理器"""
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            yield cursor
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            cursor.close()

    def _init_database(self):
        """初始化数据库表结构"""
        with self._get_cursor() as cursor:
            # 创建日程表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS schedules (
                    id TEXT PRIMARY KEY,
                    short_id TEXT NOT NULL,
                    unified_msg_origin TEXT NOT NULL,
                    sender_id TEXT NOT NULL,
                    sender_name TEXT NOT NULL,
                    event_content TEXT NOT NULL,
                    trigger_time REAL NOT NULL,
                    created_at REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    raw_time_str TEXT,
                    search_info TEXT,
                    target_id TEXT,
                    target_name TEXT
                )
            """)

            # 创建索引以加速查询
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_schedules_sender_id 
                ON schedules(sender_id)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_schedules_status 
                ON schedules(status)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_schedules_trigger_time 
                ON schedules(trigger_time)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_schedules_short_id 
                ON schedules(short_id)
            """)

            # 创建ID计数器表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS id_counters (
                    sender_id TEXT PRIMARY KEY,
                    counter INTEGER NOT NULL DEFAULT 0
                )
            """)

            # 创建元数据表（用于版本管理等）
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)

            # 记录数据库版本
            cursor.execute(
                """
                INSERT OR REPLACE INTO metadata (key, value) VALUES ('db_version', ?)
            """,
                (str(self.DB_VERSION),),
            )

    # ==================== 日程 CRUD 操作 ====================

    def insert_schedule(self, schedule: ScheduleItem) -> bool:
        """
        插入新日程

        Args:
            schedule: 日程对象

        Returns:
            是否成功
        """
        try:
            with self._get_cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO schedules (
                        id, short_id, unified_msg_origin, sender_id, sender_name,
                        event_content, trigger_time, created_at, status,
                        raw_time_str, search_info, target_id, target_name
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        schedule.id,
                        schedule.short_id,
                        schedule.unified_msg_origin,
                        schedule.sender_id,
                        schedule.sender_name,
                        schedule.event_content,
                        schedule.trigger_time,
                        schedule.created_at,
                        schedule.status,
                        schedule.raw_time_str,
                        schedule.search_info,
                        schedule.target_id,
                        schedule.target_name,
                    ),
                )
            return True
        except Exception as e:
            logger.error(f"插入日程失败: {e}")
            return False

    def get_schedule_by_id(self, schedule_id: str) -> Optional[ScheduleItem]:
        """
        通过完整ID获取日程

        Args:
            schedule_id: 完整日程ID

        Returns:
            日程对象或None
        """
        with self._get_cursor() as cursor:
            cursor.execute(
                """
                SELECT id, short_id, unified_msg_origin, sender_id, sender_name,
                       event_content, trigger_time, created_at, status,
                       raw_time_str, search_info, target_id, target_name
                FROM schedules WHERE id = ?
            """,
                (schedule_id,),
            )
            row = cursor.fetchone()
            if row:
                return ScheduleItem.from_row(tuple(row))
        return None

    def get_schedule_by_short_id(
        self, short_id: str, sender_id: str = None
    ) -> Optional[ScheduleItem]:
        """
        通过短ID获取日程

        Args:
            short_id: 短ID
            sender_id: 可选的发送者ID筛选

        Returns:
            日程对象或None
        """
        with self._get_cursor() as cursor:
            if sender_id:
                cursor.execute(
                    """
                    SELECT id, short_id, unified_msg_origin, sender_id, sender_name,
                           event_content, trigger_time, created_at, status,
                           raw_time_str, search_info, target_id, target_name
                    FROM schedules 
                    WHERE short_id = ? AND sender_id = ? AND status = 'pending'
                """,
                    (short_id.upper(), sender_id),
                )
            else:
                cursor.execute(
                    """
                    SELECT id, short_id, unified_msg_origin, sender_id, sender_name,
                           event_content, trigger_time, created_at, status,
                           raw_time_str, search_info, target_id, target_name
                    FROM schedules WHERE short_id = ? AND status = 'pending'
                """,
                    (short_id.upper(),),
                )
            row = cursor.fetchone()
            if row:
                return ScheduleItem.from_row(tuple(row))
        return None

    def find_schedule_by_id_prefix(
        self, id_prefix: str, sender_id: str
    ) -> Optional[ScheduleItem]:
        """
        通过ID前缀查找日程（支持短ID和完整ID）

        Args:
            id_prefix: ID前缀
            sender_id: 发送者ID

        Returns:
            匹配的日程对象或None
        """
        id_prefix = id_prefix.upper()

        with self._get_cursor() as cursor:
            # 1. 先尝试短ID精确匹配
            cursor.execute(
                """
                SELECT id, short_id, unified_msg_origin, sender_id, sender_name,
                       event_content, trigger_time, created_at, status,
                       raw_time_str, search_info, target_id, target_name
                FROM schedules 
                WHERE short_id = ? AND sender_id = ? AND status = 'pending'
            """,
                (id_prefix, sender_id),
            )
            row = cursor.fetchone()
            if row:
                return ScheduleItem.from_row(tuple(row))

            # 2. 短ID前缀匹配
            cursor.execute(
                """
                SELECT id, short_id, unified_msg_origin, sender_id, sender_name,
                       event_content, trigger_time, created_at, status,
                       raw_time_str, search_info, target_id, target_name
                FROM schedules 
                WHERE short_id LIKE ? AND sender_id = ? AND status = 'pending'
                LIMIT 1
            """,
                (f"{id_prefix}%", sender_id),
            )
            row = cursor.fetchone()
            if row:
                return ScheduleItem.from_row(tuple(row))

            # 3. 完整ID前缀匹配（兼容旧方式）
            cursor.execute(
                """
                SELECT id, short_id, unified_msg_origin, sender_id, sender_name,
                       event_content, trigger_time, created_at, status,
                       raw_time_str, search_info, target_id, target_name
                FROM schedules 
                WHERE id LIKE ? AND sender_id = ? AND status = 'pending'
                LIMIT 1
            """,
                (f"{id_prefix.lower()}%", sender_id),
            )
            row = cursor.fetchone()
            if row:
                return ScheduleItem.from_row(tuple(row))

        return None

    def get_user_pending_schedules(self, sender_id: str) -> List[ScheduleItem]:
        """
        获取用户所有待执行的日程

        Args:
            sender_id: 发送者ID

        Returns:
            日程列表，按触发时间排序
        """
        schedules = []
        with self._get_cursor() as cursor:
            cursor.execute(
                """
                SELECT id, short_id, unified_msg_origin, sender_id, sender_name,
                       event_content, trigger_time, created_at, status,
                       raw_time_str, search_info, target_id, target_name
                FROM schedules 
                WHERE sender_id = ? AND status = 'pending'
                ORDER BY trigger_time ASC
            """,
                (sender_id,),
            )
            for row in cursor.fetchall():
                schedules.append(ScheduleItem.from_row(tuple(row)))
        return schedules

    def get_all_pending_schedules(self) -> List[ScheduleItem]:
        """
        获取所有待执行的日程

        Returns:
            日程列表，按触发时间排序
        """
        schedules = []
        with self._get_cursor() as cursor:
            cursor.execute("""
                SELECT id, short_id, unified_msg_origin, sender_id, sender_name,
                       event_content, trigger_time, created_at, status,
                       raw_time_str, search_info, target_id, target_name
                FROM schedules 
                WHERE status = 'pending'
                ORDER BY trigger_time ASC
            """)
            for row in cursor.fetchall():
                schedules.append(ScheduleItem.from_row(tuple(row)))
        return schedules

    def update_schedule_status(self, schedule_id: str, status: str) -> bool:
        """
        更新日程状态

        Args:
            schedule_id: 日程ID
            status: 新状态

        Returns:
            是否成功
        """
        try:
            with self._get_cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE schedules SET status = ? WHERE id = ?
                """,
                    (status, schedule_id),
                )
            return True
        except Exception as e:
            logger.error(f"更新日程状态失败: {e}")
            return False

    def delete_schedule(self, schedule_id: str) -> bool:
        """
        删除日程（物理删除）

        Args:
            schedule_id: 日程ID

        Returns:
            是否成功
        """
        try:
            with self._get_cursor() as cursor:
                cursor.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
            return True
        except Exception as e:
            logger.error(f"删除日程失败: {e}")
            return False

    def count_user_pending_schedules(self, sender_id: str) -> int:
        """
        统计用户待执行日程数量

        Args:
            sender_id: 发送者ID

        Returns:
            日程数量
        """
        with self._get_cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*) FROM schedules 
                WHERE sender_id = ? AND status = 'pending'
            """,
                (sender_id,),
            )
            return cursor.fetchone()[0]

    def cleanup_expired_schedules(self) -> int:
        """
        清理已过期的待执行日程（触发时间已过但状态仍为pending）

        Returns:
            清理的数量
        """
        now = time.time()
        with self._get_cursor() as cursor:
            cursor.execute(
                """
                UPDATE schedules 
                SET status = 'triggered' 
                WHERE status = 'pending' AND trigger_time < ?
            """,
                (now,),
            )
            return cursor.rowcount

    # ==================== ID计数器操作 ====================

    def get_next_short_id(self, sender_id: str) -> str:
        """
        获取用户的下一个短ID

        Args:
            sender_id: 发送者ID

        Returns:
            新的短ID (如 "R0001")
        """
        with self._get_cursor() as cursor:
            # 获取当前计数
            cursor.execute(
                """
                SELECT counter FROM id_counters WHERE sender_id = ?
            """,
                (sender_id,),
            )
            row = cursor.fetchone()

            if row:
                counter = row[0] + 1
                cursor.execute(
                    """
                    UPDATE id_counters SET counter = ? WHERE sender_id = ?
                """,
                    (counter, sender_id),
                )
            else:
                counter = 1
                cursor.execute(
                    """
                    INSERT INTO id_counters (sender_id, counter) VALUES (?, ?)
                """,
                    (sender_id, counter),
                )

        # 格式: R + 4位数字，循环使用
        return f"R{counter % 10000:04d}"

    def get_user_counter(self, sender_id: str) -> int:
        """获取用户当前的计数器值"""
        with self._get_cursor() as cursor:
            cursor.execute(
                """
                SELECT counter FROM id_counters WHERE sender_id = ?
            """,
                (sender_id,),
            )
            row = cursor.fetchone()
            return row[0] if row else 0

    # ==================== 统计与维护 ====================

    def get_statistics(self) -> Dict:
        """
        获取数据库统计信息

        Returns:
            统计信息字典
        """
        with self._get_cursor() as cursor:
            stats = {}

            # 总日程数
            cursor.execute("SELECT COUNT(*) FROM schedules")
            stats["total_schedules"] = cursor.fetchone()[0]

            # 各状态数量
            cursor.execute("""
                SELECT status, COUNT(*) FROM schedules GROUP BY status
            """)
            stats["by_status"] = dict(cursor.fetchall())

            # 用户数量
            cursor.execute("SELECT COUNT(DISTINCT sender_id) FROM schedules")
            stats["unique_users"] = cursor.fetchone()[0]

            # 今日创建数
            today_start = datetime.now().replace(hour=0, minute=0, second=0).timestamp()
            cursor.execute(
                """
                SELECT COUNT(*) FROM schedules WHERE created_at >= ?
            """,
                (today_start,),
            )
            stats["created_today"] = cursor.fetchone()[0]

        return stats

    def vacuum(self):
        """整理数据库，回收空间"""
        conn = self._get_connection()
        conn.execute("VACUUM")

    def close(self):
        """关闭数据库连接"""
        if hasattr(self._local, "connection") and self._local.connection:
            self._local.connection.close()
            self._local.connection = None


# ==================== 数据迁移工具 ====================


def migrate_from_json(
    db: ScheduleDatabase, json_file: str, counter_file: str = None
) -> int:
    """
    从旧的JSON文件迁移数据到SQLite数据库

    Args:
        db: 数据库实例
        json_file: JSON数据文件路径
        counter_file: ID计数器文件路径（可选）

    Returns:
        迁移的记录数
    """
    import json

    migrated = 0

    # 迁移日程数据
    if os.path.exists(json_file):
        try:
            with open(json_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            for item_data in data:
                # 兼容旧数据
                if "target_id" not in item_data:
                    item_data["target_id"] = item_data.get("sender_id", "")
                if "target_name" not in item_data:
                    item_data["target_name"] = item_data.get("sender_name", "")
                if "short_id" not in item_data:
                    old_id = item_data.get("id", "")
                    if "_" in old_id:
                        ts_part = old_id.split("_")[-1]
                        item_data["short_id"] = f"R{ts_part[-4:]}"
                    else:
                        item_data["short_id"] = f"R{hash(old_id) % 10000:04d}"

                schedule = ScheduleItem(**item_data)
                if db.insert_schedule(schedule):
                    migrated += 1

            # 备份并删除旧文件
            backup_file = json_file + ".bak"
            os.rename(json_file, backup_file)

        except Exception as e:
            logger.error(f"迁移日程数据失败: {e}")

    # 迁移ID计数器
    if counter_file and os.path.exists(counter_file):
        try:
            with open(counter_file, "r", encoding="utf-8") as f:
                counters = json.load(f)

            with db._get_cursor() as cursor:
                for sender_id, counter in counters.items():
                    cursor.execute(
                        """
                        INSERT OR REPLACE INTO id_counters (sender_id, counter) VALUES (?, ?)
                    """,
                        (sender_id, counter),
                    )

            # 备份并删除旧文件
            backup_file = counter_file + ".bak"
            os.rename(counter_file, backup_file)

        except Exception as e:
            logger.error(f"迁移计数器数据失败: {e}")

    return migrated
