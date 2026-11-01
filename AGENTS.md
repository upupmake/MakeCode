# AGENTS.md

本文件是 MakeCode 仓库的编码代理指南。`README.md` 是用户文档与详细设计来源；本文件只保留执行任务所需的命令、架构边界和高风险约定。

## 项目概览

MakeCode 是终端 AI 编码代理，支持 Textual TUI、`--simple` 纯终端模式和 headless 外部 CLI。它通过统一消息层接入 `openai_chat`、`openai_responses`、`anthropic`，并提供文件/终端工具、MCP、Skills、长期记忆、任务 DAG、子代理和自动更新。

- 源码入口：`main.py`
- 工作区与模型初始化：`init.py`
- 全部 LLM Prompt：`prompts.py`
- 版本号与更新地址：`version.py`
- 源码运行：Python 3.10+；CI 使用 Python 3.12
- 发布平台：Windows X64、macOS ARM64、Linux X64、Linux ARM64（GLIBC 2.31+）

## 环境与命令

本机 macOS 开发环境已验证解释器：`/Users/makemake/miniconda3/bin/python3.13`。`python3` 可能指向无 pytest 的系统 Python（3.9.6）；在仓库根目录统一使用 `python3.13 -m ...`。

```bash
# 运行依赖
python3.13 -m pip install -r requirements.txt

# 开发工具：ruff、mypy、pytest、xdist
python3.13 -m pip install -r requirements-dev.txt

# 启动
python3.13 main.py
python3.13 main.py --simple
python3.13 main.py -p "只输出 OK" --output-format json

# 静态检查
python3.13 -m ruff check .
python3.13 -m mypy

# 全量测试（本机 8 核约 31-45 秒）
python3.13 -m pytest tests/ -q -n auto

# 定向测试
python3.13 -m pytest tests/test_file_patch.py -q
```

CI 使用 Python 3.12：`.github/workflows/build.yml` 先跑 `quality`，再跑 `build` / `build-linux`。CI 测试命令固定为 `python -m pytest tests/ -q -n 4`，不跑覆盖率。

## 架构地图

| 路径 | 职责 |
|---|---|
| `main.py` | 编排器主循环、TUI/简单/headless 分发、工具执行与回合推进 |
| `init.py` | 工作区选择、模型初始化、错误日志 |
| `prompts.py` | system prompt 与子代理 prompt 的集中定义 |
| `utils/common.py` | 文件、终端、搜索等基础工具 |
| `utils/llm_client.py` | 三种协议适配、流式输出、重试与跨协议历史重建 |
| `utils/memory.py` | 上下文压缩、长期记忆、transcript |
| `utils/memory_catalog.py` | 轻量记忆读取与展示，不启动 TUI/模型客户端 |
| `utils/tasks.py` / `utils/teams.py` | 任务 DAG 与 runnable frontier、子代理并发 |
| `utils/skills.py` / `utils/skill_catalog.py` | 技能发现、加载与项目级禁用 |
| `utils/project_instructions.py` | `AGENTS.md` 发现与按回合注入 |
| `utils/paths.py` | 安装/工作区路径派生与环境变量覆盖 |
| `system/cli.py` | 外部 CLI 参数与 headless 选项 |
| `system/commands.py` | 斜杠命令路由与交互面板 |
| `system/tui_app.py` / `system/tui_modals.py` | Textual TUI 主应用与弹窗 |
| `system/tui_proxies.py` | 无 TUI 运行时的懒代理 |
| `system/simple_*` | `--simple` 纯终端模式 |
| `system/agent_runtime.py` / `system/headless*.py` | headless 回合、输入输出与事件 |
| `utils/file_access.py` / `utils/hitl.py` / `utils/plan_mode.py` | 文件访问控制、HITL、Plan Mode |
| `tools/` | AskUser、Todo、UnderstandImage 与维护脚本 |
| `tests/` / `live_tests/` | 确定性测试 / 真实外部 API 手工测试 |

## 关键不变量

1. Prompt 文本集中在 `prompts.py`。新增或修改系统提示不要散落到业务模块；工具调用边界可以放在工具 docstring 或参数 `Field` 描述中。
2. `conversation.json` 是唯一会话历史来源。统一消息模型保留正文、工具、可见推理、usage 与必要的 native blocks；不要把 provider 请求体直接存成历史。跨协议请求只能做投影，不得修改本地历史；压缩后必须使相关 native blocks 失效。
3. headless 路径不得 import `system.tui_app`（会连带加载 Textual），函数内 import 同样禁止。使用 `system/tui_proxies.py` 的懒代理。验证命令：`python3.13 -X importtime main.py -p hi 2>&1 | grep -E '\| (textual|system\.tui_app)'` 应无输出。
4. headless 默认 `permission_mode=bypassPermissions` 是产品设计，不是漏洞。不要擅自改回 `default`，也不要新增 max-turns、max-budget、限流等非协议必需限制。`DelegateTasks` 是唯一不注册给 headless 的能力。
5. Skills 不打包进发布产物。默认安装到 `paths.install_skills_dir()`（软件级共享目录）；只有用户明确要求工作区隔离时才装到 `workdir/.makecode/skills/`。发现优先级：安装目录 > 工作区 `.makecode/skills` > 旧 `workdir/skills`。
6. 路径环境变量必须在启动 MakeCode 前设置。`memory`、MCP 管理器、`ModelManager` 等模块会在 import 时缓存路径，运行中修改不保证生效：`MAKECODE_CONFIG_DIR`、`MAKECODE_WORKSPACE_CONFIG_DIR`、`MAKECODE_SKILLS_DIRS`、`MAKECODE_MCP_CONFIG`。
7. 依赖策略：`tree-sitter==0.25.2`、`tree-sitter-language-pack==1.14.3`、`openai`、`anthropic`、`fastmcp` 必须精确锁定；Textual/Rich 不锁版本，以最新版为开发目标，不为旧版本或“未来接口变化”添加兼容分支。
8. 新增仅由 CI 或人工运行的 `tools/` 脚本时，同步加入 `MakeCode.spec` 的 `non_runtime_modules` 和 CI 打包守卫；不要把 tests、live_tests、开发依赖打进发布包。

