import os
import platform
import sys
import tarfile
from pathlib import Path

# 极其重要：在导入 tree_sitter_language_pack 之前，强制全局离线模式
# 防止模块在 import 时读取配置从而触发默认缓存或网络行为
os.environ["TS_PACK_OFFLINE"] = "1"

import pyzstd
from tree_sitter_language_pack import (
    get_parser,
    detect_language_from_path,
    process,
    ProcessConfig,
    configure
)

_TS_VALIDATOR_AVAILABLE = True
_TS_VALIDATOR_WARNING_PRINTED = False


def _current_platform_key() -> str | None:
    system = sys.platform
    machine = platform.machine().lower()
    if machine in {"amd64", "x86_64"}:
        arch = "x86_64"
    elif machine in {"arm64", "aarch64"}:
        arch = "aarch64"
    else:
        return None

    if system == "win32":
        return f"windows-{arch}"
    if system.startswith("linux"):
        return f"linux-{arch}"
    if system == "darwin" and arch == "aarch64":
        return "macos-arm64"
    return None


def _mark_ts_unavailable(message: str) -> None:
    global _TS_VALIDATOR_AVAILABLE, _TS_VALIDATOR_WARNING_PRINTED
    _TS_VALIDATOR_AVAILABLE = False
    if not _TS_VALIDATOR_WARNING_PRINTED:
        print(f"[ts_validator]⚠️ {message}")
        _TS_VALIDATOR_WARNING_PRINTED = True


def _configure_cache_dir(cache_dir: Path) -> bool:
    if not configure:
        return True
    try:
        configure(cache_dir=str(cache_dir))
        return True
    except TypeError as exc:
        if "unexpected keyword argument" not in str(exc):
            _mark_ts_unavailable(f"解析器缓存配置失败：{exc}")
            return False
    try:
        configure(str(cache_dir))
        return True
    except Exception as exc:
        _mark_ts_unavailable(f"解析器缓存配置失败：{exc}")
        return False


def init_ts_cache():
    """
    初始化 tree-sitter 语言包缓存。
    同时兼容开发环境和 PyInstaller 打包环境。
    提取 .zst 压缩包中的预编译解析器，并设置环境变量强制离线模式。
    """
    is_frozen = getattr(sys, 'frozen', False)
    
    # 动态将 PyInstaller 的临时目录添加到 Windows DLL 搜索路径中
    if is_frozen and sys.platform == "win32" and hasattr(os, 'add_dll_directory'):
        os.add_dll_directory(sys._MEIPASS)
        
    # 1. 定位源目录 (Source)
    if is_frozen:
        src_cache_dir = Path(sys._MEIPASS) / "ts_cache"
    else:
        src_cache_dir = Path(__file__).resolve().parent.parent / "ts_cache"

    if not src_cache_dir.exists():
        return

    platform_key = _current_platform_key()
    if platform_key is None:
        _mark_ts_unavailable(f"当前平台不支持离线语法校验：{sys.platform}/{platform.machine()}")
        return

    # 2. 定位目标目录 (Destination)
    if is_frozen:
        dst_cache_dir = Path(sys.executable).parent / "ts_cache"
    else:
        dst_cache_dir = src_cache_dir

    dst_cache_dir.mkdir(parents=True, exist_ok=True)
    libs_dir = dst_cache_dir / "libs" / platform_key
    libs_dir.mkdir(parents=True, exist_ok=True)

    # 3. 设置环境变量，强制指定缓存目录并完全禁用网络下载
    os.environ["TREE_SITTER_LANGUAGE_PACK_CACHE_DIR"] = str(libs_dir)
    os.environ["TS_PACK_CACHE_DIR"] = str(libs_dir)
    os.environ["TS_PACK_OFFLINE"] = "1"

    # 如果库已加载，必须调用其官方 configure API 通知它刷新缓存路径
    if not _configure_cache_dir(libs_dir):
        return

    if not pyzstd:
        _mark_ts_unavailable("pyzstd 不可用，语法校验解析器缓存无法解压。")
        return

    # 4. 检查并按需解压当前平台解析器包
    zst_file = src_cache_dir / f"parsers-{platform_key}.tar.zst"
    if not zst_file.exists():
        _mark_ts_unavailable(f"未找到当前平台解析器包：{zst_file.name}，语法校验将被绕过。")
        return

    marker = dst_cache_dir / f".extracted_{zst_file.name}"
    if marker.exists():
        return

    try:
        with pyzstd.open(zst_file, 'rb') as f:
            with tarfile.open(fileobj=f) as tar:
                tar.extractall(path=libs_dir, filter='data')
        marker.touch()
    except Exception as e:
        _mark_ts_unavailable(f"解析器包解压失败：{e}")


