"""纯终端模式下的管理面板实现（模型、Skills、记忆、MCP、额外工具等）。"""

import json
import shlex
from typing import Any

from rich.text import Text

from system.clipboard import copy_to_system_clipboard
from system.models import MESSAGE_FORMATS, REASONING_EFFORTS
from system.tool_render import format_tool_value
from system.tui_types import (
    LAYOUT_RIGHT_KEYS,
    load_layout_ratios,
    save_layout_ratios,
)
from utils.mcp_config import parse_mcp_add_args


def _header(renderer: Any, title: str) -> None:
    renderer.write(Text(f"\n{title}", style="bold cyan"))


def _parse_index(raw: str, count: int) -> int | None:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    if 1 <= value <= count:
        return value - 1
    return None


def _split_list(value: str) -> list[str]:
    return [item.strip() for item in value.replace("，", ",").split(",") if item.strip()]


def _confirm(renderer: Any, label: str, *, default: bool = False) -> bool:
    answer = renderer.prompt(f"{label} [{'Y/n' if default else 'y/N'}]")
    if answer is None:
        return False
    answer = answer.strip().lower()
    if not answer:
        return default
    return answer in {"y", "yes", "是", "1"}


# ============================================================================
# 模型管理
# ============================================================================

def manage_models(renderer: Any, model_manager: Any) -> str:
    if model_manager is None:
        renderer.notice("模型管理器未初始化。")
        return "<cancelled>"

    while True:
        if not model_manager._reload_from_disk():
            renderer.notice(model_manager.get_load_error_display() or "模型配置读取失败。")
            return "<cancelled>"
        models = model_manager.models
        current = model_manager.get_current_model()
        current_key = current.key if current else None

        _header(renderer, "⚙️ 模型管理")
        current_text = current.get_display_text() if current else "未选择"
        renderer.write(Text(f"共 {len(models)} 个模型 · 当前：{current_text}", style="dim"))
        for index, model in enumerate(models, 1):
            marker = "●" if model.key == current_key else "○"
            favorite = " ♥" if model.is_favorite else ""
            renderer.write(
                Text(f"  {index}. {marker} {model.get_display_text()}{favorite}")
            )
            renderer.write(
                Text(
                    f"     格式：{model.message_format} · effort：{model.reasoning_effort}",
                    style="dim",
                )
            )

        command = renderer.prompt(
            "操作：序号=选择，a=添加，e 序号=编辑，d 序号=删除，"
            "f 序号=收藏，r 序号 档位，q=退出"
        )
        if command is None or command.strip().lower() in {"q", "quit", "exit"}:
            return "exit"
        parts = command.split()
        if not parts:
            continue
        operation = parts[0].lower()

        if operation.isdigit():
            index = _parse_index(operation, len(models))
            if index is None:
                renderer.notice("无效序号。")
                continue
            model = models[index]
            if model_manager.select_model(model.key, model.reasoning_effort):
                selected = model_manager.get_current_model() or model
                return (
                    f"selected:{selected.get_display_text()} · "
                    f"effort: {selected.reasoning_effort} · "
                    f"format: {selected.message_format}"
                )
            renderer.notice("切换模型失败。")
            continue

        if operation == "a":
            _add_model(renderer, model_manager)
            continue

        if operation in {"e", "d", "f", "r"}:
            if len(parts) < 2:
                renderer.notice(f"用法：{operation} <序号>")
                continue
            index = _parse_index(parts[1], len(models))
            if index is None:
                renderer.notice("无效序号。")
                continue
            model = models[index]
            if operation == "e":
                _edit_model(renderer, model_manager, model)
            elif operation == "d":
                _delete_model(renderer, model_manager, model)
            elif operation == "f":
                if model_manager.toggle_favorite_by_index(index):
                    renderer.notice("已更新常用状态。")
                else:
                    renderer.notice("更新常用状态失败。")
            else:
                effort = parts[2] if len(parts) > 2 else None
                _set_effort(renderer, model_manager, model, effort)
            continue

        renderer.notice("未知操作。")