## 测试约定

- `pytest` 默认只收集 `tests/`。`live_tests/` 需要真实模型或外部 API，按需手动运行，不接入 CI。
- 测试不得读取 `.makecode/memory`、`.makecode/conversations` 等本地运行时文件；CI 是全新 checkout。使用 fixture、`tmp_path` 和构造数据。
- 会在 Windows CI 运行的测试不得断言 POSIX 权限位（`st_mode & 0o077` / `0o111`）；用 `if os.name != "nt":` 门控。
- 非交互测试设置 `MAKECODE_NON_INTERACTIVE=1`。
- 本地并行用 `-n auto`；CI 固定 `-n 4`。不要把 xdist 参数写进 `pyproject.toml` 的 `addopts`。
- 覆盖率不在 CI 中运行；本地需要时用 `python3.13 -m pytest tests/ --cov --cov-report=term`，阈值 `fail_under = 70`。
- 完成前必须运行相关测试与静态检查，并查看结果。部分测试通过或跳过失败不能作为完成证据。
- `RunTerminalCommand` 约 120 秒硬超时。长任务用 `nohup ... &` 启动后短轮询，不要写固定多轮的长循环。

## 代码风格

- 遵循 `pyproject.toml` 的 Ruff 规则；Ruff 与 mypy 必须零告警。mypy 只检查 `[tool.mypy].files` 白名单，新增模块不会自动纳入。
- 只做任务要求的改动。不顺手重构，不添加不需要的抽象、兜底或参数校验；只校验系统边界输入。
- 保持现有中文界面文案、注释与文档语言；代码标识符用英文。命令、路径、错误文本逐字保留。
- 文件和文档遵循 README 第 8.3 节的 Emoji 排版约定；不要添加装饰性 Emoji。
- 用户可见的行为、命令或架构变化，同步更新 `README.md` 与 `README_en.md`。
- 提交信息沿用现有风格：`feat:`、`fix:`、`test:`、`docs:`、`release:` 等，摘要可用中文。

## 构建与发布

- 发布必须由用户明确发起。没有授权不得 commit、push、打 tag 或创建 GitHub Release。
- 正式安装包只能由 `.github/workflows/build.yml` 在 GitHub Actions 构建、压缩、校验并上传。不要在本地运行 PyInstaller 生成发布包，也不要用本地 `dist/` 制作发布 ZIP。
- 版本号在 `version.py`。`v*` tag 触发 Actions，五个必需资产为：`MakeCode-Windows-X64.zip`、`MakeCode-macOS-ARM64.zip`、`MakeCode-Linux-X64.zip`、`MakeCode-Linux-ARM64.zip`、`version.json`。
- 新 Release 先以非 latest 创建；只有五个资产齐全并通过校验后才设为 latest，随后只清理同一 `MAJOR.MINOR` 版本线内的旧 patch。
- 实验版 tag 含 `-`（如 `v6.17.0-exp.1`），不改 `version.py`、不占用 latest、跳过 finalize。
- `RELEASE_LOG.md` 是临时未跟踪文件，绝不 `git add`；`github_release.py` 创建 Release 成功后会自动删除它。
- 发布前重新查询远程 latest，不要依赖文档或记忆中的历史版本号。
- 完整步骤见 `skills/release-process/SKILL.md`。

## Git 与工作区

- 工作区可能同时存在其他 Agent 的未提交改动。只 `git add <本次涉及的文件>`；禁止 `git add -A`、`git add -u`、`git stash`。
- 提交或推送前先 `git fetch`，检查 `git log` 与 `git status`；外部提交可能已落到 `main`。
- `.github_token`、模型 API Key 等秘密不得读取、打印、写入仓库或日志。
- `RELEASE_LOG.md` 保持未跟踪；临时脚本、构建产物和缓存不要提交。

## AGENTS.md 与项目指令

- MakeCode 从项目根（含 `.git` 的目录）到当前工作目录逐层加载 `AGENTS.md`，总量上限 32 KiB，按 user 上下文按回合注入；修改在下一轮生效。
- 越靠近当前工作目录的文件优先级越高；冲突时以内层为准，但 Security、HITL、工具权限和当前用户请求始终优先。
- 本文件是仓库级约定。子目录如需特殊规则可增加 `AGENTS.md`，不要复制整份根文件。

## 安全与执行边界

- 文件操作优先使用 FileRead、FileCreate、FileEdit、FilePatch；终端用于构建、测试、git、包管理和文件/路径/文本搜索。
- 破坏性或难以恢复的操作先向用户确认。不要用删除文件、杀进程、重置仓库等破坏性动作绕过障碍。
- 不为了让检查通过而弱化测试、校验、离线哈希或安全限制；先定位根因。
- 子代理不得并发修改同一文件；委派前先调用 `GetRunnableTasks`，只处理最新可执行前沿任务。
