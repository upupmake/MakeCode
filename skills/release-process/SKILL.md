---
name: release-process
description: "MakeCode 软件发布流程技能。当用户需要发布新版本、修改版本号、打包构建、上传更新文件、或了解发布规范时触发。包含版本变更规则、构建步骤、发布流程和更新机制说明。适用场景：发布版本、版本号管理、构建打包、更新部署、发布问题排查。"
---

# MakeCode 发布流程

本技能指导完成 MakeCode 软件的完整发布流程，包括版本管理、构建打包、发布部署和自动更新机制。

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

## 2. 构建打包流程

### 2.1 版本号检查与提交

**这是整个发布流程的第一步。** 在做任何其他操作之前，先获取远程版本以判断是否需要版本变更：

1. 请求 `https://github.com/upupmake/MakeCode/releases/latest/download/version.json` 获取远程当前已发布版本（这是首要步骤，决定后续所有操作）
2. 对比本地 `version.py` 中的 `CURRENT_VERSION`
3. 如果版本号相同 → 询问用户新版本号并更新 `version.py`
4. 如果版本号已递增 → 直接进入下一步
5. 运行 `git status` 检查所有待提交的变更（包括 `version.py` 和其他代码变更）
6. **先提交所有变更，再开始构建** — 确保构建产物基于已提交的代码
7. 版本号确认且提交完成后再开始构建

### 2.2 前置准备

确保以下工具已安装：
- Python 3.8+
- PyInstaller
- 项目依赖（`pip install -r requirements.txt`）

### 2.3 构建 updater.exe

updater 是独立的更新器程序，需要先构建：

```bash
pyinstaller updater.spec
```

构建产物：`dist/updater.exe`

### 2.4 构建主程序

```bash
pyinstaller MakeCode.spec
```

构建采用 `onedir`，不要改回 `onefile`：

- Windows：`dist/MakeCode/MakeCode.exe`，运行依赖位于 `dist/MakeCode/_internal/`
- macOS：`dist/MakeCode/MakeCode`，运行依赖位于 `dist/MakeCode/_internal/`；发布 ZIP 顶层额外提供 `MakeCode.command`，用户双击该脚本在 Terminal 中启动

Windows 的 `dist/updater.exe` 会被收集为 `dist/MakeCode/_internal/updater.exe`，因此 Windows 必须先构建 updater。macOS 和 Linux 不构建 updater，也不支持应用内自动更新。

### 2.5 构建顺序

Windows：

```
1. pyinstaller updater.spec    → dist/updater.exe
2. pyinstaller MakeCode.spec   → dist/MakeCode/（_internal 内含 updater.exe）
```

macOS：

```
pyinstaller MakeCode.spec      → dist/MakeCode/
```

Linux：

```
pyinstaller MakeCode.spec      → dist/MakeCode/
```

GitHub Actions 将 `assets/MakeCode.command` 与 `dist/MakeCode/` 一起打入 `MakeCode-macOS-ARM64.zip`。macOS 用户解压后双击顶层 `MakeCode.command`，不发布 `.app`。

GitHub Actions 将 `dist/MakeCode/` 打入 `MakeCode-Linux-X64.zip`。Linux 用户解压后直接运行 `./MakeCode/MakeCode`；若执行权限未保留，先运行 `chmod +x MakeCode/MakeCode`。

当前 GitHub Actions 构建 Windows X64、macOS ARM64 和 Linux X64。Linux runner 固定为 `ubuntu-22.04`，发布包支持基线为 GLIBC 2.35+，避免 `ubuntu-latest` 升级后无意提高最低系统要求。构建后只做 TOC、目录结构和警告日志静态检查，禁止启动 `dist` 下的程序。

---

## 3. 发布流程

### 3.1 准备发布日志

创建临时 `RELEASE_LOG.md`，写入 markdown 格式发布内容。该文件保持在 `.gitignore` 中，不提交；补丁版本日志按项目既有聚合规则生成。

### 3.2 创建 GitHub Release

使用 GitHub Release 脚本读取 `RELEASE_LOG.md` 并创建 Release/tag：

```bash
python github_release.py
```

该脚本会：
1. 删除仓库中当前主版本线的现有 Releases 和对应 tags（例如发布 `5.0.0` 时只清理 `5.x.x`，保留 `4.x.x`）
2. 创建新的 Release（tag 为 `v{版本号}`），body 包含带固定标记的发布日志
3. 不上传本地产物；创建 tag 后由 GitHub Actions 统一生成发布资产

GitHub Actions 自动构建 Windows、macOS 和 Linux ZIP。Release 汇总任务读取 Release body 中的日志，为三个平台 ZIP 分别计算大小和 SHA-256，生成向后兼容的 `version.json`（顶层字段继续描述 Windows 资产，`platforms` 字段包含所有平台），并将四个资产统一附加到 Release：`MakeCode-Windows-X64.zip`、`MakeCode-macOS-ARM64.zip`、`MakeCode-Linux-X64.zip`、`version.json`。