def _prompt_model_fields(renderer: Any, source: Any = None) -> dict[str, str] | None:
    default_format = source.message_format if source else "openai_chat"
    message_format = renderer.prompt(
        f"消息格式 ({'/'.join(MESSAGE_FORMATS)})",
        default=default_format,
    )
    if message_format is None:
        return None
    message_format = message_format.strip()
    if message_format not in MESSAGE_FORMATS:
        renderer.notice(f"无效消息格式：{message_format}")
        return None

    base_url = renderer.prompt("Base URL", default=source.base_url if source else None)
    if base_url is None:
        return None
    if not base_url.strip():
        renderer.notice("Base URL 不能为空。")
        return None

    if source is not None:
        api_key = renderer.prompt("API Key（直接回车保留当前值）", secret=True)
    else:
        api_key = renderer.prompt("API Key", secret=True)
    if api_key is None:
        return None
    if not api_key.strip():
        if source is not None:
            api_key = source.api_key
        else:
            renderer.notice("API Key 不能为空。")
            return None

    model_input = renderer.prompt(
        "Model ID(s)（多个用逗号分隔）",
        default=source.model_id if source else None,
    )
    if model_input is None:
        return None
    if not model_input.strip():
        renderer.notice("Model ID 不能为空。")
        return None

    alias_input = renderer.prompt(
        "别名（可选，多个用逗号分隔）",
        default=source.alias if source else "",
    )
    if alias_input is None:
        return None

    return {
        "base_url": base_url.strip(),
        "api_key": api_key.strip(),
        "model_input": model_input.strip(),
        "alias_input": alias_input.strip(),
        "message_format": message_format,
    }


def _apply_add_model(renderer: Any, model_manager: Any) -> None:
    fields = _prompt_model_fields(renderer)
    if fields is None:
        return
    model_ids = _split_list(fields["model_input"])
    aliases = _split_list(fields["alias_input"])
    if len(model_ids) == 1:
        aliases = [aliases[0] if aliases else ""]
    new_models = model_manager.add_model(
        fields["base_url"],
        fields["api_key"],
        model_ids,
        message_format=fields["message_format"],
        aliases=aliases,
    )
    if not new_models:
        renderer.notice("添加未保存：配置可能与已有模型重复，或配置文件写入失败。")
        return
    added = "、".join(model.get_display_text() for model in new_models)
    renderer.notice(f"已添加：{added}")


def _add_model(renderer: Any, model_manager: Any) -> None:
    _apply_add_model(renderer, model_manager)


def _edit_model(renderer: Any, model_manager: Any, model: Any) -> None:
    fields = _prompt_model_fields(renderer, source=model)
    if fields is None:
        return
    updated = model_manager.update_model_by_key(
        model.key,
        fields["base_url"],
        fields["api_key"],
        fields["model_input"],
        fields["message_format"],
        fields["alias_input"],
    )
    if updated is None:
        renderer.notice("修改未保存：配置可能与已有模型重复，或配置文件写入失败。")
        return
    renderer.notice(f"已更新：{updated.get_display_text()}")


def _delete_model(renderer: Any, model_manager: Any, model: Any) -> None:
    if not _confirm(renderer, f"确认删除模型 {model.get_display_text()}？"):
        return
    if model_manager.delete_model_by_key(model.key):
        renderer.notice("模型已删除。")
    else:
        renderer.notice("删除模型失败。")


def _set_effort(renderer: Any, model_manager: Any, model: Any, effort: str | None) -> None:
    if effort is None:
        effort = renderer.prompt(
            f"新档位 ({'/'.join(REASONING_EFFORTS)})",
            default=model.reasoning_effort,
        )
    if effort is None:
        return
    effort = effort.strip()
    if effort not in REASONING_EFFORTS:
        renderer.notice(f"无效档位：{effort}")
        return
    if model_manager.set_reasoning_effort(model.key, effort):
        renderer.notice(f"effort 已设置为 {effort}。")
    else:
        renderer.notice("设置 effort 失败。")


def choose_add_model(renderer: Any) -> dict[str, str] | None:
    return _prompt_model_fields(renderer)


# ============================================================================
# Skills 管理
# ============================================================================

