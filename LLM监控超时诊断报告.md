# LLM监控超时问题诊断报告

## 问题现象

### 用户反馈
```
消息：axx一分钟后提醒 @某人 吃饭
结果：30秒超时，日程创建失败
日志：[15:03:36] ⏰ 系统等待超时：30秒
```

### 消息组件分析
```
Plain('axx一分钟后提醒') + At(qq=3342496519) + Plain('吃饭')
```

## 关键发现

### 1. 提醒自己 vs 提醒别人的区别

**提醒自己（成功）**:
- 消息格式：`"提醒我 一分钟后 吃饭"`
- 关键词：`"提醒我"`（明确的触发词）
- 消息组件：只有Plain文本，没有At组件
- 处理流程：关键词匹配 → LLM判断 → 提取信息 → 创建日程 ✅

**提醒别人（失败）**:
- 消息格式：`"一分钟后提醒 @某人 吃饭"`
- 关键词：`"提醒"`（单独的词，不如"提醒我"明确）
- 消息组件：Plain + **At组件** + Plain
- 处理流程：关键词匹配 → LLM判断 → **卡在这里** ❌

### 2. 超时发生的位置

根据日志时间戳分析：
```
15:03:06 - 收到消息
15:03:36 - 系统等待超时（30秒后）
```

我们设置的超时时间：
- 步骤1（判断请求）：10秒
- 步骤2（提取信息）：15秒
- 步骤3（获取@对象）：10秒
- 步骤4（创建日程）：20秒
- 步骤5（生成回复）：10秒

**30秒超时 ≠ 我们的任何一个超时设置**

结论：**这是AstrBot系统层面或LLM Provider的超时，不是我们代码的超时！**

### 3. 为什么提醒别人会超时？

#### 可能原因1：LLM Provider初始化问题
```
[15:03:36] [INFO] ⏳ 系统正在初始化中，LLM请求将跳过
```

这说明**系统还在初始化中**，LLM Provider可能未就绪！

#### 可能原因2：消息格式导致LLM混淆
- 提醒自己：`"提醒我 1分钟后 吃饭"` → 结构清晰
- 提醒别人：`"axx 1分钟后提醒 [At] 吃饭"` → 结构复杂（时间在前，关键词在中间，At组件）

LLM可能因为：
1. "axx"是什么？（可能是用户名或前缀）
2. At组件在message_str中如何表示？
3. 时间在关键词前面，不符合常见模式

#### 可能原因3：_get_main_provider() 返回None
在 `_is_schedule_trigger` 和 `_extract_schedule_info` 中，如果无法获取provider，会怎样？

让我检查代码：

## 核心问题定位

### 真正的根因

经过分析，我发现**30秒超时来自AstrBot系统**，而不是我们的代码。关键证据：

1. **系统初始化检查**：
   ```
   [15:03:36] ⏳ 系统正在初始化中，LLM请求将跳过
   ```

2. **我们的代码逻辑**：
   - `_is_schedule_trigger` 只做关键词匹配，**不调用LLM**
   - 应该在几毫秒内返回True
   - 不会导致30秒超时

3. **真正卡住的地方**：
   - 步骤2：`_extract_schedule_info` 调用 `provider.text_chat()`
   - 如果系统正在初始化，provider可能阻塞等待
   - AstrBot有30秒的全局等待超时机制

### 为什么提醒自己可以成功？

可能的解释：
1. **测试时间不同**：提醒自己时系统已完全初始化，提醒别人时系统刚启动
2. **消息复杂度**：提醒别人的消息更复杂，LLM处理时间更长，更容易触发超时
3. **随机性**：LLM响应时间有波动，复杂消息更容易超时

## 解决方案

### 方案1：优化 _is_schedule_trigger（已实现）

当前实现已经优化为**纯关键词匹配**，不调用LLM：
```python
async def _is_schedule_trigger(self, message: str, event: AstrMessageEvent) -> bool:
    # 只做关键词匹配，不调用LLM
    trigger_keywords = [...]
    clean_msg = re.sub(r'\[CQ:.*?\]', '', message)
    clean_msg = re.sub(r'@\S+', '', clean_msg)
    if any(keyword in message or keyword in clean_msg for keyword in trigger_keywords):
        return True
    return False
```

