"""
AstrBot 智能日程提醒插件
支持LLM监控模式和指令模式创建日程，支持TTS语音播报和网络搜索功能
支持循环日程（每天/每周/每月/每年）
"""

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig

# 使用Core层的消息组件（修复At组件问题）
from astrbot.core.message.components import Plain, At, Record

import asyncio
import concurrent.futures
import json
import os
import re
import time
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple

# 导入数据库模块
from .database import (
    ScheduleDatabase,
    ScheduleItem,
    ReminderStatus,
    RecurrenceType,
    migrate_from_json,
)

# 用于数据库操作的线程池执行器
_db_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="db_")


@register(
    "astrbot_plugin_reminder",
    "findx010197",
    "智能日程提醒插件，支持LLM监控与指令双模式，循环日程，戳一戳",
    "3.2.3",
    "https://github.com/findx010197/astrbot_plugin_reminder",
)
class ReminderPlugin(Star):
    """智能日程提醒插件"""

    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}

        # 存储定时任务（内存中，重启后从数据库恢复）
        self.timers: Dict[str, asyncio.Task] = {}
        
        # 消息去重缓存 (key: f"{sender_id}:{message_hash}", value: timestamp)
        self._message_dedup_cache: dict[str, float] = {}

        # 数据存储路径
        # 使用相对路径，避免依赖 astrbot.core
        self.data_dir = os.path.join("data", "plugin_data", "astrbot_plugin_reminder")
        self.db_file = os.path.join(self.data_dir, "schedules.db")

        # 旧数据文件路径（用于迁移）
        self.old_data_file = os.path.join(self.data_dir, "schedules.json")
        self.old_counter_file = os.path.join(self.data_dir, "id_counters.json")

        # 初始化数据库
        self.db: ScheduleDatabase = None

        logger.info(f"日程提醒插件配置加载完成: {self.config}")

    async def _run_db_operation(self, func, *args, **kwargs):
        """在线程池中执行数据库操作，避免阻塞事件循环"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(_db_executor, lambda: func(*args, **kwargs))

    async def _restore_timers(self):
        """从数据库恢复定时任务"""
        schedules = await self._run_db_operation(self.db.get_all_pending_schedules)
        now = time.time()
        restored = 0

        for schedule in schedules:
            if schedule.trigger_time > now:
                delay = schedule.trigger_time - now
                timer_task = asyncio.create_task(
                    self._reminder_timer(delay, schedule.id)
                )
                self.timers[schedule.id] = timer_task
                restored += 1

        logger.info(f"已恢复 {restored} 个待执行日程的定时任务")

    async def initialize(self):
        """插件初始化"""
        # 确保数据目录存在
        os.makedirs(self.data_dir, exist_ok=True)

        # 初始化数据库
        self.db = ScheduleDatabase(self.db_file)

        # 检查是否需要从旧JSON迁移数据
        if os.path.exists(self.old_data_file):
            logger.info("检测到旧数据文件，开始迁移...")
            migrated = await self._run_db_operation(
                migrate_from_json, self.db, self.old_data_file, self.old_counter_file
            )
            logger.info(f"已迁移 {migrated} 条日程数据到SQLite数据库")

        # 加载待执行的日程并创建定时任务
        await self._restore_timers()

        # 清理过期日程
        cleaned = await self._run_db_operation(self.db.cleanup_expired_schedules)
        if cleaned > 0:
            logger.info(f"已清理 {cleaned} 个过期日程")

        logger.info("智能日程提醒插件已初始化完成")

    async def terminate(self):
        """插件终止时清理"""
        # 取消所有进行中的定时任务
        for timer_task in self.timers.values():
            if not timer_task.done():
                timer_task.cancel()
        self.timers.clear()

        # 关闭数据库连接
        if self.db:
            self.db.close()

        logger.info("日程提醒插件已清理所有任务并终止")

    # ==================== LLM监控模式 ====================

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """监听所有消息，判断是否触发日程设定（LLM监控模式）"""
        # 检查是否启用LLM监控模式
        if not self.config.get("enable_llm_monitor", True):
            return

        message_str = event.message_str.strip()
        if not message_str:
            return

        # 消息长度过滤：太短或太长的消息不太可能是日程请求
        if len(message_str) < 4 or len(message_str) > 200:
            return

        # 防止与指令冲突：如果消息以指令前缀开头，则忽略
        # 这里列出插件注册的指令和常见的指令前缀
        # 注意：这里不仅要匹配 /callme，还要匹配 callme（因为有些平台不需要前缀）
        # 还要匹配别名
        cmd_prefixes = [
            "/callme",
            "callme",
            "/提醒我",
            "/remind",
            "remind",
            "／callme",
            "／提醒我",  # 全角符号兼容
            "/",  # 任何以 / 开头的指令都跳过
        ]

        lower_msg = message_str.lower()
        if any(lower_msg.startswith(prefix) for prefix in cmd_prefixes):
            return

        # 额外检查：如果消息完全等于 "list" 或 "cancel"，也忽略（可能是后续交互）
        if lower_msg in ["list", "cancel", "help"]:
            return

        # 快速预过滤：先用关键词检查，避免每条消息都调用 LLM
        trigger_keywords = self.config.get(
            "trigger_keywords",
            [
                "提醒我",
                "提醒",
                "帮我提醒",
                "设置提醒",
                "定时提醒",
                "日程提醒",
                "记得提醒",
                "别忘了提醒",
                "callme",
                "喊我",
                "叫我",
            ],
        )

        # 如果不包含任何触发关键词，直接跳过（不调用 LLM）
        has_trigger_keyword = any(keyword in message_str for keyword in trigger_keywords)
        if not has_trigger_keyword:
            return  # 快速返回，避免 LLM 调用

        # 消息去重：防止多群同时收到同一消息导致重复处理
        sender_id = event.get_sender_id()
        message_hash = hash(message_str)
        dedup_key = f"{sender_id}:{message_hash}"
        current_time = time.time()
        
        # 清理过期缓存（60秒前的记录）
        expired_keys = [k for k, t in self._message_dedup_cache.items() if current_time - t > 60]
        for k in expired_keys:
            del self._message_dedup_cache[k]
        
        # 检查是否重复
        if dedup_key in self._message_dedup_cache:
            logger.info(f"[LLM监控模式] 重复消息，跳过处理: {message_str[:50]}")
            return
        
        # 记录此次处理
        self._message_dedup_cache[dedup_key] = current_time

        logger.info(f"[LLM监控模式] 关键词预匹配成功: {message_str}")
        logger.debug(f"[LLM监控模式] 发送者: {sender_id}, 消息Hash: {message_hash}")

        # 第一步：使用 LLM 进一步确认是否为日程设定请求（带超时）
        logger.info(f"[LLM监控模式] 步骤1: 开始判断是否为日程请求...")
        try:
            is_trigger = await asyncio.wait_for(
                self._is_schedule_trigger(message_str, event),
                timeout=10.0  # 10秒超时
            )
        except asyncio.TimeoutError:
            logger.warning(f"[LLM监控模式] LLM判断超时，跳过消息: {message_str[:50]}")
            event.stop_event()  # 确保停止事件传播
            return
        except Exception as e:
            logger.error(f"[LLM监控模式] LLM判断异常: {e}", exc_info=True)
            event.stop_event()
            return

        if not is_trigger:
            logger.info(f"[LLM监控模式] 不是日程请求，跳过")
            return  # 不是日程设定请求，继续其他处理

        logger.info(f"[LLM监控模式] 检测到日程设定请求: {message_str}")

        # 第二步：提取日程信息（带超时）
        logger.info(f"[LLM监控模式] 步骤2: 开始提取日程信息...")
        try:
            schedule_info = await asyncio.wait_for(
                self._extract_schedule_info(message_str, event),
                timeout=15.0  # 15秒超时
            )
            logger.info(f"[LLM监控模式] 提取结果: {schedule_info}")
        except asyncio.TimeoutError:
            logger.warning(f"[LLM监控模式] 提取日程信息超时")
            yield event.plain_result("抱歉，处理超时了，请稍后重试或使用指令模式 /callme")
            event.stop_event()
            return
        except Exception as e:
            logger.error(f"[LLM监控模式] 提取日程信息异常: {e}", exc_info=True)
            yield event.plain_result("抱歉，处理出错了，请稍后重试")
            event.stop_event()
            return

        if not schedule_info:
            logger.warning(f"[LLM监控模式] 无法解析日程信息: {message_str[:50]}")
            yield event.plain_result(
                "抱歉，我无法理解您的日程请求，请提供更明确的时间和事件内容。"
            )
            event.stop_event()
            return

        # 检查是否有 @ 对象
        logger.info(f"[LLM监控模式] 步骤3: 检查@对象...")
        try:
            target_id, target_name = await asyncio.wait_for(
                self._get_at_target(event),
                timeout=10.0  # 10秒超时
            )
            if target_id:
                logger.info(f"[LLM监控模式] 找到@对象: {target_name} ({target_id})")
                schedule_info["target_id"] = target_id
                schedule_info["target_name"] = target_name
            else:
                logger.info(f"[LLM监控模式] 未找到@对象，默认为自己")
        except asyncio.TimeoutError:
            logger.warning(f"[LLM监控模式] 获取@对象超时，默认为自己")
        except Exception as e:
            logger.error(f"[LLM监控模式] 获取@对象异常: {e}", exc_info=True)

        # 第三步：创建日程（带超时）
        logger.info(f"[LLM监控模式] 步骤4: 开始创建日程...")
        try:
            result = await asyncio.wait_for(
                self._create_schedule(event, schedule_info),
                timeout=20.0  # 20秒超时
            )
            logger.info(f"[LLM监控模式] 创建结果: success={result.get('success')}")
        except asyncio.TimeoutError:
            logger.warning(f"[LLM监控模式] 创建日程超时")
            yield event.plain_result("抱歉，创建日程超时了，请稍后重试")
            event.stop_event()
            return
        except Exception as e:
            logger.error(f"[LLM监控模式] 创建日程异常: {e}", exc_info=True)
            yield event.plain_result("抱歉，创建日程失败，请稍后重试")
            event.stop_event()
            return

        if result["success"]:
            # 生成人格化回复（带超时，失败时使用兜底回复）
            logger.info(f"[LLM监控模式] 步骤5: 生成确认回复...")
            try:
                response = await asyncio.wait_for(
                    self._generate_confirmation_response(schedule_info, event),
                    timeout=10.0  # 10秒超时
                )
            except asyncio.TimeoutError:
                logger.warning(f"[LLM监控模式] 生成确认回复超时，使用兜底回复")
                response = f"✅ 好的，我会在{schedule_info.get('time', '指定时间')}提醒您：{schedule_info.get('event', '待办事项')}"
            except Exception as e:
                logger.error(f"[LLM监控模式] 生成回复异常: {e}")
                response = f"✅ 好的，我会在{schedule_info.get('time', '指定时间')}提醒您：{schedule_info.get('event', '待办事项')}"
            yield event.plain_result(response)
            logger.info(f"[LLM监控模式] 处理完成，已发送确认消息")
        else:
            logger.warning(f"[LLM监控模式] 创建失败: {result['message']}")
            yield event.plain_result(result["message"])

        event.stop_event()
        logger.info(f"[LLM监控模式] 事件处理完毕，已停止传播")

    def _clean_llm_response(self, text: str) -> str:
        """清洗LLM返回的文本，去除思考过程和废话"""
        if not text:
            return ""

        # 1. 去除 <think>...</think> 标签及其内容
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
        # 去除 [Thinking] 等变体
        text = re.sub(r"\[.*?\]", "", text, flags=re.DOTALL)

        # 2. 去除 Markdown 代码块标记（如果有）
        text = re.sub(r"^```.*?\n", "", text)
        text = re.sub(r"\n```$", "", text)

        # 3. 去除常见的废话前缀
        prefixes = [
            "好的，",
            "没问题，",
            "根据您的设定，",
            "生成的消息如下：",
            "确认消息：",
        ]
        for prefix in prefixes:
            if text.startswith(prefix):
                text = text[len(prefix) :]

        return text.strip()

    async def _get_at_target(
        self, event: AstrMessageEvent
    ) -> tuple[Optional[str], Optional[str]]:
        """从消息中获取第一个被@的用户ID和真实昵称
        
        返回: (target_id, target_name)
        """
        from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
        
        # 调试日志：打印消息链结构
        try:
            logger.debug(
                f"消息链结构: {[type(c).__name__ for c in event.message_obj.message]}"
            )
        except Exception:
            pass

        # 1. 从消息链中提取At组件
        target_id = None
        for component in event.message_obj.message:
            if isinstance(component, At):  # 使用Core层的At
                target_id = str(getattr(component, "qq", getattr(component, "id", "")))
                if target_id:
                    break

        if not target_id:
            # 2. 如果没有At组件，尝试从文本中正则匹配 @xxx（仅记录，不获取ID）
            text = event.message_str
            match = re.search(r"@(\S+)", text)
            if match:
                name = match.group(1)
                logger.info(f"从文本中匹配到 @{name}，但无法获取真实ID")
            return None, None
        
        # 3. 获取真实昵称（仅aiocqhttp平台支持）
        target_name = "TA"  # 默认名称
        
        if isinstance(event, AiocqhttpMessageEvent):
            group_id = event.get_group_id()
            if group_id:  # 群聊环境
                try:
                    members = await event.bot.api.call_action('get_group_member_list', group_id=group_id)
                    if members:
                        for m in members:
                            if str(m.get("user_id")) == target_id:
                                card = m.get("card", "")
                                nickname = m.get("nickname", "")
                                target_name = card if card else nickname
                                logger.info(f"获取到群成员昵称: {target_name} (ID:{target_id})")
                                break
                except Exception as e:
                    logger.warning(f"获取群成员昵称失败: {e}")
            else:  # 私聊环境
                try:
                    stranger_info = await event.bot.api.call_action('get_stranger_info', user_id=int(target_id))
                    if stranger_info:
                        target_name = stranger_info.get("nickname", "TA")
                        logger.info(f"获取到私聊昵称: {target_name} (ID:{target_id})")
                except Exception as e:
                    logger.warning(f"获取私聊昵称失败: {e}")
        
        return target_id, target_name

    async def _is_schedule_trigger(self, message: str, event: AstrMessageEvent) -> bool:
        """判断消息是否为日程设定请求"""
        # 关键词快速匹配 - 匹配成功直接返回True，避免LLM调用
        trigger_keywords = self.config.get(
            "trigger_keywords",
            [
                "提醒我",
                "提醒",
                "帮我提醒",
                "设置提醒",
                "定时提醒",
                "日程提醒",
                "记得提醒",
                "别忘了提醒",
                "callme",
                "喊我",
                "叫我",
            ],
        )

        if any(keyword in message for keyword in trigger_keywords):
            logger.info(f"通过关键词匹配触发日程设定: {message}")
            return True  # 直接返回，不调用LLM

        # 如果关键词未匹配，说明在on_message预过滤时已拦截，这里不应该到达
        return False

    async def _extract_schedule_info(
        self, message: str, event: AstrMessageEvent
    ) -> Optional[dict]:
        """使用LLM提取日程关键信息"""
        provider = await self._get_main_provider(event)
        if not provider:
            return None

        # 是否需要网络搜索
        search_info = ""
        if self.config.get("enable_web_search", False):
            search_info = await self._search_for_schedule(message, event)

        current_time = datetime.now().strftime("%Y年%m月%d日 %H:%M:%S 星期%w")

        prompt = f"""请从以下用户消息中提取日程关键信息，并以JSON格式返回：
