# 🚀 MakeCode · 项目说明

🌐 语言切换：**简体中文** | [English](README_en.md) | [📦 Releases](https://github.com/cockmake/MakeCode/releases)

> 一个多代理命令行编排器。
> 
> 支持任务拓扑规划、并发子代理委派、技能加载、文件/终端工具调用，以及长会话压缩。

---

## 1. 项目简介

MakeCode 是一个面向工程任务的 Agent CLI。它采用“编排器（Orchestrator）+ 子代理（Teammates）”模式：

- 主代理负责理解需求、规划任务、调度工具、汇总结果。
- TaskManager 负责维护任务依赖关系与可执行前沿。
- Team 系统负责并发唤醒子代理执行可并行任务。
- Skills 系统负责按需加载领域技能说明。
- Memory 模块负责在长会话下压缩上下文并保存转录。

这个项目的目标不是只回答问题，而是让代理具备**可规划、可执行、可追踪、可扩展**的工程工作流能力。

---

## 🖼️ 效果展示

<table>
<tr>
<td align="center"><img src="images/1.png" width="300"/></td>
<td align="center"><img src="images/2.png" width="300"/></td>
</tr>
<tr>
<td align="center"><img src="images/3.png" width="300"/></td>
<td align="center"><img src="images/4.png" width="300"/></td>
</tr>
<tr>
<td align="center"><img src="images/5.png" width="300"/></td>
<td align="center"><img src="images/6.png" width="300"/></td>
</tr>
</table>

---

## 2. 当前能力

### 2.1 编排器主循环（`main.py`）

- 使用 OpenAI `responses.create(...)` 发起多轮对话。
- 自动处理模型输出的工具调用。
- 聚合以下工具集：
  - File / Terminal 工具
  - Skills 工具
  - Memory 工具
  - TaskManager 工具
  - Team 工具
- 支持 Rich / tqdm / 纯终端三种输出降级显示。
- 启动时展示终端环境，并在上下文过长时触发压缩。

### 2.2 工作目录与环境初始化（`init.py`）

MakeCode 采用严格的工作区（Workspace）隔离机制。所有相对路径、环境变量和技能库加载均以用户当前选择的 **工作目录（WORKDIR）** 为基准，而非 Agent 源码所在目录。

- **环境变量 (`.env`) 加载**：启动时，系统会自动在当前选定的 `WORKDIR` 下寻找 `.env` 文件。如果读取到的环境变量与系统现有的环境变量冲突，CLI 会弹出交互式提示，让用户决定是否覆盖。
- **技能库 (`skills/`) 加载**：系统会严格从 `WORKDIR/skills` 目录中扫描并加载所有的自定义技能（`SKILL.md`）。这样可以确保不同的工程项目可以使用其专属的技能配置，互不干扰。
- 支持交互式选择工作区目录（支持当前目录/自定义目录）。
- **新增** 支持交互式选择底层的接口规范标准：
  - `Chat Completions API`（标准格式，适用于接入大多数开源模型如 DeepSeek、Ollama 等）。
  - `Responses API`（内测定制格式，原生兼容）。
- 初始化 OpenAI 客户端，读取：
  - `OPENAI_API_KEY`
  - `OPENAI_BASE_URL`
  - `MODEL_ID`

### 2.3 文件与终端工具（`utils/common.py`）

提供以下基础执行能力：

- `RunRead`：读取文件，可指定行号范围。
- `RunWrite`：仅用于新建并写入文件（目标文件不存在时）。
- `RunEdit`：用于修改已存在文件的指定行范围（调用前必须先 `RunRead` 确认内容）。
- `RunGrep`：按正则在目标目录内搜索文本文件。
- `RunTerminalCommand`：执行非交互式终端命令。

实现细节：

- 文件访问受工作区边界保护，防止路径逃逸。
- 终端类型在启动时自动检测并固定。
- Windows 优先 `pwsh` / `powershell` / `cmd`，POSIX 优先 `bash` / `zsh` / `sh`。
- 终端命令默认超时为 120 秒。

### 2.4 任务管理（`utils/tasks.py`）

TaskManager 提供：

- `CreateTask`
- `UpdateTaskStatus`
- `UpdateTaskDependencies`
- `GetTask`
- `GetRunnableTasks`
- `GetTaskTable`

关键特性：

- 任务状态支持：`pending` / `in_progress` / `completed`
- 活跃任务执行 DAG 校验，避免循环依赖。
- 可执行任务定义为：状态为 `pending` 且所有依赖均已完成。
- 每次运行的任务计划会写入工作区 `.makecode/tasks/`。

### 2.5 并发子代理（`utils/teams.py`）

Team 模块支持：

- 仅接受来自最新 `GetRunnableTasks` 的任务进行委派。
- 用线程池并发运行多个子代理。
- 子代理执行前自动将计划任务置为 `in_progress`。
- 执行完成后回写任务状态。
- 为每个子代理保存独立 JSONL trace。
- 汇总本轮所有子代理报告，返回统一报告文本。

运行过程会生成：

- `.makecode/team/task_history_{session_id}.json`
- `.makecode/team/runs/<run_id>/..._trace.jsonl`

### 2.6 技能系统（`utils/skills.py`）

支持：

- `ListSkills`：列出可用技能及简述
- `LoadSkill`：加载某个技能全文

当前仓库内置技能：

- `pdf`
- `code-review`

技能存放位置：`skills/<name>/SKILL.md`

### 2.7 会话压缩（`utils/memory.py`）

- 提供 `Compact` 工具用于压缩历史对话。
- 自动保存压缩前转录到 `.makecode/transcripts/`。
- 对工具结果进行轻量清理（`micro_compact`），保留最近结果。
- 调用模型对历史进行摘要后再重建上下文。

### 2.8 子代理 Todo 工具（`tools/todo.py`）

子代理内部可使用 `TodoUpdate` 工具维护一个简易待办列表，用于多步骤任务跟踪。

---

## 3. 项目结构与架构

### 3.1 目录结构

```text
Agent/
├─ main.py                  # 编排器主循环与 CLI 交互入口
├─ init.py                  # .env 加载、工作区选择、OpenAI 客户端初始化
├─ requirements.txt         # 项目依赖
├─ README.md
├─ README_en.md
├─ tools/
│  └─ todo.py               # 子代理内部 Todo 管理工具
├─ utils/
│  ├─ llm_client.py         # LLM 标准适配器 (Chat vs Response) 
│  ├─ common.py             # 文件/终端/搜索等基础工具
│  ├─ tasks.py              # TaskManager 任务拓扑与状态管理
│  ├─ teams.py              # 子代理并发委派与执行日志
│  ├─ skills.py             # 技能发现与加载
│  └─ memory.py             # 会话压缩与转录保存
├─ skills/
│  ├─ pdf/
│  │  └─ SKILL.md
│  └─ code-review/
│     └─ SKILL.md
└─ build/                   # 打包产物/构建相关文件（若存在）
```

运行中还会生成：

- `.makecode/tasks/`：任务计划 JSON
- `.makecode/team/`：子代理历史与运行日志
- `.makecode/transcripts/`：压缩前会话转录

### 3.2 架构图（Mermaid）

```mermaid
flowchart TD
    U[用户 / CLI Input] --> O[Orchestrator<br/>main.py]
    O --> AC[llm_client.py<br/>Adapter]
    AC --> M[OpenAI 标准 / Responses API]
    O --> I[初始化与环境<br/>init.py]

    O --> C[File / Terminal Tools<br/>utils/common.py]
    O --> TM[TaskManager<br/>utils/tasks.py]
    O --> S[Skills<br/>utils/skills.py]
    O --> MM[Memory<br/>utils/memory.py]
    O --> T[Team Delegation<br/>utils/teams.py]

    C --> W[工作区文件]
    C --> X[终端命令执行]

    S --> SK[skills/*/SKILL.md]
    MM --> TR[.makecode/transcripts/]
    TM --> TP[.makecode/tasks/]
    T --> TH[.makecode/team/]

    TM --> RQ[GetRunnableTasks<br/>Runnable Frontier]
    RQ --> T

    T --> A1[Sub-Agent 1]
    T --> A2[Sub-Agent 2]
    T --> AN[Sub-Agent N]

    A1 --> TD[TodoUpdate<br/>tools/todo.py]
    A2 --> TD
    AN --> TD

    A1 --> RP[任务报告]
    A2 --> RP
    AN --> RP

    RP --> T
    T --> TM
    T --> O
    O --> F[最终响应]
```

### 3.3 架构说明

- `main.py` 是总编排器，负责与模型对话、处理工具调用、推进主循环。
- `init.py` 提供工作区选择、环境变量加载与 OpenAI 客户端初始化。
- `utils/common.py` 提供文件读写、按行编辑、文本搜索和终端命令执行能力。
- `utils/tasks.py` 维护任务 DAG、状态流转与 runnable frontier。
- `utils/teams.py` 负责把最新可执行任务并发委派给子代理，并回收结果。
- `utils/skills.py` 提供技能发现和技能内容加载。
- `utils/memory.py` 负责长会话压缩与转录保存。
- `tools/todo.py` 供子代理在多步骤任务中维护内部待办。

---

## 4. 执行流程

典型流程如下：

1. 用户输入任务。
2. 编排器基于系统策略决定是否先创建或更新 TaskManager 计划。
3. 模型返回工具调用。
4. 编排器执行工具并回填结果。
5. 若存在可并行任务，则先调用 `GetRunnableTasks`。
6. 对最新可执行前沿任务使用 `DelegateTasks` 并发委派。
7. 子代理完成后回传报告。
8. 编排器继续推进后续任务，直到形成最终答案。

---

## 5. 环境要求

- Python 3.10+
- 可用的 OpenAI 兼容接口
- 模型支持 Chat Completions API 或 Responses API

当前 `requirements.txt` 中声明的依赖：

- `openai`
- `pydantic`
- `prompt_toolkit`
- `python-dotenv`
- `rich`
- `tqdm`

---

## 6. 安装与运行

### 6.1 安装依赖

```bash
pip install -r requirements.txt
```

### 6.2 准备工作区（重要）

MakeCode 采用严格的工作区（Workspace）隔离机制，因此**不建议**在 MakeCode 源码目录直接运行任务。请在你实际要处理的项目目录（即你希望 Agent 工作的目录）中，准备以下内容：

1. **环境配置文件 `.env`**：
   在你的目标工作区根目录下创建 `.env` 文件，填入模型配置：
   ```env
   OPENAI_BASE_URL=your_endpoint
   OPENAI_API_KEY=your_api_key
   MODEL_ID=your_model_id
   ```
   > 注：`MODEL_ID` 对应的模型必须支持 Chat Completions API 或 Responses API。当该文件中的变量与系统环境变量冲突时，启动 MakeCode 时会弹出交互式提示让你选择是否覆盖。

2. **自定义技能库 `skills/`（可选）**：
   如果你的项目需要特定的专家技能，请在你的目标工作区根目录下创建一个 `skills` 文件夹。
   目录结构如：`skills/<skill-name>/SKILL.md`。MakeCode 会严格仅从该目录下加载技能。

### 6.3 启动

在 MakeCode 的源码目录下运行以下命令启动 CLI：

```bash
python main.py
```

启动后会进入向导流程：
1. **交互式选择工作区目录（WORKDIR）**：输入你刚才准备好 `.env` 和 `skills` 的所在目录（绝对路径），或者按回车使用当前目录。
2. **处理环境变量冲突**：如果 `.env` 文件变量与系统变量有冲突，按提示进行覆盖确认。
3. **选择 API 标准**：选择你使用的底层 API 协议（Chat Completions API 或 Responses API）。
4. **进入交互式终端**：开始与主代理对话。

### 6.4 内置快捷命令（Slash Commands）

在交互式 CLI 中，支持输入斜杠 `/` 来触发快捷命令（带有输入补全提示）：

| 命令 | 描述 |
| --- | --- |
| `/cmds` | 列出所有的可用命令和功能描述 |
| `/load` | 列出历史 checkpoint 并选择加载 |
| `/skills` | 列出当前工作区可用的 skills |
| `/compact` | 压缩当前对话上下文 |
| `/tools` | 列出当前可用工具详细信息 |
| `/tasks` / `/plan` | 查看任务看板和当前执行进度 |
| `/status` | 汇报系统状态、已完成任务和下一步计划 |
| `/help` | 显示使用帮助和自我介绍 |
| `/workspace` / `/ls` | 查看当前工作区目录结构 |
| `/clear` / `/reset` | 清空当前对话历史 |
| `/quit` / `/exit` | 退出程序 |

---

## 7. 使用约束

项目当前内置的重要规则包括：

- 优先使用 File 工具进行文件读写与文本搜索。
- 常规文件操作不应依赖终端命令完成。
- 委派前必须先调用 `GetRunnableTasks`。
- `DelegateTasks` 只允许处理最新可执行前沿中的任务。
- 仅适合并行且彼此独立的任务才能并发委派。
- 终端命令必须是非交互式、安全的命令。

---

## 8. 扩展方式

### 8.1 新增技能

1. 新建目录 `skills/<name>/`
2. 添加 `SKILL.md`
3. 可在 frontmatter 中声明：
   - `name`
   - `description`
   - `tags`
4. 重启后即可通过 `ListSkills` / `LoadSkill` 发现

### 8.2 新增工具

当前工具注册方式统一基于 `openai.pydantic_function_tool(...)`。系统在底层（`utils/llm_client.py`）会自动将其格式化处理为适配不同大模型 API 标准的格式。

新增工具的一般步骤：

1. 定义 Pydantic 模型作为工具入参描述
2. 实现具体的 Python 函数处理逻辑
3. 通过 `pydantic_function_tool` 注册到对应工具集合列表
4. 将该工具的方法名与对应的函数绑定到 `*_HANDLERS` 字典中
5. 在主循环或子代理循环的工具聚合列表中接入

---

## 9. 常见问题

### 9.1 缺少环境变量

如果启动时报错，请检查：

- `OPENAI_API_KEY`
- `OPENAI_BASE_URL`
- `MODEL_ID`

### 9.2 路径越界

`RunRead` / `RunWrite` / `RunEdit` / `RunGrep` 都以工作区为边界，超出工作区的路径会被拒绝。

### 9.3 终端命令失败

请确认：

- 本机存在启动时检测到的终端环境
- 命令不需要交互输入
- 命令未超过 120 秒超时限制

### 9.4 为什么委派任务失败

常见原因：

- 任务不在最新 `GetRunnableTasks` 返回结果中
- 任务存在依赖未完成
- 传入了重复或不存在的任务 ID