### 方案2：添加系统初始化检查

在调用LLM之前，检查系统是否已初始化：
```python
# 在 _extract_schedule_info 开始时
if not self.context.is_initialized():
    logger.warning("[LLM监控模式] 系统尚未初始化完成，跳过LLM调用")
    return None
```

### 方案3：优化消息预处理

确保message_str包含完整信息：
```python
# 重构消息文本获取逻辑
def _get_full_message_text(self, event: AstrMessageEvent) -> str:
    """获取完整的消息文本（包括At组件的替代文本）"""
    parts = []
    for comp in event.message_obj.message:
        if isinstance(comp, Plain):
            parts.append(comp.text)
        elif isinstance(comp, At):
            # At组件用占位符表示
            parts.append(f"@{comp.qq}")
    return ''.join(parts)
```

### 方案4：调整超时策略

如果系统初始化慢，可能需要更长的超时时间：
```python
# 步骤2：提取信息
timeout=30.0  # 从15秒增加到30秒
```

但这不是好方案，因为用户会等太久。

### 方案5：添加重试机制

如果LLM调用超时，可以重试一次（使用更短的prompt）：
```python
try:
    result = await asyncio.wait_for(llm_call(), timeout=15.0)
except asyncio.TimeoutError:
    # 使用简化版prompt重试
    result = await asyncio.wait_for(llm_call_simple(), timeout=10.0)
```

## 推荐实施方案

### 立即修复（v3.2.6）

1. **添加详细诊断日志**（已完成✅）
   - 记录每个步骤的耗时
   - 打印message_str和消息组件
   - 输出关键词匹配详情

2. **添加系统初始化检查**
   ```python
   # 在on_message开始处
   if hasattr(self.context, 'is_ready') and not self.context.is_ready():
       logger.debug("[LLM监控模式] 系统初始化中，跳过处理")
       return
   ```

3. **优化provider获取逻辑**
   ```python
   provider = await self._get_main_provider(event)
   if not provider:
       logger.warning("[LLM监控模式] 无法获取LLM Provider，跳过")
       return None
   ```

4. **改进消息文本构建**
   - 确保At组件被正确处理
   - 使用完整的消息文本进行LLM分析

### 测试建议

1. **测试系统启动后立即发送提醒**
   - 启动AstrBot
   - 等待10秒（系统初始化中）
   - 发送："提醒我 1分钟后 测试"
   - 预期：可能超时或被跳过

2. **测试系统完全启动后发送提醒**
   - 启动AstrBot
   - 等待60秒（系统完全初始化）
   - 发送："一分钟后提醒 @某人 吃饭"
   - 预期：应该成功

3. **对比测试不同消息格式**
   - 格式1：`"提醒 @某人 1分钟后 吃饭"`
   - 格式2：`"1分钟后提醒 @某人 吃饭"`
   - 格式3：`"提醒我 1分钟后 吃饭"`
   - 观察哪种格式成功率更高

## 临时解决方案

在修复完成前，用户可以：

1. **使用指令模式**：
   ```
   /callme 1分钟后 @某人 吃饭
   ```
   指令模式不依赖LLM监控，更稳定

2. **等待系统完全启动**：
   - 启动AstrBot后等待1-2分钟
   - 确保所有插件初始化完成
   - 再发送提醒请求

3. **使用更明确的关键词**：
   ```
   提醒 @某人 1分钟后 吃饭  （推荐）
   ```
   而不是：
   ```
   1分钟后提醒 @某人 吃饭  （不推荐）
   ```

## 下一步

1. 收集用户日志，确认耗时瓶颈
2. 检查AstrBot的初始化流程
3. 考虑将LLM监控改为完全异步（不阻塞事件循环）
4. 添加降级策略（LLM超时时使用规则匹配）