用户消息："{message}"
当前时间：{current_time}
{f"网络搜索补充信息：{search_info}" if search_info else ""}

请提取以下信息：
1. time: 提醒的具体时间描述（如：明天下午3点、10分钟后、下周一早上9点等）
2. event: 要提醒的事件内容（用简洁的语言描述）
3. target: 提醒的目标对象（如果有的话，没有则填"用户"）

请严格按照以下JSON格式返回，不要包含其他内容：
{{
    "time": "时间描述",
    "event": "事件内容",
    "target": "目标对象"
}}"""

        try:
            response = await provider.text_chat(prompt=prompt)
            result_text = response.completion_text.strip()

            # 尝试提取JSON
            json_match = re.search(r"\{[^{}]*\}", result_text, re.DOTALL)
            if json_match:
                result_text = json_match.group()

            schedule_info = json.loads(result_text)

            if "time" in schedule_info and "event" in schedule_info:
                schedule_info["search_info"] = search_info
                return schedule_info

            logger.warning(f"提取的信息缺少关键字段: {result_text}")
            return None

        except json.JSONDecodeError as e:
            logger.error(f"LLM返回的不是有效的JSON格式: {e}")
            return None
        except Exception as e:
            logger.error(f"提取日程信息时出错: {e}")
            return None

    # ==================== 指令模式 ====================

    @filter.command_group("callme")
    def callme_group(self):
        """日程提醒指令组"""
        pass

    @callme_group.command("list")
    async def list_reminders(self, event: AstrMessageEvent):
        """查看我的提醒列表

        用法: /callme list
        """
        sender_id = event.get_sender_id()

        # 从数据库获取用户的待执行日程（异步执行）
        user_schedules = await self._run_db_operation(
            self.db.get_user_pending_schedules, sender_id
        )

        if not user_schedules:
            yield event.plain_result("📭 您当前没有待执行的提醒哦~")
            return

        # 构建美观的列表
        result_lines = [
            "╔═══════════════════════════╗",
            "║     📋 我的提醒列表       ║",
            "╠═══════════════════════════╣",
        ]

        now = datetime.now()

        for i, schedule in enumerate(user_schedules, 1):
            trigger_dt = datetime.fromtimestamp(schedule.trigger_time)

            # 智能显示时间
            if trigger_dt.date() == now.date():
                # 今天，只显示时间
                time_str = f"今天 {trigger_dt.strftime('%H:%M')}"
            elif trigger_dt.date() == (now + timedelta(days=1)).date():
                # 明天
                time_str = f"明天 {trigger_dt.strftime('%H:%M')}"
            else:
                # 其他日期
                time_str = trigger_dt.strftime("%m/%d %H:%M")

            # 事件内容（截断过长的内容）
            event_text = schedule.event_content
            if len(event_text) > 12:
                event_text = event_text[:11] + "…"

            # 目标用户标识
            target_mark = ""
            if schedule.target_id and schedule.target_id != schedule.sender_id:
                target_mark = (
                    f" →@{schedule.target_name[:4]}"
                    if schedule.target_name
                    else " →@他人"
                )

            # 格式化每行
            result_lines.append(f"║ {schedule.short_id} │ {time_str}")
            result_lines.append(f"║      └─ {event_text}{target_mark}")

            if i < len(user_schedules):
                result_lines.append("║ ─────────────────────────")

        result_lines.append("╚═══════════════════════════╝")
        result_lines.append("")
        result_lines.append("💡 取消提醒: /callme cancel <ID>")
        result_lines.append(f"   如: /callme cancel {user_schedules[0].short_id}")

        yield event.plain_result("\n".join(result_lines))

    @callme_group.command("cancel")
    async def cancel_reminder(self, event: AstrMessageEvent, reminder_id: str = ""):
        """取消提醒

        用法: /callme cancel <提醒ID>
        示例: /callme cancel R0001
        """
        if not reminder_id:
            yield event.plain_result(
                "请提供要取消的提醒ID。\n用法: /callme cancel <ID>\n示例: /callme cancel R0001"
            )
            return

        sender_id = event.get_sender_id()
        reminder_id = reminder_id.upper()  # 统一转大写

        # 从数据库查找匹配的日程（异步执行）
        matched = await self._run_db_operation(
            self.db.find_schedule_by_id_prefix, reminder_id, sender_id
        )

        if not matched:
            yield event.plain_result(
                f"❌ 未找到ID为 '{reminder_id}' 的提醒\n\n请使用 /callme list 查看您的提醒列表"
            )
            return

        # 取消定时任务
        if matched.id in self.timers:
            self.timers[matched.id].cancel()
            del self.timers[matched.id]

        # 更新数据库状态（异步执行）
        await self._run_db_operation(
            self.db.update_schedule_status, matched.id, ReminderStatus.CANCELLED.value
        )

        # 美化取消确认消息
        trigger_dt = datetime.fromtimestamp(matched.trigger_time)
        time_str = trigger_dt.strftime("%m月%d日 %H:%M")

        yield event.plain_result(
            f"✅ 已取消提醒\n"
            f"─────────────\n"
            f"📌 {matched.event_content}\n"
            f"⏰ {time_str}\n"
            f"🏷️ {matched.short_id}"
        )

    @callme_group.command("every")
    async def callme_every(self, event: AstrMessageEvent):
        """创建循环提醒

        用法: /callme every <周期> <时间> <事件>
        周期: day(每天) / week(每周) / month(每月)

        示例:
        - /callme every day 9:00 晨会
        - /callme every week 一 14:00 周报
        - /callme every month 15 10:00 交房租
        """
        message = event.message_str.strip()

        # 移除指令前缀
        prefixes = ["/callme every", "callme every"]
        for prefix in prefixes:
            if message.lower().startswith(prefix):
                message = message[len(prefix):].strip()
                break

        if not message:
            yield event.plain_result(
                "📌 循环提醒使用说明：\n\n"
                "/callme every <周期> <时间> <事件>\n\n"
                "周期类型：\n"
                "• day - 每天\n"
                "• week - 每周（后跟星期几）\n"
                "• month - 每月（后跟几号）\n\n"
                "示例：\n"
                "• /callme every day 9:00 晨会\n"
                "• /callme every week 一 14:00 周报\n"
                "• /callme every month 15 10:00 交房租"
            )
            return

        # 解析循环参数
        parts = message.split(None, 3)  # 最多分割3次
        if len(parts) < 3:
            yield event.plain_result(
                "❌ 参数不足\n\n"
                "用法: /callme every <周期> <时间/日期> <事件>\n"
                "示例: /callme every day 9:00 晨会"
            )
            return

        recurrence_type = parts[0].lower()
        recurrence_value = ""
        recurrence_time = ""
        event_content = ""

        # 解析周期类型
        if recurrence_type in ["day", "daily", "每天"]:
            recurrence_type = "daily"
            # /callme every day 9:00 事件
            recurrence_time = parts[1]
            event_content = parts[2] if len(parts) > 2 else ""
        elif recurrence_type in ["week", "weekly", "每周"]:
            recurrence_type = "weekly"
            # /callme every week 一 14:00 事件
            # 解析星期几
            weekday_map = {
                "一": "1", "1": "1", "周一": "1", "星期一": "1", "monday": "1", "mon": "1",
                "二": "2", "2": "2", "周二": "2", "星期二": "2", "tuesday": "2", "tue": "2",
                "三": "3", "3": "3", "周三": "3", "星期三": "3", "wednesday": "3", "wed": "3",
                "四": "4", "4": "4", "周四": "4", "星期四": "4", "thursday": "4", "thu": "4",
                "五": "5", "5": "5", "周五": "5", "星期五": "5", "friday": "5", "fri": "5",
                "六": "6", "6": "6", "周六": "6", "星期六": "6", "saturday": "6", "sat": "6",
                "日": "7", "天": "7", "7": "7", "周日": "7", "星期日": "7", "sunday": "7", "sun": "7",
            }
            recurrence_value = weekday_map.get(parts[1].lower(), "1")
            recurrence_time = parts[2] if len(parts) > 2 else "09:00"
            event_content = parts[3] if len(parts) > 3 else ""
        elif recurrence_type in ["month", "monthly", "每月"]:
            recurrence_type = "monthly"
            # /callme every month 15 10:00 事件
            recurrence_value = parts[1]  # 几号
            recurrence_time = parts[2] if len(parts) > 2 else "09:00"
            event_content = parts[3] if len(parts) > 3 else ""
        else:
            yield event.plain_result(
                f"❌ 不支持的周期类型: {recurrence_type}\n\n"
                "支持的周期：day(每天) / week(每周) / month(每月)"
            )
            return

        if not event_content:
            yield event.plain_result(
                "❌ 请指定提醒事件\n\n"
                "示例: /callme every day 9:00 晨会"
            )
            return

        # 标准化时间格式
        if not re.match(r'^\d{1,2}:\d{2}$', recurrence_time):
            # 尝试解析其他格式
            time_match = re.search(r'(\d{1,2})[点时:：](\d{0,2})', recurrence_time)
            if time_match:
                hour = time_match.group(1)
                minute = time_match.group(2) or "00"
                recurrence_time = f"{hour}:{minute.zfill(2)}"
            else:
                recurrence_time = "09:00"  # 默认早上9点

        # 计算首次触发时间
        now = datetime.now()
        hour, minute = map(int, recurrence_time.split(':'))
        
        if recurrence_type == "daily":
            # 每天：今天或明天
            trigger_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if trigger_dt <= now:
                trigger_dt += timedelta(days=1)
        elif recurrence_type == "weekly":
            # 每周：计算下个对应星期几
            target_weekday = int(recurrence_value) - 1  # 转为 0-6
            if target_weekday < 0:
                target_weekday = 6
            days_ahead = target_weekday - now.weekday()
            if days_ahead < 0 or (days_ahead == 0 and now.hour * 60 + now.minute >= hour * 60 + minute):
                days_ahead += 7
            trigger_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            trigger_dt += timedelta(days=days_ahead)
        elif recurrence_type == "monthly":
            # 每月：计算下个对应日期
            import calendar
            target_day = int(recurrence_value)
            trigger_dt = now.replace(day=min(target_day, calendar.monthrange(now.year, now.month)[1]),
                                     hour=hour, minute=minute, second=0, microsecond=0)
            if trigger_dt <= now:
                # 推到下个月
                if now.month == 12:
                    trigger_dt = trigger_dt.replace(year=now.year + 1, month=1)
                else:
                    max_day = calendar.monthrange(now.year, now.month + 1)[1]
                    trigger_dt = trigger_dt.replace(month=now.month + 1, day=min(target_day, max_day))

        trigger_time = trigger_dt.timestamp()

        # 构建 schedule_info
        schedule_info = {
            "time": trigger_dt.strftime("%Y-%m-%d %H:%M"),
            "event": event_content,
            "recurrence": recurrence_type,
            "recurrence_time": recurrence_time,
            "recurrence_value": recurrence_value,
        }

        # 检查是否有 @ 对象
        target_id, target_name = await self._get_at_target(event)
        if target_id:
            schedule_info["target_id"] = target_id
            schedule_info["target_name"] = target_name

        # 创建日程
        result = await self._create_schedule(event, schedule_info, trigger_time)

        if result["success"]:
            schedule = result["schedule"]
            recurrence_desc_map = {
                "daily": "每天",
                "weekly": f"每周{['一','二','三','四','五','六','日'][int(recurrence_value or '1')-1]}",
                "monthly": f"每月{recurrence_value}号",
            }
            recurrence_desc = recurrence_desc_map.get(recurrence_type, "")
            
            yield event.plain_result(
                f"✅ 循环提醒已创建\n"
                f"─────────────\n"
                f"📌 {event_content}\n"
                f"🔁 {recurrence_desc} {recurrence_time}\n"
                f"⏰ 首次: {trigger_dt.strftime('%m/%d %H:%M')}\n"
                f"🏷️ {schedule.short_id}"
            )
        else:
            yield event.plain_result(result["message"])

    @callme_group.command("at")
    async def callme_at(self, event: AstrMessageEvent):
        """创建一次性提醒（指令模式）

        用法: /callme at <时间> <事件>
        
        时间格式示例：
        • 相对时间：5分钟后、半小时后、2小时后
        • 今天：今天下午3点、今晚8点
        • 明天：明天早上、明天下午2点
        • 具体日期：下周一上午9点、2月15日下午3点
        
        示例：
        • /callme at 10分钟后 记得喝水
        • /callme at 明天早上9点 开会
        • /callme at 下周一下午3点 提交报告
        """
        message = event.message_str.strip()

        # 移除指令前缀
        prefixes = ["/callme at", "callme at"]
        for prefix in prefixes:
            if message.lower().startswith(prefix):
                message = message[len(prefix):].strip()
                break

        if not message:
            yield event.plain_result(
                "📌 一次性提醒使用说明：\n\n"
                "/callme at <时间> <事件>\n\n"
                "时间格式示例：\n"
                "• 5分钟后、半小时后、2小时后\n"
                "• 今天下午3点、今晚8点\n"
                "• 明天早上、明天中午、明天晚上\n"
                "• 下周一上午9点\n\n"
                "示例：\n"
                "• /callme at 10分钟后 记得喝水\n"
                "• /callme at 明天早上9点 开会\n"
                "• /callme at 下周一下午3点 提交报告"
            )
            return

        # 使用LLM提取信息
        schedule_info = await self._extract_schedule_info(message, event)

        if not schedule_info:
            yield event.plain_result(
                "❌ 无法理解您的提醒请求\n\n"
                "请使用格式: /callme at <时间> <事件>\n"
                "示例: /callme at 明天早上9点 开会"
            )
            return

        # 检查是否有 @ 对象
        target_id, target_name = await self._get_at_target(event)
        if target_id:
            schedule_info["target_id"] = target_id
            schedule_info["target_name"] = target_name

        # 创建日程
        result = await self._create_schedule(event, schedule_info)

        if result["success"]:
            schedule = result["schedule"]
            trigger_dt = datetime.fromtimestamp(schedule.trigger_time)
            
            yield event.plain_result(
                f"✅ 一次性提醒已创建\n"
                f"─────────────\n"
                f"📌 {schedule.event_content}\n"
                f"⏰ {trigger_dt.strftime('%m月%d日 %H:%M')}\n"
                f"🏷️ {schedule.short_id}"
            )
        else:
            yield event.plain_result(result["message"])

    @filter.command("callme", alias={"提醒我", "remind"})
    async def callme_quick(self, event: AstrMessageEvent):
        """快速创建提醒

        用法示例:
        - /callme 等一下 记得喝水
        - /callme 10分钟后 提交报告
        - /callme 明天早上 开会
        - /callme 下周一下午3点 面试
        - /提醒我 半小时后 吃药
        """
        message = event.message_str.strip()

        # 移除指令前缀
        for prefix in ["/callme", "/提醒我", "/remind", "callme", "提醒我", "remind"]:
            if message.lower().startswith(prefix):
                message = message[len(prefix) :].strip()
                break

        if not message:
            yield event.plain_result(self._get_help_text())
            return

        # 使用LLM提取信息
        schedule_info = await self._extract_schedule_info(message, event)

        if not schedule_info:
            yield event.plain_result(
                "抱歉，我无法理解您的提醒请求。\n\n" + self._get_help_text()
            )
            return

        # 检查是否有 @ 对象
        target_id, target_name = await self._get_at_target(event)
        if target_id:
            schedule_info["target_id"] = target_id
            schedule_info["target_name"] = target_name

        # 创建日程
        result = await self._create_schedule(event, schedule_info)

        if result["success"]:
            response = await self._generate_confirmation_response(schedule_info, event)
            yield event.plain_result(response)
        else:
            yield event.plain_result(result["message"])

    def _get_help_text(self) -> str:
        """获取帮助文本"""
        return """📌 日程提醒使用说明：

