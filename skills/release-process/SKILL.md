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

UPDATE_SERVER_URL = "https://starvpn.forwardforever.top"
VERSION_CHECK_URL = f"{UPDATE_SERVER_URL}/version.json"
DOWNLOAD_URL = f"{UPDATE_SERVER_URL}/MakeCode-Windows-X64.zip"
```

### 版本变更检查清单

在修改版本号前，确认以下事项：

- [ ] **MAJOR 变更**：检查是否有不兼容的 API 变更、数据库/配置格式迁移需求
- [ ] **MINOR 变更**：确认新功能已完整实现并通过测试
- [ ] **PATCH 变更**：确认 Bug 已修复且不影响现有功能
- [ ] 更新 `version.py` 中的 `CURRENT_VERSION`
- [ ] 检查是否需要更新 `UPDATE_SERVER_URL`（服务器地址变更时）

---

## 2. 构建打包流程

### 2.1 版本号检查与提交

**这是整个发布流程的第一步。** 在做任何其他操作之前，先获取远程版本以判断是否需要版本变更：

1. 请求 `https://starvpn.forwardforever.top/version.json` 获取远程当前已发布版本（这是首要步骤，决定后续所有操作）
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
- macOS：`dist/MakeCode.app`

Windows 的 `dist/updater.exe` 会被收集为 `dist/MakeCode/_internal/updater.exe`，因此 Windows 必须先构建 updater。macOS 不构建 updater，也不支持应用内自动更新。

### 2.5 构建顺序

Windows：

```
1. pyinstaller updater.spec    → dist/updater.exe
2. pyinstaller MakeCode.spec   → dist/MakeCode/（_internal 内含 updater.exe）
```

macOS：

```
pyinstaller MakeCode.spec      → dist/MakeCode.app
```

当前 GitHub Actions 只构建 Windows X64 和 macOS ARM64，不构建 Linux 应用。构建后只做 TOC、目录结构和警告日志静态检查，禁止启动 `dist` 下的程序。

---

## 3. 发布流程

### 3.1 生成版本信息文件

运行发布脚本生成 `version.json`（`--release_log` 为必需参数，传入发布日志文件路径）：

创建发布日志文件（如 `RELEASE_LOG.md`），写入 markdown 格式的发布内容，然后：

```bash
python release.py --release_log RELEASE_LOG.md
```

该脚本用于 Windows 自动更新资产，会：
1. 检查 `dist/MakeCode/MakeCode.exe` 是否存在
2. 将完整 `dist/MakeCode/` 打成 `dist/MakeCode-Windows-X64.zip`，ZIP 顶层必须是 `MakeCode/`
3. 计算 ZIP 的大小和 SHA-256
4. 生成 `dist/version.json`，内容包含：
   - `version`：当前版本号
   - `download_url`：Windows 完整 ZIP 的 HTTPS 地址
   - `sha256`：ZIP 校验值
   - `size`：ZIP 字节数
   - `release_log`：发布日志（markdown 格式，用于更新通知展示）

`RELEASE_LOG.md` 是临时发布文件，保持在 `.gitignore` 中，不提交。补丁版本发布日志按项目既有聚合规则生成。

### 3.2 上传文件到服务器（可并行）

FTP 上传和 GitHub Release 上传相互独立，**可以使用一行命令同时执行**以加快发布速度：

```bash
# 同时执行 FTP 和 GitHub 上传（伪代码，请根据终端环境调整）
python ftp_release.py & python github_release.py
```

或者分别执行：

使用 FTP 上传脚本将构建产物上传到更新服务器：

```bash
python ftp_release.py
```

该脚本会将以下文件上传到 FTP 服务器：

| 本地文件 | 服务器路径 | 用途 |
|---------|-----------|------|
| `dist/MakeCode-Windows-X64.zip` | MakeCode-Windows-X64.zip | Windows 完整目录自动更新包 |
| `dist/version.json` | version.json | Windows 版本检查与资产校验 |

**FTP 配置**（存储在 `.ftp_config` 文件中）：
```json
{
    "host": "120.79.196.147",
    "port": 21,
    "user": "panel_ssl_site",
    "pass": "******"
}
```

**注意事项**：
- `.ftp_config` 包含 FTP 凭据，已加入 `.gitignore`，不会提交到远程仓库
- 脚本使用 `NatFTP` 类修复 NAT 环境下 PASV 返回内网 IP 的问题
- 服务器需开放被动端口范围（39000-40000），否则数据通道会超时

### 3.3 上传到 GitHub Release（可与 FTP 并行）

使用 GitHub Release 脚本将构建产物发布到 GitHub，可与 FTP 上传同时执行（见 3.2 节的一行命令）：

```bash
python github_release.py
```

该脚本会：
1. 删除仓库中当前主版本线的现有 Releases 和对应 tags（例如发布 `4.0.0` 时只清理 `4.x.x`，保留 `3.x.x`）
2. 创建新的 Release（tag 为 `v{版本号}`），body 包含版本和 commit 信息（markdown 格式）
3. 上传 `MakeCode-Windows-X64.zip` 和 `version.json`

标签触发的 GitHub Actions 则发布两个用户资产：`MakeCode-Windows-X64.zip` 与 `MakeCode-macOS-ARM64.zip`。macOS ZIP 内是完整 `MakeCode.app`，由用户手动下载替换。

