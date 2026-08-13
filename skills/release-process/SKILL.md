---
name: release-process
description: "MakeCode 软件发布流程技能。当用户需要发布新版本、修改版本号、触发或排查 GitHub Actions 发布构建、或了解发布规范时触发。包含版本变更规则、Actions 构建与发布流程和更新机制说明。适用场景：发布版本、版本号管理、GitHub Actions 构建、更新部署、发布问题排查。"
---

# MakeCode 发布流程

本技能指导完成 MakeCode 软件的完整发布流程。正式发布包统一由 GitHub Actions 构建和上传，本地不执行发布打包、不上传本地产物。

---

## 1. 版本变更规则

MakeCode 使用**语义化版本号**（Semantic Versioning），格式为 `MAJOR.MINOR.PATCH`：

### 版本号定义

| 版本类型 | 变更时机 | 示例 |
|---------|---------|------|
| **MAJOR**（主版本） | 不兼容的 API 变更、架构重大重构、数据格式不兼容变更 | 2.x.x → 3.0.0 |
| **MINOR**（次版本） | 新增功能、新增工具、新增能力（向后兼容） | 3.0.x → 3.1.0 |
| **PATCH**（补丁版本） | Bug 修复、性能优化、文档更新（向后兼容） | 3.0.0 → 3.0.1 |

### 版本号修改位置

版本号统一在 `version.py` 文件中管理：

```python
# version.py
CURRENT_VERSION = "3.0.1"  # ← 修改此处

GITHUB_RELEASE_BASE_URL = "https://github.com/upupmake/MakeCode/releases/latest/download"
VERSION_CHECK_URL = f"{GITHUB_RELEASE_BASE_URL}/version.json"
DOWNLOAD_URL = f"{GITHUB_RELEASE_BASE_URL}/MakeCode-Windows-X64.zip"
```

### 版本变更检查清单

在修改版本号前，确认以下事项：

- [ ] **MAJOR 变更**：检查是否有不兼容的 API 变更、数据库/配置格式迁移需求
- [ ] **MINOR 变更**：确认新功能已完整实现并通过测试
- [ ] **PATCH 变更**：确认 Bug 已修复且不影响现有功能
- [ ] 更新 `version.py` 中的 `CURRENT_VERSION`
- [ ] 检查 GitHub Release 下载地址是否仍指向 `upupmake/MakeCode`

---

## 2. GitHub Actions 发布构建

正式发布包只允许由 `.github/workflows/build.yml` 构建。发布流程不得在本地运行 PyInstaller、不得使用本地 `dist/` 产物创建压缩包，也不得向 GitHub Release 上传本地产物。

### 2.1 发布前准备

在创建 Release/tag 前：

1. 读取 `https://github.com/upupmake/MakeCode/releases/latest/download/version.json`，确认远程当前稳定版本。
2. 对比 `version.py` 中的 `CURRENT_VERSION`，按语义化版本规则更新版本号。
3. 运行相关测试和发布前检查。
4. 检查所有待发布变更，包括 `version.py`。
5. 提交并推送发布提交，确保 tag 和 Actions 构建都基于已提交代码。
6. 准备临时 `RELEASE_LOG.md`，再运行 `python github_release.py` 创建非 latest 的 Release/tag。

### 2.2 Actions 构建职责

`v*` tag 触发 GitHub Actions 后，由工作流自动完成：

- Windows X64：先构建 updater，再构建 PyInstaller onedir 主程序；验证 `MakeCode.exe`、`_internal/` 和内置 updater；生成 `MakeCode-Windows-X64.zip`。
- macOS ARM64：构建 PyInstaller onedir 主程序；将顶层 `assets/MakeCode.command` 与 `MakeCode/` 目录一起生成 `MakeCode-macOS-ARM64.zip`；不得发布或恢复 `.app`/BUNDLE 方案。
- Linux X64：在固定的 `python:3.12-bullseye` 容器中先构建 updater，再构建 onedir 主程序；生成 `MakeCode-Linux-X64.zip`，支持基线为 GLIBC 2.31+。