def manage_skills(renderer: Any, skill_loader: Any) -> str | dict[str, Any]:
    if skill_loader is None:
        renderer.notice("Skills 加载器未初始化。")
        return "<cancelled>"
    entries = skill_loader.get_skill_entries()
    if not entries:
        renderer.notice("当前工作区没有可用 Skills。")
        return "closed"

    original = {entry["name"]: bool(entry["enabled"]) for entry in entries}
    draft = dict(original)

    while True:
        _header(renderer, "📚 Skills 配置")
        enabled_count = sum(draft.values())
        renderer.write(
            Text(
                f"共 {len(entries)} 个 · 已启用 {enabled_count} · "
                f"已禁用 {len(entries) - enabled_count}",
                style="dim",
            )
        )
        for index, entry in enumerate(entries, 1):
            enabled = draft[entry["name"]]
            renderer.write(
                Text(
                    f"  {index}. [{'✓' if enabled else '×'}] {entry['name']} · "
                    f"{'已启用' if enabled else '已禁用'}"
                )
            )
            description = " ".join(str(entry.get("description", "")).split())
            if description:
                renderer.write(Text(f"     {description}", style="dim"))

        command = renderer.prompt("序号=切换状态，a=应用，q=退出")
        if command is None or command.strip().lower() in {"q", "quit", "exit"}:
            return "closed"
        command = command.strip()
        if command.lower() == "a":
            changes = {
                name: enabled
                for name, enabled in draft.items()
                if enabled != original[name]
            }
            if not changes:
                renderer.notice("没有需要应用的变更。")
                return "closed"
            try:
                skill_loader.apply_skill_enabled_states(changes)
            except (OSError, UnicodeError, ValueError) as exc:
                renderer.notice(f"保存 Skills 配置失败：{exc}")
                return "closed"
            enabled_count = sum(changes.values())
            return {
                "action": "applied",
                "enabled": enabled_count,
                "disabled": len(changes) - enabled_count,
            }
        index = _parse_index(command, len(entries))
        if index is None:
            renderer.notice("无效序号。")
            continue
        name = entries[index]["name"]
        draft[name] = not draft[name]


# ============================================================================
# 长期记忆管理
# ============================================================================

def _memory_sort_key(memory: dict[str, Any]) -> str:
    return str(memory.get("updated_at") or memory.get("created_at") or "")


def _filter_memories(memories: list[dict[str, Any]], search: str) -> list[dict[str, Any]]:
    tokens = search.casefold().split()
    if not tokens:
        return memories

    def matches(memory: dict[str, Any]) -> bool:
        haystack = " ".join(
            (
                str(memory.get("insight", "")),
                str(memory.get("reuse_condition", "")),
            )
        ).casefold()
        return all(token in haystack for token in tokens)

    return [memory for memory in memories if matches(memory)]


def _render_memory_detail(renderer: Any, memory: dict[str, Any]) -> None:
    renderer.write(Text(f"ID: {memory.get('id', '')}"))
    renderer.write(Text(f"Category: {memory.get('category', '')}"))
    renderer.write(Text(f"Created: {memory.get('created_at', '')}"))
    renderer.write(Text(f"Updated: {memory.get('updated_at', '')}"))
    renderer.write(Text(f"Insight:\n{memory.get('insight', '')}"))
    renderer.write(Text(f"Evidence:\n{memory.get('evidence', '') or '（未填写）'}"))
    renderer.write(Text(f"Reuse condition:\n{memory.get('reuse_condition', '')}"))


def manage_memories(renderer: Any, memory_provider: Any) -> list[str]:
    deleted_ids: list[str] = []
    search = ""

    while True:
        try:
            memories = sorted(
                memory_provider.list_long_term_memories(),
                key=_memory_sort_key,
                reverse=True,
            )
        except Exception as exc:
            renderer.notice(f"读取长期记忆失败：{exc}")
            return deleted_ids
        memories = _filter_memories(memories, search)

        _header(renderer, "🧠 长期记忆管理")
        summary = f"共 {len(memories)} 条 active 记忆"
        if search:
            summary += f" · 搜索：{search}"
        renderer.write(Text(summary, style="dim"))
        for index, memory in enumerate(memories, 1):
            insight = " ".join(str(memory.get("insight", "")).split())
            if len(insight) > 160:
                insight = f"{insight[:157]}..."
            renderer.write(
                Text(
                    f"  {index}. [{memory.get('category', '未分类')}] "
                    f"{insight or '（无内容）'}"
                )
            )
            renderer.write(
                Text(
                    f"     ID: {memory.get('id', '')} · "
                    f"updated: {memory.get('updated_at') or memory.get('created_at', '')}",
                    style="dim",
                )
            )

        command = renderer.prompt(
            "序号=详情，a=添加，d 序号=删除，/关键词=搜索，c=清除搜索，q=退出"
        )
        if command is None or command.strip().lower() in {"q", "quit", "exit"}:
            return deleted_ids
        command = command.strip()

        if command.lower() == "a":
            _add_memory(renderer, memory_provider)
            continue
        if command.lower() == "c":
            search = ""
            continue
        if command.startswith("/"):
            search = command[1:].strip()
            continue
        if command.lower().startswith("d "):
            index = _parse_index(command[2:].strip(), len(memories))
            if index is None:
                renderer.notice("无效序号。")
                continue
            memory = memories[index]
            if not _confirm(renderer, f"确认删除记忆 {memory.get('id', '')}？"):
                continue
            try:
                result = memory_provider.delete_long_term_memories([memory.get("id")])
            except Exception as exc:
                renderer.notice(f"删除长期记忆失败：{exc}")
                continue
            if result.get("deleted_ids"):
                deleted_ids.extend(result["deleted_ids"])
                renderer.notice(f"已删除：{', '.join(result['deleted_ids'])}")
            else:
                renderer.notice("删除失败：未找到该 active 记忆。")
            continue
        index = _parse_index(command, len(memories))
        if index is None:
            renderer.notice("未知操作。")
            continue
        _render_memory_detail(renderer, memories[index])