**快速创建提醒：**
/callme <时间> <事件>

**时间格式示例：**
• 等一下/稍后/一会儿
• 5分钟后/半小时后/2小时后
• 明天早上/明天中午/明天晚上
• 下周一/星期三下午3点
• 12月25日上午10点

**其他指令：**
• /callme list - 查看提醒列表
• /callme cancel <ID> - 取消提醒

**示例：**
/callme 10分钟后 记得喝水
/提醒我 明天早上 开会"""

    # ==================== 核心功能 ====================

    async def _create_schedule(
        self, event: AstrMessageEvent, schedule_info: dict, preset_trigger_time: float = None
    ) -> dict:
        """创建日程
        
        Args:
            event: 消息事件
            schedule_info: 日程信息字典
            preset_trigger_time: 预设的触发时间（用于循环日程命令）
        """
        sender_id = event.get_sender_id()

        # 获取目标用户（如果是帮别人设置）
        target_id = schedule_info.get("target_id", sender_id)
        target_name = schedule_info.get("target_name", event.get_sender_name())
        if target_id == sender_id:  # 如果相等，说明是自己，修正名称
            target_name = event.get_sender_name()

        # 检查数量限制（异步执行）
        max_reminders = self.config.get("max_reminders", 20)
        user_count = await self._run_db_operation(
            self.db.count_user_pending_schedules, sender_id
        )

        if user_count >= max_reminders:
            return {
                "success": False,
                "message": f"您已达到最大提醒数量限制({max_reminders})，请先取消一些提醒再试。",
            }

        # 解析时间（如果没有预设则解析）
        time_str = schedule_info.get("time", "")
        if preset_trigger_time:
            trigger_time = preset_trigger_time
        else:
            trigger_time = await self._parse_time_string(time_str, event)

        if trigger_time is None:
            return {
                "success": False,
                "message": f"无法解析时间 '{time_str}'，请使用更明确的时间描述。",
            }

        if trigger_time <= time.time():
            return {"success": False, "message": "提醒时间必须是将来的时间。"}

        # 生成ID（异步执行）
        schedule_id = f"{sender_id}_{int(time.time() * 1000)}"  # 完整ID用于内部索引
        short_id = await self._run_db_operation(
            self.db.get_next_short_id, sender_id
        )  # 从数据库获取短ID

        # 获取循环日程信息
        recurrence_type = schedule_info.get("recurrence", "none")
        recurrence_value = schedule_info.get("recurrence_value", "")
        recurrence_time = schedule_info.get("recurrence_time", "")

        # 创建日程对象
        schedule = ScheduleItem(
            id=schedule_id,
            short_id=short_id,
            unified_msg_origin=event.unified_msg_origin,
            sender_id=sender_id,
            sender_name=event.get_sender_name(),
            event_content=schedule_info.get("event", "未知事件"),
            trigger_time=trigger_time,
            created_at=time.time(),
            status=ReminderStatus.PENDING.value,
            raw_time_str=time_str,
            search_info=schedule_info.get("search_info", ""),
            target_id=target_id,
            target_name=target_name,
            recurrence_type=recurrence_type,
            recurrence_value=recurrence_value,
            recurrence_time=recurrence_time,
            trigger_count=0,
        )

        # 保存到数据库（异步执行）
        success = await self._run_db_operation(self.db.insert_schedule, schedule)
        if not success:
            return {"success": False, "message": "保存日程失败，请稍后重试。"}

        # 创建定时任务
        delay = trigger_time - time.time()
        timer_task = asyncio.create_task(self._reminder_timer(delay, schedule_id))
        self.timers[schedule_id] = timer_task

        trigger_dt = datetime.fromtimestamp(trigger_time)
        logger.info(
            f"已创建日程 [{short_id}] {schedule_info.get('event')}, 触发时间: {trigger_dt.strftime('%m/%d %H:%M')}"
        )

        return {"success": True, "schedule": schedule, "message": "日程创建成功"}

    async def _reminder_timer(self, delay: float, schedule_id: str):
        """定时器任务"""
        try:
            await asyncio.sleep(delay)

            # 从数据库获取最新的日程信息（异步执行）
            schedule = await self._run_db_operation(
                self.db.get_schedule_by_id, schedule_id
            )

            if schedule and schedule.status == ReminderStatus.PENDING.value:
                short_id = schedule.short_id

                # 发送提醒消息
                await self._send_reminder(schedule)

                # 判断是否为循环日程
                if schedule.is_recurring:
                    # 计算下次触发时间
                    next_trigger_time = self._calculate_next_trigger(schedule)
                    
                    if next_trigger_time:
                        # 更新数据库中的触发时间和计数
                        await self._run_db_operation(
                            self.db.update_schedule_for_next_trigger,
                            schedule_id,
                            next_trigger_time,
                            True  # increment_count
                        )
                        
                        # 创建新的定时任务
                        delay = next_trigger_time - time.time()
                        if delay > 0:
                            timer_task = asyncio.create_task(self._reminder_timer(delay, schedule_id))
                            self.timers[schedule_id] = timer_task
                            
                            next_dt = datetime.fromtimestamp(next_trigger_time)
                            logger.info(
                                f"循环日程 {short_id} 已重新调度，下次触发: {next_dt.strftime('%Y-%m-%d %H:%M')}"
                            )
                    else:
                        # 无法计算下次时间，标记为已触发
                        await self._run_db_operation(
                            self.db.update_schedule_status,
                            schedule_id,
                            ReminderStatus.TRIGGERED.value,
                        )
                        if schedule_id in self.timers:
                            del self.timers[schedule_id]
                        logger.warning(f"循环日程 {short_id} 无法计算下次触发时间，已结束")
                else:
                    # 一次性日程，标记为已触发
                    await self._run_db_operation(
                        self.db.update_schedule_status,
                        schedule_id,
                        ReminderStatus.TRIGGERED.value,
                    )

                    # 清理内存中的定时任务引用
                    if schedule_id in self.timers:
                        del self.timers[schedule_id]

                    logger.info(f"提醒 {short_id} 已触发并完成")

        except asyncio.CancelledError:
            logger.info(f"提醒任务 {schedule_id} 已被取消")
        except Exception as e:
            logger.error(f"提醒任务 {schedule_id} 执行出错: {e}")

    def _calculate_next_trigger(self, schedule: ScheduleItem) -> Optional[float]:
        """计算循环日程的下次触发时间
        
        Args:
            schedule: 日程对象
            
        Returns:
            下次触发的时间戳，如果无法计算则返回None
        """
        import calendar
        
        if not schedule.is_recurring:
            return None
            
        current_trigger = datetime.fromtimestamp(schedule.trigger_time)
        recurrence_type = schedule.recurrence_type
        
        try:
            if recurrence_type == "daily":
                # 每天：推进1天
                next_dt = current_trigger + timedelta(days=1)
                
            elif recurrence_type == "weekly":
                # 每周：推进7天
                next_dt = current_trigger + timedelta(days=7)
                
            elif recurrence_type == "monthly":
                # 每月：推进到下个月的同一天
                recurrence_value = schedule.recurrence_value or str(current_trigger.day)
                target_day = int(recurrence_value)
                
                # 计算下个月
                if current_trigger.month == 12:
                    next_year = current_trigger.year + 1
                    next_month = 1
                else:
                    next_year = current_trigger.year
                    next_month = current_trigger.month + 1
                
                # 处理月末日期（如31号在2月会变成28/29号）
                max_day_in_next_month = calendar.monthrange(next_year, next_month)[1]
                actual_day = min(target_day, max_day_in_next_month)
                
                next_dt = current_trigger.replace(
                    year=next_year,
                    month=next_month,
                    day=actual_day
                )
                
            elif recurrence_type == "yearly":
                # 每年：推进1年
                next_dt = current_trigger.replace(year=current_trigger.year + 1)
                
            else:
                logger.warning(f"未知的循环类型: {recurrence_type}")
                return None
            
            return next_dt.timestamp()
            
        except Exception as e:
            logger.error(f"计算下次触发时间失败: {e}")
            return None

    async def _send_poke_reminder(self, schedule: ScheduleItem):
        """发送戳一戳提醒（参考pokepro插件实现）
        
        Args:
            schedule: 日程项目，包含unified_msg_origin和target_id
        """
        try:
            umo = schedule.unified_msg_origin
            target_id = schedule.target_id
            
            if not target_id:
                logger.debug("没有目标用户ID，跳过戳一戳")
                return
            
            # 从unified_msg_origin解析平台信息
            # 格式: platform_name:message_type:session_id
            parts = umo.split(":")
            if len(parts) < 3:
                logger.warning(f"无效的unified_msg_origin格式: {umo}")
                return
            
            platform_name = parts[0]
            message_type = parts[1]
            session_id = parts[2]
            
            # 只有aiocqhttp平台支持戳一戳
            if platform_name != "aiocqhttp":
                logger.debug(f"平台 {platform_name} 不支持戳一戳")
                return
            
            # 获取平台实例
            from astrbot.api.event import filter as event_filter
            platform = self.context.get_platform(event_filter.PlatformAdapterType.AIOCQHTTP)
            if not platform:
                logger.warning("未找到aiocqhttp平台实例")
                return
            
            # 获取client（参考pokepro的实现）
            client = None
            if hasattr(platform, 'get_client'):
                client = platform.get_client()
            elif hasattr(platform, 'client'):
                client = platform.client
            
            if not client:
                logger.warning("无法获取aiocqhttp客户端实例")
                return
            
            # 检查是否为群聊
            is_group = message_type == "group"
            
            if is_group:
                # 群戳一戳（参考pokepro: await client.group_poke(group_id=int(group_id), user_id=tid)）
                group_id = session_id
                logger.info(f"发送群戳一戳: group={group_id}, user={target_id}")
                await client.group_poke(group_id=int(group_id), user_id=int(target_id))
            else:
                # 私聊戳一戳（参考pokepro: await client.friend_poke(user_id=tid)）
                logger.info(f"发送私聊戳一戳: user={target_id}")
                await client.friend_poke(user_id=int(target_id))
            
            logger.info(f"戳一戳发送成功: target={target_id}")
            
        except Exception as e:
            logger.error(f"发送戳一戳失败: {e}", exc_info=True)

    async def _send_reminder(self, schedule: ScheduleItem):
        """发送提醒消息（完整流程：戳一戳+@+文本+TTS）"""
        try:
            umo = schedule.unified_msg_origin

            # 生成提醒消息
            message = await self._generate_reminder_message(schedule)

            # ===== 步骤1: 戳一戳提醒对象 =====
            if schedule.target_id and self.config.get("enable_poke", True):
                logger.info(f"发送戳一戳: target={schedule.target_id}")
                await self._send_poke_reminder(schedule)
                # 戳一戳后稍微延迟一下，避免消息发送过快
                await asyncio.sleep(0.5)

            # ===== 步骤2: @提醒对象+昵称+提醒文本 =====
            chain = []

            # 始终尝试@目标用户（包括自己提醒自己）
            # 只要有 target_id，就添加 @
            if schedule.target_id:
                chain.append(At(qq=int(schedule.target_id)))  # 修复：使用int类型
                chain.append(Plain(" "))  # At后加个空格

            chain.append(Plain(message))
            
            # 发送文本消息
            message_chain = MessageChain()
            message_chain.chain = chain
            await self.context.send_message(umo, message_chain)

            logger.info(f"已发送提醒消息: {message[:50]}...")

            # ===== 步骤3: TTS语音播报 =====
            if self.config.get("enable_tts", False):
                await self._send_tts_reminder(schedule, message)

        except Exception as e:
            logger.error(f"发送提醒消息失败: {e}", exc_info=True)

    async def _send_tts_reminder(self, schedule: ScheduleItem, message: str):
        """发送TTS语音提醒"""
        try:
            tts_provider_id = self.config.get("tts_provider")
            if not tts_provider_id:
                logger.warning("未配置TTS提供商，跳过语音播报")
                return

            # 获取TTS提供商
            tts_provider = self.context.get_provider_by_id(tts_provider_id)
            if not tts_provider:
                logger.warning(f"未找到TTS提供商: {tts_provider_id}")
                return

            # 调试：打印 Provider 的属性，方便排查接口名
            logger.debug(
                f"TTS Provider {type(tts_provider).__name__} methods: {dir(tts_provider)}"
            )

            # 生成语音
            # AstrBot TTS Provider 标准接口为 get_audio(text)，返回音频文件路径
            audio_path = None
            if hasattr(tts_provider, "get_audio"):
                try:
                    audio_path = await tts_provider.get_audio(message)
                except Exception as e:
                    logger.error(f"调用 TTS Provider.get_audio 失败: {e}")
                    return
            # 兼容性尝试：某些旧插件可能使用其他名称
            elif hasattr(tts_provider, "generate_voice"):
                # ... (保留旧逻辑作为 fallback，但主要依赖 get_audio)
                try:
                    ret = await tts_provider.generate_voice(message)
                    if ret:
                        # 如果返回二进制数据，需要手动保存
                        if isinstance(ret, bytes):
                            temp_path = (
                                self.data_dir
                                / f"tts_{schedule.id}_{int(time.time())}.wav"
                            )
                            with open(temp_path, "wb") as f:
                                f.write(ret)
                            audio_path = str(temp_path)
                        # 如果返回对象
                        elif hasattr(ret, "audio_content"):
                            temp_path = (
                                self.data_dir
                                / f"tts_{schedule.id}_{int(time.time())}.wav"
                            )
                            with open(temp_path, "wb") as f:
                                f.write(ret.audio_content)
                            audio_path = str(temp_path)
                except Exception:
                    pass

            if not audio_path:
                # 再次尝试 text_to_speech
                if hasattr(tts_provider, "text_to_speech"):
                    try:
                        audio_data = await tts_provider.text_to_speech(message)
                        # ... (同上，二进制保存逻辑)
                        if isinstance(audio_data, bytes):
                            temp_path = (
                                self.data_dir
                                / f"tts_{schedule.id}_{int(time.time())}.wav"
                            )
                            with open(temp_path, "wb") as f:
                                f.write(audio_data)
                            audio_path = str(temp_path)
                    except Exception:
                        pass

            if audio_path and os.path.exists(audio_path):
                # 发送语音消息
                chain = [Record(file=str(audio_path), url=str(audio_path))]
                message_chain = MessageChain()
                message_chain.chain = chain
                await self.context.send_message(
                    schedule.unified_msg_origin, message_chain
                )

                # AstrBot 的 TTS 生成的文件通常在临时目录，插件不应负责清理，或者根据具体实现
                # 这里暂不删除，以免发送未完成文件被删
            else:
                logger.warning(
                    f"TTS生成失败或文件不存在。Provider: {type(tts_provider).__name__}"
                )

        except Exception as e:
            logger.error(f"TTS语音播报执行异常: {e}")

    # ==================== 时间解析 ====================

    async def _parse_time_string(
        self, time_str: str, event: AstrMessageEvent
    ) -> Optional[float]:
        """解析时间字符串为时间戳"""
        now = datetime.now()

        # 快捷时间处理
        quick_presets = self.config.get("quick_time_presets", {})

        # "等一下"、"一会儿"等
        if any(k in time_str for k in ["等一下", "一会儿", "等会", "稍等"]):
            minutes = quick_presets.get("wait_a_moment", 5)
            return (now + timedelta(minutes=minutes)).timestamp()

        # "稍后"、"过会儿"
        if any(k in time_str for k in ["稍后", "过会儿", "待会"]):
            minutes = quick_presets.get("short_while", 10)
            return (now + timedelta(minutes=minutes)).timestamp()

        # "半小时后"
        if "半小时" in time_str:
            minutes = quick_presets.get("half_hour", 30)
            return (now + timedelta(minutes=minutes)).timestamp()

        # X分钟后
        minute_match = re.search(r"(\d+)\s*分钟", time_str)
        if minute_match:
            minutes = int(minute_match.group(1))
            return (now + timedelta(minutes=minutes)).timestamp()

        # X小时后
        hour_match = re.search(r"(\d+)\s*小时", time_str)
        if hour_match:
            hours = int(hour_match.group(1))
            return (now + timedelta(hours=hours)).timestamp()

        # 使用LLM解析复杂时间
        return await self._llm_parse_time(time_str, event)

    async def _llm_parse_time(
        self, time_str: str, event: AstrMessageEvent
    ) -> Optional[float]:
        """使用LLM解析复杂时间表达"""
        provider = await self._get_main_provider(event)
        if not provider:
            return None

        current_time = datetime.now()
        time_presets = self.config.get("default_time_points", {})

        prompt = f"""请将以下时间描述转换为具体的日期时间（YYYY-MM-DD HH:MM:SS）。

