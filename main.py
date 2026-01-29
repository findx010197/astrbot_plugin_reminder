"""
AstrBot 智能日程提醒插件
支持LLM监控模式和指令模式创建日程，支持TTS语音播报和网络搜索功能
"""

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
import astrbot.api.message_components as Comp
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

import asyncio
import json
import os
import re
import time
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, asdict
from enum import Enum


class ReminderStatus(Enum):
    """提醒状态枚举"""
    PENDING = "pending"      # 等待触发
    TRIGGERED = "triggered"  # 已触发
    CANCELLED = "cancelled"  # 已取消


@dataclass
class ScheduleItem:
    """日程数据结构"""
    id: str                          # 唯一ID
    unified_msg_origin: str          # 会话标识
    sender_id: str                   # 发送者ID
    sender_name: str                 # 发送者名称
    event_content: str               # 事件内容
    trigger_time: float              # 触发时间戳
    created_at: float                # 创建时间戳
    status: str = "pending"          # 状态
    raw_time_str: str = ""           # 原始时间描述
    search_info: str = ""            # 网络搜索获取的信息
    
    def to_dict(self) -> dict:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: dict) -> "ScheduleItem":
        return cls(**data)


@register("astrbot_plugin_reminder", "findx010197", "智能日程提醒插件，支持LLM监控与指令双模式", "1.0.0", "https://github.com/findx010197/astrbot_plugin_reminder")
class ReminderPlugin(Star):
    """智能日程提醒插件"""
    
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}
        
        # 存储定时任务
        self.timers: Dict[str, asyncio.Task] = {}
        # 存储日程信息
        self.schedules: Dict[str, ScheduleItem] = {}
        
        # 数据存储路径 (get_astrbot_data_path() 返回 str，需要用 os.path.join 拼接)
        self.data_dir = os.path.join(get_astrbot_data_path(), "plugin_data", "astrbot_plugin_reminder")
        self.data_file = os.path.join(self.data_dir, "schedules.json")
        
        logger.info(f"日程提醒插件配置加载完成: {self.config}")

    async def initialize(self):
        """插件初始化"""
        # 确保数据目录存在
        os.makedirs(self.data_dir, exist_ok=True)
        
        # 加载持久化数据
        if self.config.get("data_persistence", True):
            await self._load_schedules()
        
        logger.info("智能日程提醒插件已初始化完成")

    async def terminate(self):
        """插件终止时清理"""
        # 取消所有进行中的定时任务
        for timer_task in self.timers.values():
            if not timer_task.done():
                timer_task.cancel()
        self.timers.clear()
        
        # 保存数据
        if self.config.get("data_persistence", True):
            await self._save_schedules()
        
        logger.info("日程提醒插件已清理所有任务并终止")

    # ==================== 数据持久化 ====================
    
    async def _load_schedules(self):
        """从文件加载日程数据"""
        try:
            if os.path.exists(self.data_file):
                with open(self.data_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                
                now = time.time()
                for item_data in data:
                    item = ScheduleItem.from_dict(item_data)
                    # 只加载未过期的待执行日程
                    if item.status == ReminderStatus.PENDING.value and item.trigger_time > now:
                        self.schedules[item.id] = item
                        # 重新创建定时任务
                        delay = item.trigger_time - now
                        timer_task = asyncio.create_task(self._reminder_timer(delay, item.id))
                        self.timers[item.id] = timer_task
                
                logger.info(f"已加载 {len(self.schedules)} 个待执行日程")
        except Exception as e:
            logger.error(f"加载日程数据失败: {e}")

    async def _save_schedules(self):
        """保存日程数据到文件"""
        try:
            data = [item.to_dict() for item in self.schedules.values()]
            with open(self.data_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            logger.debug(f"已保存 {len(data)} 个日程")
        except Exception as e:
            logger.error(f"保存日程数据失败: {e}")

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
        
        # 第一步：判断是否触发日程设定
        is_trigger = await self._is_schedule_trigger(message_str, event)
        
        if not is_trigger:
            return  # 不是日程设定请求，继续其他处理
        
        logger.info(f"[LLM监控模式] 检测到日程设定请求: {message_str}")
        
        # 第二步：提取日程信息
        schedule_info = await self._extract_schedule_info(message_str, event)
        
        if not schedule_info:
            yield event.plain_result("抱歉，我无法理解您的日程请求，请提供更明确的时间和事件内容。")
            event.stop_event()
            return
        
        # 第三步：创建日程
        result = await self._create_schedule(event, schedule_info)
        
        if result["success"]:
            # 生成人格化回复
            response = await self._generate_confirmation_response(schedule_info, event)
            yield event.plain_result(response)
        else:
            yield event.plain_result(result["message"])
        
        event.stop_event()

    async def _is_schedule_trigger(self, message: str, event: AstrMessageEvent) -> bool:
        """判断消息是否为日程设定请求"""
        # 关键词快速匹配
        trigger_keywords = self.config.get("trigger_keywords", 
            ["提醒我", "设置提醒", "定时提醒", "日程提醒", "记得提醒", "别忘了提醒"])
        
        if any(keyword in message for keyword in trigger_keywords):
            logger.info(f"通过关键词匹配触发日程设定: {message}")
            return True
        
        # 使用LLM判断
        provider = await self._get_detection_provider(event)
        if not provider:
            return False
        
        prompt = f"""请判断以下用户消息是否是日程设定或提醒请求：
用户消息："{message}"

判断标准：
1. 用户明确表达想要被提醒某件事
2. 消息中包含时间相关的描述（如：明天、下午3点、10分钟后等）
3. 消息中包含要做的事情或要被提醒的内容

如果是日程/提醒请求，回复"是"，否则回复"否"。
只需回复"是"或"否"，不要包含其他内容。"""

        try:
            response = await provider.text_chat(prompt=prompt)
            result = response.completion_text.strip().lower()
            return result in ["是", "是的", "yes", "true"]
        except Exception as e:
            logger.error(f"LLM判断日程触发时出错: {e}")
            return False

    async def _extract_schedule_info(self, message: str, event: AstrMessageEvent) -> Optional[dict]:
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
{f'网络搜索补充信息：{search_info}' if search_info else ''}

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
            json_match = re.search(r'\{[^{}]*\}', result_text, re.DOTALL)
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
        user_schedules = [
            s for s in self.schedules.values() 
            if s.sender_id == sender_id and s.status == ReminderStatus.PENDING.value
        ]
        
        if not user_schedules:
            yield event.plain_result("您当前没有待执行的提醒。")
            return
        
        # 按触发时间排序
        user_schedules.sort(key=lambda x: x.trigger_time)
        
        result_lines = ["📋 您的提醒列表：\n"]
        for i, schedule in enumerate(user_schedules, 1):
            trigger_dt = datetime.fromtimestamp(schedule.trigger_time)
            time_str = trigger_dt.strftime("%m月%d日 %H:%M")
            result_lines.append(f"{i}. [{time_str}] {schedule.event_content}")
            result_lines.append(f"   ID: {schedule.id[:8]}...")
        
        yield event.plain_result("\n".join(result_lines))

    @callme_group.command("cancel")
    async def cancel_reminder(self, event: AstrMessageEvent, reminder_id: str = ""):
        """取消提醒
        
        用法: /callme cancel <提醒ID前缀>
        """
        if not reminder_id:
            yield event.plain_result("请提供要取消的提醒ID（可以只输入前几位）。\n用法: /callme cancel <ID前缀>")
            return
        
        sender_id = event.get_sender_id()
        
        # 查找匹配的日程
        matched = None
        for sid, schedule in self.schedules.items():
            if sid.startswith(reminder_id) and schedule.sender_id == sender_id:
                matched = schedule
                break
        
        if not matched:
            yield event.plain_result(f"未找到ID以 '{reminder_id}' 开头的提醒，或该提醒不属于您。")
            return
        
        # 取消定时任务
        if matched.id in self.timers:
            self.timers[matched.id].cancel()
            del self.timers[matched.id]
        
        # 更新状态
        matched.status = ReminderStatus.CANCELLED.value
        del self.schedules[matched.id]
        
        # 保存数据
        if self.config.get("data_persistence", True):
            await self._save_schedules()
        
        yield event.plain_result(f"✅ 已取消提醒：{matched.event_content}")

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
                message = message[len(prefix):].strip()
                break
        
        if not message:
            yield event.plain_result(self._get_help_text())
            return
        
        # 使用LLM提取信息
        schedule_info = await self._extract_schedule_info(message, event)
        
        if not schedule_info:
            yield event.plain_result("抱歉，我无法理解您的提醒请求。\n\n" + self._get_help_text())
            return
        
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
    
    async def _create_schedule(self, event: AstrMessageEvent, schedule_info: dict) -> dict:
        """创建日程"""
        sender_id = event.get_sender_id()
        
        # 检查数量限制
        max_reminders = self.config.get("max_reminders", 20)
        user_count = sum(1 for s in self.schedules.values() 
                        if s.sender_id == sender_id and s.status == ReminderStatus.PENDING.value)
        
        if user_count >= max_reminders:
            return {
                "success": False,
                "message": f"您已达到最大提醒数量限制({max_reminders})，请先取消一些提醒再试。"
            }
        
        # 解析时间
        time_str = schedule_info.get("time", "")
        trigger_time = await self._parse_time_string(time_str, event)
        
        if trigger_time is None:
            return {
                "success": False,
                "message": f"无法解析时间 '{time_str}'，请使用更明确的时间描述。"
            }
        
        if trigger_time <= time.time():
            return {
                "success": False,
                "message": "提醒时间必须是将来的时间。"
            }
        
        # 创建日程对象
        schedule_id = f"{sender_id}_{int(time.time() * 1000)}"
        schedule = ScheduleItem(
            id=schedule_id,
            unified_msg_origin=event.unified_msg_origin,
            sender_id=sender_id,
            sender_name=event.get_sender_name(),
            event_content=schedule_info.get("event", "未知事件"),
            trigger_time=trigger_time,
            created_at=time.time(),
            status=ReminderStatus.PENDING.value,
            raw_time_str=time_str,
            search_info=schedule_info.get("search_info", "")
        )
        
        # 存储日程
        self.schedules[schedule_id] = schedule
        
        # 创建定时任务
        delay = trigger_time - time.time()
        timer_task = asyncio.create_task(self._reminder_timer(delay, schedule_id))
        self.timers[schedule_id] = timer_task
        
        # 保存数据
        if self.config.get("data_persistence", True):
            await self._save_schedules()
        
        logger.info(f"已创建日程 {schedule_id}，将在 {delay:.0f} 秒后触发")
        
        return {
            "success": True,
            "schedule": schedule,
            "message": "日程创建成功"
        }

    async def _reminder_timer(self, delay: float, schedule_id: str):
        """定时器任务"""
        try:
            await asyncio.sleep(delay)
            
            if schedule_id in self.schedules:
                schedule = self.schedules[schedule_id]
                await self._send_reminder(schedule)
                
                # 更新状态
                schedule.status = ReminderStatus.TRIGGERED.value
                del self.schedules[schedule_id]
                
                if schedule_id in self.timers:
                    del self.timers[schedule_id]
                
                # 保存数据
                if self.config.get("data_persistence", True):
                    await self._save_schedules()
                    
        except asyncio.CancelledError:
            logger.info(f"提醒任务 {schedule_id} 已被取消")
        except Exception as e:
            logger.error(f"提醒任务 {schedule_id} 执行出错: {e}")

    async def _send_reminder(self, schedule: ScheduleItem):
        """发送提醒消息"""
        try:
            umo = schedule.unified_msg_origin
            
            # 生成提醒消息
            message = await self._generate_reminder_message(schedule)
            
            # 构建消息链
            chain = [Comp.Plain(message)]
            
            # 发送文本消息
            message_chain = MessageChain()
            message_chain.chain = chain
            await self.context.send_message(umo, message_chain)
            
            logger.info(f"已发送提醒消息: {message[:50]}...")
            
            # TTS语音播报
            if self.config.get("enable_tts", False):
                await self._send_tts_reminder(schedule, message)
                
        except Exception as e:
            logger.error(f"发送提醒消息失败: {e}")

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
            
            # 生成语音
            # 注意：这里需要根据AstrBot实际的TTS API来调整
            audio_data = await tts_provider.text_to_speech(message)
            
            if audio_data:
                # 保存语音文件
                audio_path = os.path.join(self.data_dir, f"tts_{schedule.id}.wav")
                with open(audio_path, 'wb') as f:
                    f.write(audio_data)
                
                # 发送语音消息
                chain = [Comp.Record(file=str(audio_path), url=str(audio_path))]
                message_chain = MessageChain()
                message_chain.chain = chain
                await self.context.send_message(schedule.unified_msg_origin, message_chain)
                
                # 清理临时文件
                try:
                    os.remove(audio_path)
                except:
                    pass
                    
        except Exception as e:
            logger.error(f"TTS语音播报失败: {e}")

    # ==================== 时间解析 ====================
    
    async def _parse_time_string(self, time_str: str, event: AstrMessageEvent) -> Optional[float]:
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
        minute_match = re.search(r'(\d+)\s*分钟', time_str)
        if minute_match:
            minutes = int(minute_match.group(1))
            return (now + timedelta(minutes=minutes)).timestamp()
        
        # X小时后
        hour_match = re.search(r'(\d+)\s*小时', time_str)
        if hour_match:
            hours = int(hour_match.group(1))
            return (now + timedelta(hours=hours)).timestamp()
        
        # 使用LLM解析复杂时间
        return await self._llm_parse_time(time_str, event)

    async def _llm_parse_time(self, time_str: str, event: AstrMessageEvent) -> Optional[float]:
        """使用LLM解析复杂时间表达"""
        provider = await self._get_main_provider(event)
        if not provider:
            return None
        
        current_time = datetime.now()
        time_presets = self.config.get("default_time_points", {})
        
        prompt = f"""请将以下时间描述转换为具体的日期时间。

时间描述："{time_str}"
当前时间：{current_time.strftime("%Y年%m月%d日 %H:%M:%S")} 星期{['一','二','三','四','五','六','日'][current_time.weekday()]}

默认时间设置：
- 早上：{time_presets.get("morning", "08:00")}
- 中午：{time_presets.get("noon", "12:00")}
- 下午：{time_presets.get("afternoon", "14:00")}
- 晚上：{time_presets.get("evening", "19:00")}
- 深夜：{time_presets.get("night", "22:00")}
- 星期X默认时间：{time_presets.get("default_weekday", "09:00")}

请直接返回目标时间，格式为：YYYY-MM-DD HH:MM:SS
只返回时间，不要包含其他内容。如果无法解析，返回"无法解析"。"""

        try:
            response = await provider.text_chat(prompt=prompt)
            result = response.completion_text.strip()
            
            if "无法解析" in result:
                return None
            
            # 尝试解析返回的时间
            time_match = re.search(r'(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2}):?(\d{2})?', result)
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
    
    async def _generate_confirmation_response(self, schedule_info: dict, event: AstrMessageEvent) -> str:
        """生成日程确认回复"""
        time_str = schedule_info.get("time", "指定时间")
        event_content = schedule_info.get("event", "未知事件")
        
        if not self.config.get("enable_personality", True):
            return f"✅ 好的，我会在{time_str}提醒您：{event_content}"
        
        # 获取人格设定
        system_prompt = await self._get_persona_prompt(event.unified_msg_origin)
        
        provider = await self._get_main_provider(event)
        if not provider:
            return f"✅ 好的，我会在{time_str}提醒您：{event_content}"
        
        prompt = f"""用户刚刚设置了一个日程提醒，请生成一条友好的确认消息。

日程信息：
- 时间：{time_str}
- 事件：{event_content}

请根据以下角色设定生成回复：
{system_prompt if system_prompt else "你是一个贴心的助手"}

要求：
1. 确认已记录日程
2. 语气自然、友好
3. 简洁明了，不超过50字
"""

        try:
            response = await provider.text_chat(prompt=prompt)
            return response.completion_text.strip()
        except Exception as e:
            logger.error(f"生成确认回复失败: {e}")
            return f"✅ 好的，我会在{time_str}提醒您：{event_content}"

    async def _generate_reminder_message(self, schedule: ScheduleItem) -> str:
        """生成提醒消息"""
        if not self.config.get("enable_personality", True):
            return f"⏰ 提醒：{schedule.sender_name}，您之前设定的提醒时间到了！\n📌 事项：{schedule.event_content}"
        
        # 获取人格设定
        system_prompt = await self._get_persona_prompt(schedule.unified_msg_origin)
        
        provider = await self._get_main_provider_by_umo(schedule.unified_msg_origin)
        if not provider:
            return f"⏰ 提醒：{schedule.sender_name}，您之前设定的提醒时间到了！\n📌 事项：{schedule.event_content}"
        
        prompt = f"""现在需要提醒用户一个之前设定的事项，请生成一条友好的提醒消息。

提醒信息：
- 用户名：{schedule.sender_name}
- 事项：{schedule.event_content}
- 原始设定时间描述：{schedule.raw_time_str}
{f'- 相关信息：{schedule.search_info}' if schedule.search_info else ''}

请根据以下角色设定生成提醒消息：
{system_prompt if system_prompt else "你是一个贴心的助手"}

要求：
1. 称呼用户或使用适当的提醒语
2. 清楚说明要提醒的事项
3. 语气自然、友好
4. 可以适当添加emoji
"""

        try:
            response = await provider.text_chat(prompt=prompt)
            return response.completion_text.strip()
        except Exception as e:
            logger.error(f"生成提醒消息失败: {e}")
            return f"⏰ 提醒：{schedule.sender_name}，您之前设定的提醒时间到了！\n📌 事项：{schedule.event_content}"

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
                search_keyword = result.replace("需要搜索：", "").replace("需要搜索:", "").strip()
                
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
