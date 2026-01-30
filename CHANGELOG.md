# Changelog

All notable changes to this project will be documented in this file.

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