时间描述："{time_str}"
当前时间：{current_time.strftime("%Y-%m-%d %H:%M:%S")} 星期{["一", "二", "三", "四", "五", "六", "日"][current_time.weekday()]}

【推断规则】
1. **模糊时刻（如“3点”、“十点”）**：
   - 优先理解为**未来最近**的那个时刻。
   - 如果当前是上午且该时刻在当前之后，视为上午的时刻（如9点说“10点”->今日10:00）。
   - 如果当前是下午且该时刻在当前之后，视为下午的时刻（如14点说“4点”->今日16:00）。
   - 如果该时刻在今日已过去，视为晚上的时刻（如11点说“10点”->今日22:00）或明天的时刻。
   
2. **模糊日期（如“星期五”、“15号”）**：
   - 优先理解为**本周**或**本月**尚未发生的日期。
   - 只有当本周/本月该日期已过去时，才顺延至下周/下月。

3. **默认时间点**（如果只说了日期没说具体几点）：
   - 早上/上午：{time_presets.get("morning", "08:00")}
   - 中午：{time_presets.get("noon", "12:00")}
   - 下午：{time_presets.get("afternoon", "14:00")}
   - 晚上：{time_presets.get("evening", "19:00")}
   - 没说时段：{time_presets.get("default_weekday", "09:00")}