三个平台的构建任务负责执行测试和静态检查，包括 TOC、目录结构、警告日志、入口文件，以及 Linux ELF/GLIBC 要求。工作流禁止启动 `dist/` 下的程序。

### 2.3 Actions 发布资产

Release 汇总任务只接收各平台 Actions artifacts，并完成：

1. 下载 Windows、macOS 和 Linux 发布资产。
2. 检查三个平台 ZIP 均存在。
3. 计算每个平台 ZIP 的大小和 SHA-256。
4. 生成包含 `platforms` 字段的 `version.json`。
5. 将三个 ZIP 和 `version.json` 上传到 GitHub Release。
6. 再次确认四个资产齐全，将新 Release 设为 latest。
7. 最后清理同一 `MAJOR.MINOR` 版本线内更早的 patch Releases 和 tags。

本地只负责版本、代码、测试、发布日志和创建 Release/tag；发布包的编译、压缩、校验、上传和 latest 切换均由 GitHub Actions 完成。

---

## 3. 发布流程

### 3.1 准备发布日志

创建临时 `RELEASE_LOG.md`，写入 markdown 格式发布内容。该文件保持在 `.gitignore` 中，不提交。

发布日志按 `MAJOR.MINOR` 版本线维护：

- **Patch 版本**：必须保留当前 `MAJOR.MINOR` 版本线此前所有已发布版本的完整日志，并将当前 patch 的变更追加在最前面。例如发布 `6.4.8` 时，日志必须包含 `v6.4.7`、`v6.4.6` 及更早的 `6.4.x` 日志；不能只写当前 patch，也不能因清理旧 Release/tag 而丢失历史内容。
- **Minor 版本**：从新的 `MAJOR.MINOR` 版本线重新开始，只写新 minor 版本的变更，不继承上一 minor 版本线的日志。例如 `6.5.0` 不继承 `6.4.x` 日志；之后的 `6.5.x` patch 再持续累积 `6.5` 版本线日志。
- **Major 版本**：同样从新的版本线重新开始，只写新 major/minor 版本线的变更。

准备日志时，优先从远程 latest `version.json` 的 `release_log` 获取当前版本线历史；如果当前版本是新 minor 或新 major 的首个版本，则丢弃旧版本线历史并创建新日志。生成后确认日志从当前版本开始，并且 patch 版本包含同一 `MAJOR.MINOR` 线的完整历史。

### 3.2 创建 GitHub Release

使用 GitHub Release 脚本读取 `RELEASE_LOG.md` 并创建 Release/tag：

```bash
python github_release.py
```

该脚本会：
1. 创建新的 Release（tag 为 `v{版本号}`），body 包含带固定标记的发布日志
2. 新 Release 创建时显式保持为非 latest，避免构建期间 `latest/download` 指向尚无资产的版本
3. 不删除任何已有 Release/tag，也不上传本地产物；创建 tag 后由 GitHub Actions 统一生成发布资产

GitHub Actions 自动构建 Windows、macOS 和 Linux ZIP。Release 汇总任务读取 Release body 中的日志，为三个平台 ZIP 分别计算大小和 SHA-256，生成向后兼容的 `version.json`（顶层字段继续描述 Windows 资产，`platforms` 字段包含所有平台），并将四个资产统一附加到 Release：`MakeCode-Windows-X64.zip`、`MakeCode-macOS-ARM64.zip`、`MakeCode-Linux-X64.zip`、`version.json`。四个资产全部上传成功后，汇总任务再次通过 GitHub API 校验资产完整性，将新 Release 设为 latest，最后删除同一 `MAJOR.MINOR` 版本线内更早的 Releases 和对应 tags。

**GitHub 配置**：
- 仓库：`upupmake/MakeCode`
- Token：存储在 `.github_token` 文件中（已加入 `.gitignore`）
- Token 需要 `repo` 权限