def _add_memory(renderer: Any, memory_provider: Any) -> None:
    category = renderer.prompt("分类（category）")
    if category is None:
        return
    insight = renderer.prompt("内容（insight）")
    if insight is None:
        return
    evidence = renderer.prompt("依据（evidence，可选）")
    if evidence is None:
        return
    reuse_condition = renderer.prompt("复用条件（reuse condition）")
    if reuse_condition is None:
        return
    if not category.strip() or not insight.strip() or not reuse_condition.strip():
        renderer.notice("分类、内容和复用条件不能为空。")
        return
    try:
        memory_provider.append_long_term_memory(
            category=category.strip(),
            insight=insight.strip(),
            evidence=evidence.strip(),
            reuse_condition=reuse_condition.strip(),
        )
    except Exception as exc:
        renderer.notice(f"添加长期记忆失败：{exc}")
        return
    renderer.notice("长期记忆已添加。")


# ============================================================================
# 记忆配置
# ============================================================================

_MEMORY_CONFIG_FIELDS = (
    ("context_length", "全局上下文长度（k tokens）"),
    ("memory_size", "长期记忆容量上限"),
    ("tool_output_compact_threshold", "第一层工具输出压缩阈值（%）"),
    ("partial_compact_threshold", "第二层局部压缩阈值（%）"),
    ("tool_output_compact_tokens", "第一层压缩后保留总 tokens（偶数）"),
    ("partial_compact_min_percent", "第二层可压缩落点下限（%）"),
    ("partial_compact_max_percent", "第二层可压缩落点上限（%）"),
    ("memory_recall_window_size", "记忆召回抑制窗口大小"),
)


def _prompt_positive_int(
        renderer: Any,
        label: str,
        default: int,
        *,
        even: bool = False,
) -> int | None:
    while True:
        raw = renderer.prompt(label, default=str(default))
        if raw is None:
            return None
        try:
            value = int(raw)
        except (TypeError, ValueError):
            renderer.notice("请输入大于 0 的整数。")
            continue
        if value <= 0:
            renderer.notice("请输入大于 0 的整数。")
            continue
        if even and value % 2:
            renderer.notice("请输入正偶数。")
            continue
        return value


def manage_memory_config(
        renderer: Any,
        values: dict[str, Any],
        model_manager: Any = None,
) -> str | dict[str, Any]:
    current = dict(values)
    while True:
        result = dict(current)
        for field, label in _MEMORY_CONFIG_FIELDS:
            value = _prompt_positive_int(
                renderer,
                f"{label} ({field})",
                int(current.get(field, 1)),
                even=(field == "tool_output_compact_tokens"),
            )
            if value is None:
                return "<cancelled>"
            result[field] = value

        if not (
            0
            < result["tool_output_compact_threshold"]
            < result["partial_compact_threshold"]
            < 100
        ):
            renderer.notice("压缩阈值必须满足 0 < 第一层阈值 < 第二层阈值 < 100。")
            continue
        if not (
            0
            < result["partial_compact_min_percent"]
            < result["partial_compact_max_percent"]
            < 100
        ):
            renderer.notice("第二层可压缩落点必须满足 0 < 下限 < 上限 < 100。")
            continue
        break

    pre_recall = renderer.prompt(
        "记忆预召回 (y/n)",
        default="y" if current.get("memory_pre_recall") else "n",
    )
    if pre_recall is None:
        return "<cancelled>"
    result["memory_pre_recall"] = pre_recall.strip().lower() in {
        "y",
        "yes",
        "1",
        "on",
        "是",
        "启用",
    }

    result["memory_recall_model_key"] = current.get("memory_recall_model_key")
    result["memory_recall_model_display"] = current.get(
        "memory_recall_model_display",
        "同主模型",
    )
    if model_manager is not None:
        model_manager._reload_from_disk()
        models = model_manager.models
        options = ["使用主模型（同主模型）"] + [
            model.get_display_text() for model in models
        ]
        keys = [None] + [model.key for model in models]
        default_index = 0
        if result["memory_recall_model_key"] in keys:
            default_index = keys.index(result["memory_recall_model_key"])
        _header(renderer, "记忆管理模型")
        for index, option in enumerate(options, 1):
            marker = "●" if index - 1 == default_index else "○"
            renderer.write(Text(f"  {index}. {marker} {option}"))
        raw_index = renderer.prompt("模型序号", default=str(default_index + 1))
        if raw_index is None:
            return "<cancelled>"
        try:
            selected_index = int(raw_index) - 1
        except ValueError:
            selected_index = default_index
        if not (0 <= selected_index < len(keys)):
            selected_index = default_index
        result["memory_recall_model_key"] = keys[selected_index]
        result["memory_recall_model_display"] = options[selected_index]

    return result