**GitHub 配置**：
- 仓库：`upupmake/MakeCode`
- Token：存储在 `.github_token` 文件中（已加入 `.gitignore`）
- Token 需要 `repo` 权限

**注意事项**：
- 每次发布只会清除当前主版本线的历史 Release，只保留该主版本线的最新版本
- 发布新的主版本（如 `4.0.0`）时，必须保留上一主版本线的稳定版本（如 `3.x.x` 最后一个版本）
- Token 权限不足会导致 404 错误，需确保勾选 `repo` 权限

### 3.3 发布检查清单

- [ ] 版本号已确认（`version.py`）
- [ ] **所有变更已提交**（`version.py` + 代码变更）— 构建前完成
- [ ] `RELEASE_LOG.md` 已准备并包含本次日志
- [ ] GitHub Release/tag 已创建且指向本次发布提交
- [ ] 标签触发的 Windows/macOS/Linux Actions 构建成功
- [ ] Release 汇总任务已为三个平台 ZIP 生成带 `platforms` 字段的 `version.json`
- [ ] Release 包含 Windows ZIP、macOS ZIP、Linux ZIP 和 `version.json`
- [ ] GitHub latest Release 的 `version.json` 可访问，顶层哈希、大小与 Windows ZIP 匹配，各 `platforms` 条目与对应 ZIP 匹配
- [ ] **确认工作区干净**：运行 `git status` 确认无未提交的文件

---

## 4. 自动更新机制

### 4.1 更新检查流程

用户端启动时会：
1. Windows 请求 GitHub latest Release 的 `version.json` 获取最新版本信息
2. Windows 比较本地版本与服务器版本，并可下载、校验和安装更新
3. macOS 和 Linux 不支持应用内自动更新，用户需从 GitHub Release 手动下载最新版

下载必须使用 HTTPS 和系统证书验证。客户端要求 manifest 提供有效 `sha256` 与正整数 `size`，下载后同时校验大小和 SHA-256。

### 4.2 Windows 更新执行流程

主程序将内置的 updater 释放到安装目录之外，随后用 `os._exit(0)` 退出整个进程。updater.exe 负责完整 onedir 更新：

```
1. 接收 --install-dir、--archive、--pid
2. 等待主程序退出（超时 30 秒）
3. 拒绝路径穿越、绝对路径、盘符/反斜杠路径和符号链接
4. 解压到安装目录同级 staging，并验证 MakeCode/MakeCode.exe 与 MakeCode/_internal/
5. 将旧安装目录切换为 backup
6. 将旧目录的 .makecode 用户配置复制到新版
7. 将 staged MakeCode 目录切换为正式安装目录
8. 启动新版并等待 ready-file 启动确认
9. 确认成功后删除 backup；失败或超时则恢复旧目录
```

### 4.3 更新边界与迁移

- macOS 暂不做应用内自动更新；发布包使用 `MakeCode.command + MakeCode/ onedir`，用户从 GitHub Release 手动下载并替换。
- 从旧 onefile 版本迁移到首个 onedir 版本应手动完成。安装 onedir 版本后，后续 Windows 版本才能使用完整目录自动更新。
- Windows 的 `.makecode` 位于安装目录内，目录更新时必须保留；macOS 打包版配置位于 `~/Library/Application Support/MakeCode`。
- 不得只替换 onedir 中的 `MakeCode.exe`，否则会造成 EXE 与 `_internal` 依赖版本混合。

---

## 5. 常见问题排查

### Q1: 构建失败 "找不到 updater.exe"

确保先运行 `pyinstaller updater.spec`，再运行 `pyinstaller MakeCode.spec`。

### Q2: version.json 生成失败

检查 Release 汇总任务是否能找到 `MakeCode-Windows-X64.zip`，以及 GitHub Release body 是否包含发布日志起止标记。

### Q3: Windows 用户无法更新

检查：
- GitHub latest Release 的 `version.json` 是否可访问
- `download_url` 是否是 `MakeCode-Windows-X64.zip` 的 HTTPS 地址
- `sha256` 与 `size` 是否和服务器 ZIP 完全匹配
- ZIP 顶层是否为唯一的 `MakeCode/`，内部是否包含 `MakeCode.exe` 与 `_internal/`
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
| `updater.spec` | Windows 独立更新器打包配置 |
| `updater.py` | Windows 完整目录事务更新器源码 |
| `github_release.py` | 从 RELEASE_LOG.md 创建 GitHub Release/tag；所有资产由 Actions 生成 |
| `.github_token` | GitHub Token（不提交远程） |