**GitHub 配置**：
- 仓库：`upupmake/MakeCode`
- Token：存储在 `.github_token` 文件中（已加入 `.gitignore`）
- Token 需要 `repo` 权限

**注意事项**：
- 每次发布只会清除当前主版本线的历史 Release，只保留该主版本线的最新版本
- 发布新的主版本（如 `4.0.0`）时，必须保留上一主版本线的稳定版本（如 `3.x.x` 最后一个版本）
- Token 权限不足会导致 404 错误，需确保勾选 `repo` 权限

### 3.4 发布检查清单

- [ ] 版本号已确认（`version.py`）
- [ ] **所有变更已提交**（`version.py` + 代码变更）— 构建前完成
- [ ] Windows updater.exe 已先构建
- [ ] Windows `dist/MakeCode/` 或 macOS `dist/MakeCode.app` 已构建
- [ ] Windows `python release.py --release_log RELEASE_LOG.md` 已执行成功
- [ ] `dist/MakeCode-Windows-X64.zip` 与 `dist/version.json` 已生成且相互匹配
- [ ] ZIP 的顶层目录为 `MakeCode/`，并包含 `MakeCode.exe`、`_internal/` 和 `_internal/updater.exe`
- [ ] FTP 上传完成
- [ ] GitHub Release 上传完成
- [ ] 验证服务器版本检查接口返回正确
- [ ] **确认工作区干净**：运行 `git status` 确认无未提交的文件

---

## 4. 自动更新机制

### 4.1 更新检查流程

用户端启动时会：
1. 请求 `{UPDATE_SERVER_URL}/version.json` 获取最新版本信息
2. 比较本地版本与服务器版本
3. Windows 打包版可下载并安装更新；macOS 只提示用户从 GitHub Release 手动下载最新版

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

- macOS 暂不做应用内自动更新，因为可靠替换 `.app` 还涉及代码签名、公证和隔离属性；只提示手动下载。
- 从旧 onefile 版本迁移到首个 onedir 版本应手动完成。安装 onedir 版本后，后续 Windows 版本才能使用完整目录自动更新。
- `.makecode` 位于安装目录内，目录更新时必须保留。
- 不得只替换 onedir 中的 `MakeCode.exe`，否则会造成 EXE 与 `_internal` 依赖版本混合。

---

## 5. 常见问题排查

### Q1: 构建失败 "找不到 updater.exe"

确保先运行 `pyinstaller updater.spec`，再运行 `pyinstaller MakeCode.spec`。

### Q2: version.json 生成失败

检查 `dist/MakeCode/MakeCode.exe` 是否存在，确保 Windows onedir 构建步骤已完成。

### Q3: Windows 用户无法更新

检查：
- 服务器上的 `version.json` 是否可访问
- `download_url` 是否是 `MakeCode-Windows-X64.zip` 的 HTTPS 地址
- `sha256` 与 `size` 是否和服务器 ZIP 完全匹配
- ZIP 顶层是否为唯一的 `MakeCode/`，内部是否包含 `MakeCode.exe` 与 `_internal/`
- 当前客户端是否已经是 onedir 版本；旧 onefile 客户端需要手动迁移

### Q4: 版本号格式错误

确保使用 `MAJOR.MINOR.PATCH` 格式，如 `3.0.1`，不要添加前缀 `v`。

### Q5: FTP 上传数据通道超时

FTP 使用两个通道：控制通道（端口 21）和数据通道。被动模式下数据通道端口由服务器动态分配。

解决方法：
1. 确保服务器已开放被动端口范围（39000-40000）
2. 如果服务器在 NAT 后面，`ftp_release.py` 中的 `NatFTP` 类会自动用公网 IP 替换 PASV 返回的内网 IP

---

## 6. 快速发布命令参考

```bash
# 完整发布流程
# 1. 获取远程已发布版本，判断是否需要版本变更（首要步骤）
curl -s https://starvpn.forwardforever.top/version.json
# 2. 对比本地 version.py，必要时更新版本号并提交所有变更
git add -A && git commit -m "release: vX.Y.Z"
# 3. Windows 构建打包（macOS 只运行第二条）
pyinstaller updater.spec
pyinstaller MakeCode.spec
# 不启动 dist 程序；检查 dist/MakeCode、TOC 和 warn-MakeCode.txt
# 4. 创建临时发布日志 RELEASE_LOG.md，写入 markdown 格式发布内容
# 5. 生成 Windows 完整 ZIP 与 version.json
python release.py --release_log RELEASE_LOG.md
# 6. 上传到服务器（可以使用一行命令同时进行，伪代码请根据终端环境调整）
python ftp_release.py & python github_release.py
# 7. 确认工作区干净
git status  # 应输出 "nothing to commit, working tree clean"
```

---

## 7. 相关文件说明

| 文件 | 用途 |
|------|------|
| `version.py` | 版本号和服务器地址配置 |
| `release.py` | 将 Windows onedir 打成 ZIP，并生成 version.json |
| `MakeCode.spec` | Windows onedir 与 macOS BUNDLE 打包配置 |
| `updater.spec` | Windows 独立更新器打包配置 |
| `updater.py` | Windows 完整目录事务更新器源码 |
| `ftp_release.py` | FTP 上传脚本（配置存储在 `.ftp_config`） |
| `github_release.py` | GitHub Release 上传脚本（配置存储在 `.github_token`） |
| `.ftp_config` | FTP 服务器配置（不提交远程） |
| `.github_token` | GitHub Token（不提交远程） |