# ============================================================================
# 额外工具
# ============================================================================

def manage_extra_tools(renderer: Any, values: dict[str, Any]) -> str | dict[str, Any]:
    from system.models import get_model_manager
    from tools.extra_tools import set_understand_image_config

    enabled = bool(values.get("enabled", False))
    model_key = values.get("model_key")
    model_display = values.get("model_display", "同主模型")
    model_manager = get_model_manager()

    while True:
        _header(renderer, "🔧 额外工具")
        renderer.write(
            Text(f"UnderstandImage：{'已启用' if enabled else '已关闭'}")
        )
        renderer.write(Text(f"处理模型：{model_display}"))
        command = renderer.prompt("t=切换启用，m=选择模型，q=关闭")
        if command is None or command.strip().lower() in {"q", "quit", "exit"}:
            return "<closed>"
        command = command.strip().lower()

        if command == "t":
            new_enabled = not enabled
            if set_understand_image_config(new_enabled, model_key):
                enabled = new_enabled
                renderer.notice(f"UnderstandImage 已{'启用' if enabled else '关闭'}。")
            else:
                renderer.notice("保存额外工具配置失败。")
            continue

        if command == "m":
            if model_manager is None:
                renderer.notice("模型管理器未初始化。")
                continue
            model_manager._reload_from_disk()
            models = model_manager.models
            options = ["使用主模型（同主模型）"] + [
                model.get_display_text() for model in models
            ]
            keys = [None] + [model.key for model in models]
            default_index = 0
            if model_key in keys:
                default_index = keys.index(model_key)
            for index, option in enumerate(options, 1):
                marker = "●" if index - 1 == default_index else "○"
                renderer.write(Text(f"  {index}. {marker} {option}"))
            raw_index = renderer.prompt("模型序号", default=str(default_index + 1))
            if raw_index is None:
                return "<closed>"
            try:
                selected_index = int(raw_index) - 1
            except ValueError:
                selected_index = default_index
            if not (0 <= selected_index < len(keys)):
                selected_index = default_index
            selected_key = keys[selected_index]
            if set_understand_image_config(enabled, selected_key):
                model_key = selected_key
                model_display = options[selected_index]
                renderer.notice(f"处理模型已设置为：{model_display}")
            else:
                renderer.notice("保存额外工具配置失败。")
            continue

        renderer.notice("未知操作。")


# ============================================================================
# MCP 服务管理
# ============================================================================

def _reload_mcp_servers(mcp_manager: Any, draft: dict[str, bool]) -> list[dict[str, Any]]:
    try:
        servers = mcp_manager.list_server_switches()
    except FileNotFoundError:
        servers = []
    current_names = {item["name"] for item in servers}
    for name in list(draft):
        if name not in current_names:
            draft.pop(name)
    for item in servers:
        draft.setdefault(item["name"], bool(item.get("disabled", False)))
    return servers


def _render_mcp_servers(
        renderer: Any,
        servers: list[dict[str, Any]],
        draft: dict[str, bool],
) -> None:
    _header(renderer, "🔌 MCP 服务管理")
    if not servers:
        renderer.notice("当前没有已配置的 MCP 服务。")
        return
    for index, item in enumerate(servers, 1):
        name = item["name"]
        disabled = draft.get(name, False)
        enabled_text = "禁用" if disabled else "启用"
        runtime = item.get("runtime_state") or (
            "loaded" if item.get("loaded") else "unloaded"
        )
        renderer.write(
            Text(
                f"  {index}. {'○' if disabled else '●'} {name} · "
                f"草稿：{enabled_text} · 运行：{runtime} · "
                f"工具：{item.get('tool_count', 0)}"
            )
        )
        renderer.write(
            Text(
                f"     协议：{item.get('transport', '未知')} · "
                f"目标：{item.get('target', '未配置')}",
                style="dim",
            )
        )