请直接返回格式化的时间字符串（YYYY-MM-DD HH:MM:SS），不要包含其他内容。如果无法解析，返回"无法解析"。"""

        try:
            response = await provider.text_chat(prompt=prompt)
            result = response.completion_text.strip()

            if "无法解析" in result:
                return None

            # 尝试解析返回的时间
            time_match = re.search(
                r"(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2}):?(\d{2})?", result
            )
            if time_match:
                year = int(time_match.group(1))
                month = int(time_match.group(2))
                day = int(time_match.group(3))
                hour = int(time_match.group(4))
                minute = int(time_match.group(5))
                second = int(time_match.group(6)) if time_match.group(6) else 0

                target_time = datetime(year, month, day, hour, minute, second)
                return target_time.timestamp()

            return None

        except Exception as e:
            logger.error(f"LLM解析时间失败: {e}")
            return None

    # ==================== 消息生成 ====================

    async def _generate_confirmation_response(
        self, schedule_info: dict, event: AstrMessageEvent
    ) -> str:
        """生成日程确认回复"""
        time_str = schedule_info.get("time", "指定时间")
        event_content = schedule_info.get("event", "未知事件")
        target_id = schedule_info.get("target_id", "")
        sender_id = event.get_sender_id()

        is_self = target_id == sender_id or not target_id

        # 获取用户昵称映射
        sender_display = self._get_user_display_name(sender_id, event.get_sender_name())
        target_display = self._get_user_display_name(
            target_id, schedule_info.get("target_name", "TA")
        ) if target_id else sender_display

        # 检查是否为循环日程
        is_recurring = schedule_info.get("recurrence_type", "none") != "none"
        recurrence_desc = ""
        if is_recurring:
            rec_type = schedule_info.get("recurrence_type", "")
            if rec_type == "daily":
                recurrence_desc = "每天"
            elif rec_type == "weekly":
                recurrence_desc = "每周"
            elif rec_type == "monthly":
                recurrence_desc = "每月"
            elif rec_type == "yearly":
                recurrence_desc = "每年"

        if not self.config.get("enable_personality", True):
            time_prefix = f"{recurrence_desc}" if recurrence_desc else ""
            if is_self:
                return f"✅ 好的，我会{time_prefix}在{time_str}提醒您：{event_content}"
            else:
                return f"✅ 好的，我会{time_prefix}在{time_str}提醒{target_display}：{event_content}"

        # 获取人格设定（优先使用专用提醒人设）
        use_dedicated_persona, persona_dict, max_length = self._get_reminder_persona()

        if use_dedicated_persona and persona_dict:
            # 使用专用提醒人设
            system_prompt = self._build_persona_system_prompt(persona_dict)
        else:
            # 回退到系统人设
            system_prompt = await self._get_persona_prompt(event.unified_msg_origin)
            if not system_prompt:
                # 最终默认人设
                system_prompt = "你说话语气绵软慵懒，语速舒缓，带着一丝漫不经心的疏离感，却又娇憨柔和。习惯带\"哦~\"、\"呢\"、\"呀\"等轻柔尾音。"
            max_length = 50

        provider = await self._get_main_provider(event)
        if not provider:
            time_prefix = f"{recurrence_desc}" if recurrence_desc else ""
            return f"✅ 好的，我会{time_prefix}在{time_str}提醒{'你' if is_self else target_display}：{event_content}"

        target_rule = (
            "对用户使用\"你\"来称呼，语气要贴心。"
            if is_self
            else f"必须直呼其名\"{target_display}\"，禁止使用\"你\"。"
        )

        prompt = f"""【任务】生成一条日程确认消息

