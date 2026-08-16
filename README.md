# Dududa 2.0 AstrBot 插件（薄壳）

## 仓库定位

**本仓库 = 生产部署入口（AstrBot 插件本体）**，只包含适配层与装配逻辑，不包含 Agent 核心实现：

- `main.py`：AstrBot 事件/命令适配、依赖装配、兼容 re-export（薄壳，保持精简）
- `_router.py`：早期模型路由（已被核心 ModelRouter 取代，保留兼容）
- `metadata.yaml`：插件元信息（AstrBot 读取）
- `data/`：运行时数据（画像/记忆/配置），不提交

**核心运行时在 [dududa20-prototype](https://github.com/Shaywww/dududa20-prototype)**（Agent Runtime：领域模型 / 13 阶段 Pipeline / MCP 工具 / LLM 路由 / 记忆画像 / 控制台 / 运维）。本插件通过固定路径加载其代码，两个仓库配合部署：

- 改核心逻辑（感知/决策/规划/工具/记忆/路由）→ 提交 prototype 仓库
- 改插件装配、AstrBot 适配、管理命令 → 提交本仓库

## 更新日志

> 按主题分组，最新改动在前。

### 0.2.6 天气地点一致性

- 识别“我现在在/目前在/人在某地”等当前位置表达，并写入结构化用户画像
- 天气回复始终使用用户查询地点；最近气象数据点只作为来源，不再冒充用户所在城市或区县
- 无显式地点且画像也没有当前位置时主动询问，不再默认猜测合肥或其他城市

### 0.2.5 表情回复事实约束

- 表情包只回应画面与上下文能够支持的情绪，不再臆测“听到八卦”“被谁吓到”等具体情节
- 缺少上下文时优先自然询问，不用虚构原因换取俏皮感

### 0.2.4 图片与表情包识别

- 读取 NapCat 原始 OneBot 图片段中的 `summary`、`emoji_id`、`emoji_package_id`、`key` 等表情信号
- QQ 商城表情和 `mface` 优先按聊天情绪自然接话，不再默认逐项描述、强制 OCR
- 普通图片与无法从平台确定类型的梗图由视觉模型静默分类；用户明确要求识图、OCR 或分析时仍完整执行
- 修复 AstrBot 未生成标准图片组件时，无法从原始消息嵌套 `data.url` 读取媒体的兼容问题

### 0.2.3 颜文字风格校正

- 默认人格改为只使用 `(≧▽≦)`、`^^~` 等纯文本颜文字
- 提示词与最终投递层同时禁止并过滤 `😋` 等 Unicode 彩色 Emoji
- 保留温度、评分等事实符号，不影响搜索答案内容

### 0.2.2 DeepSeek 托管联网搜索

- 官方 DeepSeek Responses API 的服务端 `web_search` 作为主搜索链路
- 必须同时取得最终答案、实际网页来源并通过相关性校验，否则自动回退 Bing RSS
- 只向回复流程传递最终检索摘要，过滤推理过程、搜索进度和跟踪参数

### 0.2.1 回答可靠性

- 取消在用户第一次正常提问前自动插入整段使用说明；需要帮助时由用户主动发送 `/dududa_help`
- 慢任务进度提示阈值从 3 秒调整为 5 秒，减少普通问答中的打扰
- 配合核心运行时修复联网搜索空参数、工具失败后继续猜测及内部占位符泄漏

### 0.2.0 用户体验

- 慢任务 3 秒后主动提示当前阶段，支持 `/dududa_cancel` 取消；同一用户/会话只运行一个任务
- 首次主动私聊附带一次极简引导，不在加好友时推送，也不在群聊打扰
- `/dududa_memory` 支持查看本人记忆、删除单条/全部、暂停写入和临时无记忆对话
- `/dududa_subscribe` 提供显式订阅、退订和免打扰；默认不订阅、每日最多 1 条
- 管理员推送必须先预览再确认，并在发送前再次检查退订、免打扰和频率限制
- `/dududa_help` 根据实际 Provider 健康状态生成帮助；模型双线路失败返回可检索错误编号

常用命令：

```text
/dududa_help
/dududa_cancel
/dududa_memory list|active|paused|temporary|delete <ID>|clear
/dududa_subscribe add 更新|remove 更新|quiet 22:30-08:00
```

### 感知与规划

- 默认启用模型感知（DUDUDA_PERCEPTION_MODEL=1）
- 感知信号携带可用能力清单，直接输出 tool_plan 供规划阶段合并（省一次 LLM 调用）

### 装配与接线

- 接线用户画像 ProfileStore、群策略 GroupPolicyStore、确认存储 ConfirmationStore（文档 2.4.6 / 2.5.2 / 2.5.9）
- 接线媒体仓库 media_repo（图片暂存）与运行时限额 RuntimeLimits
- 接线消息幂等注册表 + 两阶段投递回执（after_message_sent 钩子，文档 2.3.15-2.3.16）
- 接线 hybrid OCRenderer（DUDUDA_HYBRID_RENDER）与风格存储 dududa_style 命令（文档 2.5.8）
- run_id/trace_id 贯穿 Main 全部 LLM/记忆包装调用

### 兼容与回退

- OpenAI 兼容 base URL 统一规范化到 /v1（宿主根路径是管理面板）
- 保持薄壳 <500 行，标准化逻辑收敛到 provider 层