def validate_code(path: str, content: str) -> tuple[bool, str]:
    """
    校验代码语法的正确性。
    采用 Fail-Open (静默放行) 策略：
    如果找不到语言、缺少 DLL、文件被意外删除、或发生任何加载错误，均静默跳过（返回 True, ""）。
    仅在成功解析出语法树且包含明确的 has_error 时，才拦截写入，并提取精准报错。
    """
    if not _TS_VALIDATOR_AVAILABLE:
        return True, ""

    if not get_parser or not detect_language_from_path:
        print("[ts_validator]⚠️ 解析器模块未成功导入，所有校验将被绕过。")
        return True, ""

    try:
        # Ignore plain text / documentation formats to prevent false positive syntax errors
        IGNORED_EXTS = {
            ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv", ".adoc",
            ".gitignore", ".dockerignore", ".eslintignore", ".prettierignore",
            ".env", ".ini", ".cfg", ".conf"
        }
        if Path(path).suffix.lower() in IGNORED_EXTS:
            return True, ""

        lang = detect_language_from_path(path)
        if not lang:
            return True, ""

        parser = get_parser(lang)
        if not parser:
            return True, ""

        tree = parser.parse(content.encode("utf-8", errors="replace"))
        if tree.root_node.has_error:
            error_msg = f"检测到语言: {lang}"

            # 尝试利用官方 process API 提取详细的诊断报错
            if process and ProcessConfig:
                try:
                    config = ProcessConfig(
                        lang,
                        structure=False,
                        imports=False,
                        exports=False,
                        diagnostics=True
                    )
                    result = process(content, config)
                    diagnostics = result.get("diagnostics", [])

                    if diagnostics:
                        error_msg += "\n\n详细错误信息："
                        # 限制最多显示前 3 个核心错误，避免撑爆大模型上下文
                        for diag in diagnostics[:3]:
                            msg = diag.get("message", "Unknown error")
                            span = diag.get("span", {})
                            # 兼容不同版本的 span 格式返回 (利用 dict 强制转换消除 TypedDict 类型警告)
                            span_dict = dict(span)
                            start = span_dict.get("start", {}) if isinstance(span_dict.get("start"),
                                                                             dict) else span_dict
                            line = start.get("line", start.get("start_line", "?"))
                            col = start.get("column", start.get("start_column", "?"))

                            # 注意: tree-sitter 通常从 0 开始索引行号，为模型友好展示建议 +1
                            line_disp = int(line) + 1 if isinstance(line, (int, str)) and str(line).isdigit() else line

                            error_msg += f"\n- 行 {line_disp}, 列 {col}: {msg}"

                        if len(diagnostics) > 3:
                            error_msg += f"\n... (还有 {len(diagnostics) - 3} 个错误未显示)"
                except Exception:
                    pass  # 提取详细诊断失败不影响拦截，直接返回基础报错

            return False, error_msg

        return True, ""
    except Exception as e:
        # 任何异常都放行，但在控制台打印警告以便排查环境问题（例如缺少 VC++ 运行库）
        print(f"[ts_validator]⚠️ 语法校验被环境异常绕过 (Bypassed): {e}")
        return True, ""