def _manage_mcp_tools(renderer: Any, mcp_manager: Any, server_name: str) -> None:
    try:
        data = mcp_manager.list_tool_switches(server_name)
    except Exception as exc:
        renderer.notice(f"读取工具列表失败：{exc}")
        return
    tools = data.get("tools") or []
    if not tools:
        renderer.notice("该服务当前没有可管理的工具（可能尚未连接）。")
        return

    disabled = {
        tool["original_name"]
        for tool in tools
        if tool.get("disabled") and tool.get("original_name")
    }
    while True:
        _header(renderer, f"🛠️ {server_name} · MCP 工具")
        for index, tool in enumerate(tools, 1):
            state = "已禁用" if tool.get("original_name") in disabled else "已启用"
            renderer.write(Text(f"  {index}. [{state}] {tool.get('name', '')}"))
            if tool.get("description"):
                renderer.write(Text(f"     {tool['description']}", style="dim"))
        command = renderer.prompt("序号=切换，y=应用，q=返回")
        if command is None or command.strip().lower() in {"q", "quit", "exit"}:
            return
        command = command.strip()
        if command.lower() == "y":
            try:
                result = mcp_manager.apply_tool_switches(server_name, sorted(disabled))
            except Exception as exc:
                renderer.notice(f"应用工具开关失败：{exc}")
                return
            renderer.notice(result.get("message", "工具开关已保存。"))
            return
        index = _parse_index(command, len(tools))
        if index is None:
            renderer.notice("无效序号。")
            continue
        original_name = tools[index].get("original_name")
        if not original_name:
            renderer.notice("该工具缺少原始名称，无法切换。")
            continue
        if original_name in disabled:
            disabled.remove(original_name)
        else:
            disabled.add(original_name)


def _default_mcp_args(name: str, cfg: dict[str, Any]) -> str:
    parts = [name]
    if cfg.get("url"):
        parts.extend(["--url", str(cfg["url"])])
    if cfg.get("transport"):
        parts.extend(["--transport", str(cfg["transport"])])
    for key, value in (cfg.get("env") or {}).items():
        parts.extend(["--env", f"{key}={value}"])
    for key, value in (cfg.get("headers") or {}).items():
        parts.extend(["--header", f"{key}={value}"])
    if cfg.get("cwd"):
        parts.extend(["--cwd", str(cfg["cwd"])])
    if cfg.get("auth"):
        parts.extend(["--auth", str(cfg["auth"])])
    if cfg.get("timeout") is not None:
        parts.extend(["--timeout", str(cfg["timeout"])])
    if cfg.get("sse_read_timeout") is not None:
        parts.extend(["--sse-read-timeout", str(cfg["sse_read_timeout"])])
    if cfg.get("keep_alive") is not None:
        parts.extend(["--keep-alive", "true" if cfg["keep_alive"] else "false"])
    if cfg.get("command"):
        parts.extend([
            "--",
            str(cfg["command"]),
            *[str(item) for item in cfg.get("args") or []],
        ])
    return " ".join(shlex.quote(part) for part in parts)


def _prompt_mcp_args(renderer: Any, default: str = "") -> tuple[str, dict] | None:
    renderer.write(
        Text(
            "参数格式与 /mcp-add 相同，例如：fs -- npx -y @server/pkg "
            "或 api --url https://example.com/mcp --header X-Api-Key=xxx",
            style="dim",
        )
    )
    raw = renderer.prompt("MCP 服务参数", default=default or None)
    if raw is None:
        return None
    try:
        tokens = shlex.split(raw)
    except ValueError as exc:
        renderer.notice(f"参数解析失败：{exc}")
        return None
    try:
        return parse_mcp_add_args(tokens)
    except ValueError as exc:
        renderer.notice(f"参数无效：{exc}")
        return None