【你的人设】
{system_prompt}

【日程信息】
- 提醒时间：{time_str}
- 提醒事件：{event_content}
- 提醒对象：{"用户自己（昵称：" + sender_display + "）" if is_self else "昵称：" + target_display}

【称呼规则】
{target_rule}

【输出要求】
1. 字数限制：整条回复不超过{max_length}字。
2. 核心内容：必须明确表达\"已记下/记住了\"的意思。
3. 直接输出：不要有任何前缀、解释或思考过程。

请直接输出回复内容："""

        try:
            response = await provider.text_chat(prompt=prompt)
            raw_text = response.completion_text.strip()
            cleaned = self._clean_llm_response(raw_text)
            # 超长则使用兜底回复
            if len(cleaned) > max_length + 30:
                time_prefix = f"{recurrence_desc}" if recurrence_desc else ""
                if is_self:
                    return f"✅ 好的，我会{time_prefix}在{time_str}提醒你：{event_content}"
                else:
                    return f"✅ 好的，我会{time_prefix}在{time_str}提醒{target_display}：{event_content}"
            return cleaned
        except Exception as e:
            logger.error(f"生成确认回复失败: {e}")
            time_prefix = f"{recurrence_desc}" if recurrence_desc else ""
            return f"✅ 好的，我会{time_prefix}在{time_str}提醒{'你' if is_self else target_display}：{event_content}"

    def _get_user_display_name(self, user_id: str, default_name: str) -> str:
        """获取用户显示名称（优先使用配置的别名）"""
        # 配置格式为列表，每项格式为 "用户ID,称呼"
        user_alias_list = self.config.get("user_alias", [])
        for item in user_alias_list:
            if not isinstance(item, str) or "," not in item:
                continue
            # 按逗号分割，只分割第一个逗号（防止称呼中包含逗号）
            parts = item.split(",", 1)
            if len(parts) == 2:
                uid = parts[0].strip()
                alias = parts[1].strip()
                if uid == str(user_id) and alias:
                    return alias
        return default_name

    async def _generate_reminder_message(self, schedule: ScheduleItem) -> str:
        """生成提醒消息"""
        is_self_reminder = (
            schedule.target_id == schedule.sender_id or not schedule.target_id
        )

        # 获取用户显示名称（优先使用配置的别名）
        sender_display = self._get_user_display_name(
            schedule.sender_id, schedule.sender_name
        )
        target_display = self._get_user_display_name(
            schedule.target_id, schedule.target_name
        ) if schedule.target_id else sender_display

        # 基础兜底回复
        base_reply = f"{schedule.event_content}的时间到了哦！"
        if not is_self_reminder:
            base_reply = f"是{sender_display}拜托我提醒{target_display}{schedule.event_content}的哦~"

        if not self.config.get("enable_personality", True):
            return base_reply

        # 获取人设配置（优先使用专用提醒人设）
        use_dedicated_persona, persona_dict, max_length = self._get_reminder_persona()

        if use_dedicated_persona and persona_dict:
            # 使用专用提醒人设
            system_prompt = self._build_persona_system_prompt(persona_dict)
        else:
            # 回退到系统人设
            system_prompt = await self._get_persona_prompt(schedule.unified_msg_origin)
            if not system_prompt:
                # 最终默认人设
                system_prompt = "你说话语气绵软慵懒，语速舒缓，带着一丝漫不经心的疏离感，却又娇憨柔和。习惯带\"哦~\"、\"呢\"、\"呀\"等轻柔尾音。"
            max_length = 50

        provider = await self._get_main_provider_by_umo(schedule.unified_msg_origin)
        if not provider:
            return base_reply

        # 构建场景与称呼规则
        if is_self_reminder:
            scene_desc = f"这是{sender_display}自己设置的提醒。"
            name_rule = f"对用户说话时使用\"你\"或直呼其昵称\"{sender_display}\"。"
        else:
            scene_desc = f"这是{sender_display}委托你提醒{target_display}的事项。"
            name_rule = f"需要自然提及是{sender_display}托你来提醒的，对被提醒者使用\"你\"或其昵称\"{target_display}\"。"

        prompt = f"""【任务】生成一条日程提醒消息。

