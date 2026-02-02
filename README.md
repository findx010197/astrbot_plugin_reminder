# AstrBot 智能日程提醒插件

一个功能丰富的 AstrBot 日程提醒插件，支持 LLM 监控与指令双模式创建日程，基于人格生成提醒话语。

## ✨ 功能特点

### 双模式创建日程

**LLM监控模式：**
- 自动监控用户消息，通过 LLM 判断是否为日程设定请求
- 支持关键词快速匹配（如"提醒我"、"设置提醒"等）
- 智能提取时间和事件内容
- 仅支持提醒自己（简单可靠）

**指令模式：**
- `/callme <时间> <事件>` - 快速创建提醒
- `/提醒我 <时间> <事件>` - 中文别名
- `/callme list` - 查看提醒列表
- `/callme cancel <ID>` - 取消提醒

### 智能时间解析

支持多种时间表达方式：
- 快捷词：等一下、稍后、半小时后（可配置具体分钟数）
- 相对时间：5分钟后、2小时后
- 自然语言：明天早上、下周一下午3点、12月25日上午10点
- 时段默认时间可在配置中自定义

### 人格化消息生成

- 创建日程时根据当前人格生成确认回复
- 触发提醒时根据人格生成友好的提醒话语
- 可通过配置关闭人格化功能

### 其他功能

- **TTS语音播报**（可选）：提醒触发时可同时发送语音消息
- **网络搜索**（可选）：当日程涉及需要查询的信息时，可调用搜索功能辅助创建
- **数据持久化**：日程数据持久保存，重启后自动恢复

## 📦 安装

### 通过 AstrBot 插件市场安装（推荐）

在 AstrBot 管理面板的插件市场中搜索 `astrbot_plugin_reminder` 进行安装。

### 手动安装

```bash
# 进入 AstrBot 插件目录
cd /path/to/astrbot/data/plugins

# 克隆仓库
git clone https://github.com/findx010197/astrbot_plugin_reminder.git
```

## ⚙️ 配置说明

安装后可在 AstrBot 管理面板的插件配置中进行设置：

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `llm_provider` | 主LLM提供商 | 使用当前会话默认 |
| `schedule_detection_llm` | 日程检测专用LLM | 同上 |
| `tts_provider` | TTS语音合成提供商 | 不启用 |
| `enable_llm_monitor` | 是否启用LLM监控模式 | true |
| `enable_tts` | 是否启用语音播报 | false |
| `enable_web_search` | 是否启用网络搜索 | false |
| `enable_personality` | 是否启用人格化回复 | true |
| `max_reminders` | 每用户最大提醒数量 | 20 |
| `quick_time_presets` | 快捷时间预设 | 见下方 |
| `default_time_points` | 各时段默认时间点 | 见下方 |
| `trigger_keywords` | 触发关键词列表 | ["提醒我", ...] |
| `user_alias` | 用户昵称映射 | [] |
| `data_persistence` | 是否持久化存储 | true |

### 用户昵称映射

```json
{
  "user_alias": [
    "1498976088,小明",
    "123456789,老板",
    "987654321,小王"
  ]
}
```

格式：`"用户QQ号,昵称"`

此昵称将在提醒消息中使用，格式：`@用户 昵称 时间到了~`

### 快捷时间预设

```json
{
  "wait_a_moment": 5,   // "等一下"对应5分钟
  "short_while": 10,    // "稍后"对应10分钟
  "half_hour": 30       // "半小时后"对应30分钟
}
```

### 默认时间点

```json
{
  "morning": "08:00",      // 早上
  "noon": "12:00",         // 中午
  "afternoon": "14:00",    // 下午
  "evening": "19:00",      // 晚上
  "night": "22:00",        // 深夜
  "default_weekday": "09:00"  // 星期X
}
```

## 📝 使用示例

### LLM监控模式
```
用户: 提醒我明天早上开会
Bot: 好的，我会在明天早上8点提醒您开会哦~

（到时触发）
Bot: [戳一戳]
     @用户 小明 早上8点到了，该去开会啦~
```

### 指令模式
```
用户: /callme 10分钟后 喝水
Bot: ✅ 一次性提醒已创建
     📌 喝水
     ⏰ 2月2日 15:40
     🏷️ R0001

用户: /callme list
Bot: 📋 您的提醒列表：
     1. [02/02 15:40] 喝水
        ID: R0001
```

## 🔧 环境要求

- AstrBot >= 4.5.0
- Python >= 3.9

本插件所有依赖均由 AstrBot 核心提供，无需额外安装。

## 📄 许可证

MIT License

## 🤝 贡献

欢迎提交 Issue 和 Pull Request！

## 📮 联系

如有问题，请在 [GitHub Issues](https://github.com/findx010197/astrbot_plugin_reminder/issues) 中反馈。