def choose_mcp_switch(
        renderer: Any,
        server_switches: list[dict[str, Any]],
        mcp_manager: Any,
) -> str | dict:
    if mcp_manager is None:
        renderer.notice("MCP 管理器未初始化。")
        return {"action": "cancel"}

    draft = {
        item["name"]: bool(item.get("disabled", False))
        for item in server_switches
    }
    servers = _reload_mcp_servers(mcp_manager, draft)

    while True:
        _render_mcp_servers(renderer, servers, draft)
        command = renderer.prompt(
            "序号=切换，t 序号=工具，v=总览，a=添加，e 序号=编辑，"
            "d 序号=删除，y=应用，q=取消"
        )
        if command is None or command.strip().lower() in {"q", "quit", "exit"}:
            return {
                "action": "cancel",
                "disabled_updates": {},
                "deleted_results": [],
                "added_results": [],
                "updated_results": [],
                "tool_results": [],
            }
        command = command.strip()
        parts = command.split()
        operation = parts[0].lower()

        if operation == "y":
            return {
                "action": "confirm",
                "disabled_updates": dict(draft),
                "deleted_results": [],
                "added_results": [],
                "updated_results": [],
                "tool_results": [],
            }

        if operation == "v":
            status = mcp_manager.get_status_info()
            renderer.write(
                Text(
                    f"服务总数：{len(status.get('config_servers', []))} · "
                    f"已启用：{len(status.get('enabled_config_servers', []))} · "
                    f"已加载：{len(status.get('loaded_servers', []))} · "
                    f"工具：{status.get('total_tool_count', status.get('tool_count', 0))}"
                )
            )
            continue

        if operation == "a":
            parsed = _prompt_mcp_args(renderer)
            if parsed is None:
                continue
            name, cfg = parsed
            try:
                result = mcp_manager.add_server_config(name, cfg)
            except Exception as exc:
                renderer.notice(f"添加 MCP 服务失败：{exc}")
                continue
            renderer.notice(result.get("message", f"已添加 MCP 服务：{name}"))
            servers = _reload_mcp_servers(mcp_manager, draft)
            continue

        if operation in {"t", "e", "d"} and len(parts) < 2:
            renderer.notice(f"用法：{operation} <序号>")
            continue

        if operation in {"t", "e", "d"}:
            index = _parse_index(parts[1], len(servers))
            if index is None:
                renderer.notice("无效序号。")
                continue
            server_name = servers[index]["name"]
            if operation == "t":
                _manage_mcp_tools(renderer, mcp_manager, server_name)
                servers = _reload_mcp_servers(mcp_manager, draft)
                continue
            if operation == "e":
                try:
                    cfg = mcp_manager.get_server_config(server_name)
                except Exception as exc:
                    renderer.notice(f"读取服务配置失败：{exc}")
                    continue
                parsed = _prompt_mcp_args(
                    renderer,
                    default=_default_mcp_args(server_name, cfg),
                )
                if parsed is None:
                    continue
                new_name, new_cfg = parsed
                known_keys = {
                    "command",
                    "args",
                    "url",
                    "transport",
                    "cwd",
                    "auth",
                    "timeout",
                    "sse_read_timeout",
                    "keep_alive",
                    "disabled",
                    "env",
                    "headers",
                    "disabledTools",
                }
                for key, value in cfg.items():
                    if key not in known_keys:
                        new_cfg.setdefault(key, value)
                try:
                    result = mcp_manager.update_server_config(
                        server_name,
                        new_name,
                        new_cfg,
                    )
                except Exception as exc:
                    renderer.notice(f"更新 MCP 服务失败：{exc}")
                    continue
                renderer.notice(result.get("message", "MCP 服务配置已更新。"))
                servers = _reload_mcp_servers(mcp_manager, draft)
                continue
            if not _confirm(renderer, f"确认删除 MCP 服务 {server_name}？"):
                continue
            try:
                result = mcp_manager.delete_server_config(server_name)
            except Exception as exc:
                renderer.notice(f"删除 MCP 服务失败：{exc}")
                continue
            renderer.notice(result.get("message", "MCP 服务已删除。"))
            servers = _reload_mcp_servers(mcp_manager, draft)
            continue

        index = _parse_index(operation, len(servers))
        if index is None:
            renderer.notice("未知操作。")
            continue
        name = servers[index]["name"]
        draft[name] = not draft.get(name, bool(servers[index].get("disabled", False)))


# ============================================================================
# 布局
# ============================================================================

def manage_layout(renderer: Any) -> str | dict[str, int]:
    ratios = load_layout_ratios()
    _header(renderer, "🧩 Layout 布局比例")
    renderer.write(
        Text("右侧面板高度比例，取值 0-10；0 表示隐藏但继续接收渲染。", style="dim")
    )
    for key in LAYOUT_RIGHT_KEYS:
        raw = renderer.prompt(
            f"{key} 比例 (0-10)",
            default=str(ratios.get(key, 0)),
        )
        if raw is None:
            return "<cancelled>"
        try:
            value = int(raw)
        except ValueError:
            renderer.notice(f"{key} 必须是 0-10 的整数。")
            return "<cancelled>"
        ratios[key] = min(max(value, 0), 10)
    try:
        save_layout_ratios(ratios)
    except (OSError, ValueError) as exc:
        renderer.notice(f"保存布局配置失败：{exc}")
        return "<cancelled>"
    return dict(ratios)