【你的人设】
{system_prompt}

【提醒事项】
{schedule.event_content}

【场景说明】
{scene_desc}

【称呼规则】
{name_rule}

【输出要求】
1. 字数限制：整条回复不超过{max_length}字（含事项内容）。
2. 语气自然：像朋友间的提醒，禁止"系统提醒"、"时间到了"等机械用语。
3. 直接输出：不要有任何前缀、解释或思考过程。

请直接输出回复内容："""

        try:
            response = await provider.text_chat(prompt=prompt)
            raw_text = response.completion_text.strip()
            cleaned_text = self._clean_llm_response(raw_text)
            # 超长则使用兜底回复
            if len(cleaned_text) > max_length + 20:
                return base_reply
            return cleaned_text
        except Exception as e:
            logger.error(f"生成提醒消息失败: {e}")
            return base_reply

    # ==================== 网络搜索 ====================

    async def _search_for_schedule(self, message: str, event: AstrMessageEvent) -> str:
        """为日程搜索相关信息"""
        try:
            provider = await self._get_main_provider(event)
            if not provider:
                return ""

            # 判断是否需要搜索
            judge_prompt = f"""请判断以下日程内容是否需要从网络搜索获取更多信息：
"{message}"

需要搜索的情况包括：
1. 涉及具体活动、电影、演出的时间
2. 需要查询天气、航班等实时信息
3. 涉及特定地点的营业时间等