**注意事项**：
- 旧版本清理只能发生在新 Release 的 Windows、macOS、Linux ZIP 和 `version.json` 全部上传成功之后
- 每次只清除当前 `MAJOR.MINOR` 版本线内更早的 patch Releases；例如发布 `6.1.8` 时清理旧 `6.1.x`，保留 `6.0.x`、`5.x.x`
- 若构建、manifest 生成或资产上传失败，旧 Release/tag 必须保持不变
- Token 权限不足会导致 404 错误，需确保勾选 `repo` 权限

### 3.3 发布检查清单

- [ ] 版本号已确认（`version.py`）
- [ ] **所有变更已提交**（`version.py` + 代码变更）— 构建前完成
- [ ] `RELEASE_LOG.md` 已准备并包含本次日志
- [ ] GitHub Release/tag 已创建且指向本次发布提交
- [ ] 标签触发的 Windows/macOS/Linux Actions 构建成功
- [ ] Release 汇总任务已为三个平台 ZIP 生成带 `platforms` 字段的 `version.json`
- [ ] Release 包含 Windows ZIP、macOS ZIP、Linux ZIP 和 `version.json`
- [ ] 新 Release 已在四个资产齐全后设为 latest，构建期间旧 latest 保持可用
- [ ] 资产上传成功后，同一 `MAJOR.MINOR` 版本线内更早的 Release/tag 已清理，其他版本线稳定版本仍保留
- [ ] GitHub latest Release 的 `version.json` 可访问，顶层哈希、大小与 Windows ZIP 匹配，各 `platforms` 条目与对应 ZIP 匹配
- [ ] **确认工作区干净**：运行 `git status` 确认无未提交的文件

---

## 4. 自动更新机制

### 4.1 更新检查流程

用户端启动时会：
1. Windows/Linux 请求 GitHub latest Release 的 `version.json` 获取最新版本信息
2. 从 `platforms` 严格选择 `windows-x86_64` 或 `linux-x86_64` 资产并下载、校验和安装
3. macOS 不支持应用内自动更新，用户需从 GitHub Release 手动下载最新版

冻结版 Windows/Linux 同时支持 TUI `/update` 与外部 `--update`。外部命令必须在工作区、模型客户端、MCP 和 TUI 初始化前执行，展示版本与发布日志后默认通过 `[y/N]` 确认；仅显式传入 `-y`/`--yes` 时可跳过确认，非交互终端未传免确认参数时必须拒绝下载。源码运行与 macOS 不支持外部自动安装。`--check-update` 继续保持只读，不下载或安装。

下载必须使用 HTTPS 和系统证书验证。客户端要求 manifest 提供有效 `sha256` 与正整数 `size`，下载后同时校验大小和 SHA-256。Linux 不得回退使用兼容旧客户端的 Windows 顶层字段。

### 4.2 Windows/Linux 更新执行流程

主程序将内置 updater 释放到安装目录之外。Windows 启动 updater 后用 `os._exit(0)` 退出，updater 等待旧进程释放文件；Linux 用 `exec` 将当前前台进程替换为 updater，无需等待旧 PID，shell 会等待更新结果。updater 负责完整 onedir 更新：

```
1. 接收 --install-dir、--archive、--pid（Linux exec 路径传 0）
2. PID 非 0 时等待主程序退出（超时 30 秒）
3. 拒绝路径穿越、绝对路径和不安全符号链接
4. 解压到安装目录同级 staging，验证平台入口与 MakeCode/_internal/
5. Windows 保留安装根目录与 .makecode，事务移动程序条目；Linux 将旧安装目录切换为 backup 并复制 .makecode
6. 将 staged onedir 应用切换到正式位置
7. 替换成功后删除 backup 并提示用户手动重新启动 MakeCode
8. 替换失败则恢复旧版本
```

Linux 更新包可保留 PyInstaller 相对符号链接，但链接目标必须仍位于 `MakeCode/` 内；安装目录必须对当前用户可写。