# ============================================================================
# 对话复制
# ============================================================================

def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str) and content:
        return content
    if isinstance(content, list):
        chunks = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict)
            and block.get("type") == "text"
            and block.get("text")
        ]
        if chunks:
            return "\n\n".join(chunks)
    return "".join(
        block.get("text", "")
        for block in message.get("content_blocks") or []
        if isinstance(block, dict) and block.get("type") == "text"
    )


def _tool_calls(message: dict[str, Any]) -> list[dict[str, Any]]:
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list) and tool_calls:
        return [call for call in tool_calls if isinstance(call, dict)]
    return [
        block
        for block in message.get("content_blocks") or []
        if isinstance(block, dict) and block.get("type") == "tool_call"
    ]


def _tool_call_parts(tool_call: dict[str, Any]) -> tuple[str, Any]:
    function = tool_call.get("function")
    if isinstance(function, dict):
        return function.get("name", ""), function.get("arguments", "")
    return tool_call.get("name", ""), tool_call.get("arguments", "")


def _terminal_commands(message: dict[str, Any]) -> list[str]:
    commands: list[str] = []
    for tool_call in _tool_calls(message):
        name, arguments = _tool_call_parts(tool_call)
        if name != "RunTerminalCommand":
            continue
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                continue
        if not isinstance(arguments, dict):
            continue
        command = arguments.get("command")
        if isinstance(command, str) and command:
            commands.append(command)
    return commands


def _copy_sections(messages: list[dict[str, Any]]) -> list[tuple[str, str]]:
    sections: list[tuple[str, str]] = []
    user_number = 0
    assistant_number = 0
    terminal_input_number = 0
    terminal_output_number = 0
    for message in messages:
        role = message.get("role", "")
        if role == "user":
            content = _message_text(message)
            if content:
                user_number += 1
                sections.append((f"User · {user_number}", content))
            continue
        if role == "assistant":
            content = _message_text(message)
            if content:
                assistant_number += 1
                sections.append((f"Assistant · {assistant_number}", content))
            for command in _terminal_commands(message):
                terminal_input_number += 1
                sections.append((f"Terminal Input · {terminal_input_number}", f"$ {command}"))
            continue
        if role in {"tool", "function"} and message.get("name") == "RunTerminalCommand":
            output = message.get("content")
            if output is None:
                output = message.get("output")
            if output is None:
                continue
            terminal_output_number += 1
            output_text = output if isinstance(output, str) else format_tool_value(output)
            sections.append((f"Terminal Output · {terminal_output_number}", output_text))
    return sections


def show_copy_content(renderer: Any, messages: list[dict[str, Any]]) -> str:
    sections = _copy_sections(messages)
    if not sections:
        renderer.notice("当前对话暂无可查看的内容。")
        return "closed"
    text = "\n\n".join(f"[{label}]\n{content}" for label, content in sections)
    for label, content in sections:
        renderer.write(Text(f"[{label}]\n{content}"))
    renderer.write(Text(f"共 {len(text)} 个字符。", style="dim"))
    answer = renderer.prompt("c=复制全部到系统剪贴板，q=关闭", default="q")
    if answer is not None and answer.strip().lower() == "c":
        if copy_to_system_clipboard(text):
            renderer.notice(f"已复制全部内容（{len(text)} 个字符）到系统剪贴板。")
        else:
            renderer.notice("系统剪贴板不可用，请直接选择终端文本复制。")
    return "closed"


# ============================================================================
# 模型选择器
# ============================================================================

def _choose_with_select_prefix(
        renderer: Any,
        title: str,
        options: list[str],
) -> str:
    _header(renderer, title)
    for index, option in enumerate(options, 1):
        renderer.write(Text(f"  {index}. {option}"))
    raw = renderer.prompt("序号")
    if raw is None:
        return "<cancelled>"
    index = _parse_index(raw, len(options))
    if index is None:
        return "<cancelled>"
    return f"select:{index}"


def choose_recall_model(renderer: Any, options: list[str]) -> str:
    return _choose_with_select_prefix(renderer, "🧠 选择记忆管理模型", options)


def choose_image_understanding_model(renderer: Any, options: list[str]) -> str:
    return _choose_with_select_prefix(renderer, "🖼️ 选择图片理解模型", options)
