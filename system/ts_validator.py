import os
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


def init_ts_cache():
    """
    初始化 tree-sitter 语言包缓存。
    同时兼容开发环境和 PyInstaller 打包环境。
    提取 .zst 压缩包中的预编译解析器，并设置环境变量强制离线模式。
    """
    is_frozen = getattr(sys, 'frozen', False)
    
    # 1. 定位源目录 (Source)
    if is_frozen:
        src_cache_dir = Path(sys._MEIPASS) / "ts_cache"
    else:
        src_cache_dir = Path(__file__).resolve().parent.parent / "ts_cache"

    if not src_cache_dir.exists():
        return

    # 2. 定位目标目录 (Destination)
    if is_frozen:
        dst_cache_dir = Path(sys.executable).parent / "ts_cache"
    else:
        dst_cache_dir = src_cache_dir
        
    dst_cache_dir.mkdir(parents=True, exist_ok=True)
    libs_dir = dst_cache_dir / "libs"
    libs_dir.mkdir(parents=True, exist_ok=True)

    # 3. 设置环境变量，强制指定缓存目录并完全禁用网络下载
    os.environ["TREE_SITTER_LANGUAGE_PACK_CACHE_DIR"] = str(libs_dir)
    os.environ["TS_PACK_CACHE_DIR"] = str(libs_dir)
    os.environ["TS_PACK_OFFLINE"] = "1"

    # 如果库已加载，必须调用其官方 configure API 通知它刷新缓存路径
    if configure:
        configure(cache_dir=str(libs_dir))

    if not pyzstd:
        return

    # 4. 检查并按需解压
    for zst_file in src_cache_dir.glob("*.tar.zst"):
        # 忽略已经标记的占位文件，防止死循环解压占位文件自己
        if zst_file.name.startswith(".extracted_"):
            continue
            
        marker = dst_cache_dir / f".extracted_{zst_file.name}"
        if marker.exists():
            continue

        try:
            with pyzstd.open(zst_file, 'rb') as f:
                with tarfile.open(fileobj=f) as tar:
                    tar.extractall(path=libs_dir, filter='data')
            marker.touch()
        except Exception as e:
            print(f"[ts_validator] Failed to extract {zst_file}: {e}")


def validate_code(path: str, content: str) -> tuple[bool, str]:
    """
    校验代码语法的正确性。
    采用 Fail-Open (静默放行) 策略：
    如果找不到语言、缺少 DLL、文件被意外删除、或发生任何加载错误，均静默跳过（返回 True, ""）。
    仅在成功解析出语法树且包含明确的 has_error 时，才拦截写入，并提取精准报错。
    """
    if not get_parser or not detect_language_from_path:
        return True, ""

    try:
        lang = detect_language_from_path(path)
        if not lang:
            return True, ""

        parser = get_parser(lang)
        if not parser:
            return True, ""

        tree = parser.parse(content.encode("utf-8", errors="replace"))
        if tree.root_node.has_error:
            error_msg = f"文件修改后存在语法错误(Syntax error)，已被拦截。检测到语言: {lang}"
            
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
                        # 限制最多显示前 5 个核心错误，避免撑爆大模型上下文
                        for diag in diagnostics[:5]:
                            msg = diag.get("message", "Unknown error")
                            span = diag.get("span", {})
                            
                            # 兼容不同版本的 span 格式返回
                            start = span.get("start", {}) if isinstance(span.get("start"), dict) else span
                            line = start.get("line", start.get("start_line", "?"))
                            col = start.get("column", start.get("start_column", "?"))
                            
                            # 注意: tree-sitter 通常从 0 开始索引行号，为模型友好展示建议 +1
                            line_disp = int(line) + 1 if isinstance(line, (int, str)) and str(line).isdigit() else line
                            
                            error_msg += f"\n- 行 {line_disp}, 列 {col}: {msg}"
                            
                        if len(diagnostics) > 5:
                            error_msg += f"\n... (还有 {len(diagnostics) - 5} 个错误未显示)"
                except Exception:
                    pass # 提取详细诊断失败不影响拦截，直接返回基础报错

            return False, error_msg

        return True, ""
    except Exception:
        # 任何异常（例如缺失 DLL、不支持的新语言等）都直接放行，绝不阻塞正常的文件操作
        return True, ""