如果需要搜索，回复"需要搜索：<搜索关键词>"
如果不需要，回复"不需要"
"""

            response = await provider.text_chat(prompt=judge_prompt)
            result = response.completion_text.strip()

            if "不需要" in result:
                return ""

            # 提取搜索关键词
            if "需要搜索" in result:
                search_keyword = (
                    result.replace("需要搜索：", "").replace("需要搜索:", "").strip()
                )

                # 这里可以集成实际的搜索功能
                # 目前返回空字符串，实际使用时可以调用浏览器搜索API
                logger.info(f"需要搜索: {search_keyword}")

                # TODO: 实现实际的搜索功能
                # search_result = await self._do_web_search(search_keyword)
                # return search_result

            return ""

        except Exception as e:
            logger.error(f"搜索相关信息失败: {e}")
            return ""

    # ==================== 辅助方法 ====================

    async def _get_main_provider(self, event: AstrMessageEvent):
        """获取主LLM提供商"""
        provider_id = self.config.get("llm_provider")
        if provider_id:
            return self.context.get_provider_by_id(provider_id)
        return self.context.get_using_provider(umo=event.unified_msg_origin)

    async def _get_main_provider_by_umo(self, umo: str):
        """根据UMO获取主LLM提供商"""
        provider_id = self.config.get("llm_provider")
        if provider_id:
            return self.context.get_provider_by_id(provider_id)
        return self.context.get_using_provider(umo=umo)

    async def _get_detection_provider(self, event: AstrMessageEvent):
        """获取日程检测用的LLM提供商"""
        detection_provider_id = self.config.get("schedule_detection_llm")
        if detection_provider_id:
            return self.context.get_provider_by_id(detection_provider_id)
        return await self._get_main_provider(event)

    def _get_reminder_persona(self) -> tuple[bool, dict, int]:
        """获取提醒人设配置

        返回值：(是否启用, 人设字典, 最大字数)
        人设字典包含: name, character_setting, event_handling, content_control, max_length, user_alias_requirement
        """
        reminder_persona_config = self.config.get("reminder_persona", {})

        # 检查是否启用
        enabled = reminder_persona_config.get("enabled", False)
        if not enabled:
            return False, None, 50

        # 获取当前活跃人设名称
        active_persona_name = reminder_persona_config.get("active_persona_name", "哥伦比娅")

        # 从人设库中查找对应的人设
        persona_list = self.config.get("reminder_persona_list", [])
        for persona in persona_list:
            if isinstance(persona, dict) and persona.get("name") == active_persona_name:
                max_length = persona.get("max_length", 50)
                return True, persona, max_length

        # 如果未找到指定的人设，返回禁用状态
        logger.warning(f"未找到人设'{active_persona_name}'，已禁用提醒人设功能")
        return False, None, 50

    def _build_persona_system_prompt(self, persona: dict) -> str:
        """根据人设配置构建完整的系统提示词

        参数：
            persona: 人设字典，包含各项配置

        返回值：完整的系统提示词
        """
        if not persona or not isinstance(persona, dict):
            return ""

        character_setting = persona.get("character_setting", "")
        event_handling = persona.get("event_handling", "")
        content_control = persona.get("content_control", "")
        user_alias_requirement = persona.get("user_alias_requirement", "")

        prompt = f"""{character_setting}

【事件处理方式】
{event_handling}

【内容生成约束】
{content_control}

【用户昵称使用约束】
{user_alias_requirement}"""

        return prompt

    async def _get_persona_prompt(self, umo: str) -> str:
        """获取人格设定提示词"""
        try:
            persona_manager = self.context.persona_manager
            default_persona = await persona_manager.get_default_persona_v3(umo=umo)
            if default_persona and "prompt" in default_persona:
                return default_persona["prompt"]
        except Exception as e:
            logger.warning(f"获取人格设定失败: {e}")
        return ""
