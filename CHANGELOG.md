# Changelog

All notable changes to this project will be documented in this file.

## [v3.2.4] - 2026-02-02

### Fixed
- **戳一戳功能完善**：
  - 修复v3.2.3中戳一戳API调用被注释（TODO标记）导致功能未实际生效的问题
  - 参考[pokepro插件](https://github.com/Zhalslar/astrbot_plugin_pokepro)重构实现
  - 重构方法名：`_send_poke(umo, target_id)` → `_send_poke_reminder(schedule)`
  - 使用正确的client获取方式：`platform.get_client()` 或 `platform.client`
  - 实现真正的API调用：`await client.group_poke()` 和 `await client.friend_poke()`
  
- **LLM监控超时修复**：
  - 修复建立提醒别人类日程时LLM监控超时的问题
  - 根本原因：`_get_at_target()` 调用未添加超时保护，获取群成员列表时可能较慢
  - 解决方案：为 `_get_at_target()` 添加10秒超时保护（`asyncio.wait_for`）
  - 超时时默认为提醒自己，避免阻塞整个流程
  - 添加详细的异常处理和日志记录
  
### Verified
- **成员昵称映射功能**：
  - 经代码审查确认该功能已在v3.2.3及之前版本中正常实现
  - `_get_user_display_name()` 方法在 `_generate_reminder_message()` 和 `_generate_confirmation_response()` 中被正确调用
  - 配置格式：`user_alias: ["用户ID,称呼"]`（按逗号分隔，只分割第一个逗号）
  - 优先使用配置的别名，未配置则使用从API获取的真实昵称

### Technical Details
- 超时策略：判断请求(10s) → 提取信息(15s) → **获取@对象(10s)** → 创建日程(20s) → 生成回复(10s)
- 支持群聊和私聊环境的戳一戳功能
- 仅aiocqhttp平台支持戳一戳，其他平台自动跳过

---

## [v3.2.3] - 2026-02-02

### Fixed
- **[关键修复] @成员提醒卡死问题**：
  - 修复导入层错误：改用Core层的消息组件替代API层
  - 修复At组件构造：使用正确的int类型参数
  - 重写_get_at_target函数：支持异步获取真实昵称
  
### Added
- **戳一戳功能**：
  - 添加_send_poke方法支持戳一戳提醒（aiocqhttp平台）
  - 完整提醒流程：戳一戳 → @+文本 → TTS语音
  - 添加enable_poke配置项（默认开启）
  
### Changed
- **真实昵称获取**：
  - 群聊环境：通过get_group_member_list API获取群名片/昵称
  - 私聊环境：通过get_stranger_info API获取用户昵称
  - 所有_get_at_target调用改为await异步调用

---

## [v3.2.2] - 2026-02-02

### Fixed
- **[致命修复] LLM监控卡死问题**：
  - 修复创建“提醒别人”类型日程时AstrBot控制界面卡死30秒问题
  - 根本原因：`_extract_schedule_info`、`_create_schedule` 等函数缺少异常捕获
  - 在所有超时和异常路径上添加 `event.stop_event()` 调用
  - 确保事件不会继续传播导致系统等待超时

### Improved
- **增强日志追踪**：
  - 在LLM监控流程的每个关键步骤添加详细日志
  - 方便追踪处理进度和定位问题
  - 日志包括：步骤开始、提取结果、@对象信息、创建结果等
  - 异常情况使用 `exc_info=True` 输出完整堆栈

- **优化异常处理**：
  - 所有 `asyncio.wait_for` 超时后都调用 `event.stop_event()`
  - 所有异常分支都添加用户友好的错误提示
  - 确保任何情况下都不会阻塞事件循环

## [v3.2.1] - 2026-02-02

### Fixed
- **[致命修复] 初始化缺失**：
  - 修复 `_message_dedup_cache` 未在 `__init__` 中初始化导致的 `AttributeError`
  - 测试场景1、2报错：`'ReminderPlugin' object has no attribute '_message_dedup_cache'`
  - 现已在构造函数中正确初始化为空字典

### Added
- **新增 `/callme at` 指令**：
  - 用于通过指令模式创建一次性提醒
  - 支持相对时间（`5分钟后`、`半小时后`）和绝对时间（`明天早上9点`）
  - 使用示例：`/callme at 10分钟后 记得喝水`
  - 解决测试场景4反馈的功能缺失问题

### Improved
- 指令菜单现支持完整功能：
  - `/callme at` - 一次性提醒
  - `/callme every` - 循环提醒
  - `/callme list` - 查看列表
  - `/callme cancel` - 取消提醒

## [v3.2.0] - 2026-02-02

### Fixed
- **[关键修复] LLM监控模式优化**：
  - 添加消息去重机制，防止多群同时收到同一消息导致重复处理（60秒缓存窗口）
  - 优化关键词匹配逻辑，关键词成功匹配后直接确认为日程请求，跳过LLM二次判断，大幅减少LLM调用次数和延迟
  - 移除冗余的LLM判断代码，降低超时风险
  
- **[关键修复] 循环日程功能修复**：
  - 修复了循环日程触发后无法自动重新调度的问题
  - 在 `_reminder_timer` 中添加循环日程检测逻辑，触发后自动计算并设置下次触发时间
  - 实现 `_calculate_next_trigger` 方法，支持每天(daily)、每周(weekly)、每月(monthly)、每年(yearly)的准确计算
  - 循环日程现在会正确记录 `trigger_count` 计数，并在数据库中更新下次触发时间
  - 修复了月末日期在不同月份的处理逻辑（如31号在2月自动调整为28/29号）

### Improved
- 代码清理：移除了 `_is_schedule_trigger` 中 36 行不可达的死代码
- 日志优化：为循环日程重调度添加详细日志记录

## [v3.1.2] - 2026-01-30

### Fixed
- 修复了 "提醒@某人" 格式指令无法被插件捕获，而被系统默认 reminder 工具抢占的问题。
- 扩大了默认触发关键词范围，新增 "提醒"、"帮我提醒" 等。

## [v3.1.1] - 2026-01-30

### Fixed
- 修复了旧版本数据库升级到新版本时可能发生的 `sqlite3.OperationalError: no such column: recurrence_type` 崩溃问题。
- 优化了数据库迁移逻辑，增强了对列缺失情况的检测和自动修复能力。

## [v3.1.0] - 2026-01-30

### Added
- 增加了版本描述信息。

## [v3.0.0] - 2026-01-30

### Added
- **循环日程支持**：新增每天、每周、每月、每年的循环提醒功能。
- **人格化系统优化**：提醒消息现在可以根据设定的人格进行风格化生成。
- **戳一戳交互**：支持通过戳一戳 Bot 头像来触发特定交互。
- 数据库结构升级，新增 `recurrence_type`, `recurrence_value` 等字段。

## [v2.2.1] - 2026-01-30

### Fixed
- 修复了一些已知的小问题。

## [v2.2.0] - 2026-01-30

### Added
- 初始版本功能完善。