### 4.3 更新边界与迁移

- macOS 暂不做应用内自动更新；发布包使用 `MakeCode.command + MakeCode/ onedir`，用户从 GitHub Release 手动下载并替换。
- 从旧 onefile 版本迁移到首个 onedir 版本应手动完成。安装 onedir 版本后，后续 Windows/Linux 版本才能使用完整目录自动更新。
- 从 5.3.1 或更早版本升级到 5.3.2 时仍由旧 updater 自动启动新版并等待 `MAKECODE_UPDATE_READY_FILE`；5.3.2 主程序必须保留该一次性兼容信号。5.3.2 内置的新 updater 不设置此变量，也不自动启动后续版本。
- Windows/Linux 的 `.makecode` 位于安装目录内，目录更新时必须保留；macOS 打包版配置位于 `~/Library/Application Support/MakeCode`。
- 不得只替换 onedir 中的 `MakeCode.exe`，否则会造成 EXE 与 `_internal` 依赖版本混合。

---

## 5. 常见问题排查

### Q1: GitHub Actions 构建失败“找不到 updater”

检查 `.github/workflows/build.yml` 中 Windows/Linux job 是否仍保持“先构建 updater，再构建 `MakeCode.spec`”的内部顺序，并检查 updater artifact 是否被收集到 onedir 的 `_internal/`。不要改为本地构建或上传本地产物来绕过 Actions 失败。

### Q2: version.json 生成失败

检查 Release 汇总任务是否能找到 `MakeCode-Windows-X64.zip`，以及 GitHub Release body 是否包含发布日志起止标记。

### Q3: Windows/Linux 用户无法更新

检查：
- GitHub latest Release 的 `version.json` 是否可访问，`platforms` 是否包含当前平台
- 平台 `download_url` 是否指向对应 Windows/Linux ZIP 的 HTTPS 地址
- `sha256` 与 `size` 是否和服务器 ZIP 完全匹配
- ZIP 顶层是否为唯一的 `MakeCode/`，内部是否包含平台入口与 `_internal/`
- Linux 安装目录是否可写、入口是否保留执行权限
- 当前客户端是否已经是 onedir 版本；旧 onefile 客户端需要手动迁移

### Q4: 版本号格式错误

确保使用 `MAJOR.MINOR.PATCH` 格式，如 `3.0.1`，不要添加前缀 `v`。

---

## 6. 快速发布命令参考

```bash
# 完整发布流程
# 1. 获取远程已发布版本，判断是否需要版本变更（首要步骤）
curl -L https://github.com/upupmake/MakeCode/releases/latest/download/version.json
# 2. 对比本地 version.py，必要时更新版本号并提交所有变更
git add -A && git commit -m "release: vX.Y.Z"
# 3. 创建临时发布日志 RELEASE_LOG.md，写入 markdown 格式发布内容
# 4. 推送发布提交后创建 GitHub Release/tag
python github_release.py
# 5. 等待 GitHub Actions 构建三平台 ZIP 并生成 version.json
# 6. 确认工作区干净
git status  # 应输出 "nothing to commit, working tree clean"
```

---

## 7. 相关文件说明

| 文件 | 用途 |
|------|------|
| `version.py` | 版本号和 GitHub latest Release 地址配置 |
| `.github/workflows/build.yml` | 构建 Windows、macOS、Linux ZIP，并生成包含各平台哈希与大小的 version.json |
| `MakeCode.spec` | Windows、macOS 与 Linux onedir 打包配置 |
| `assets/MakeCode.command` | macOS 发布包顶层启动器 |
| `updater.spec` | Windows/Linux 独立更新器打包配置 |
| `updater.py` | Windows/Linux onedir 事务更新器源码 |
| `github_release.py` | 创建非 latest 的 GitHub Release/tag；供 Actions 在资产上传后设为 latest，并清理同一次版本线内更早的 Release/tag |
| `.github_token` | GitHub Token（不提交远程） |
