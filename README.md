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
