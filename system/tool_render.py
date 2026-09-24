import json
from typing import Any


def _format_json_lines(value: Any, indent_level: int = 0) -> list[str]:
    indent = "  " * indent_level
    child_indent = "  " * (indent_level + 1)

    if isinstance(value, dict):
        if not value:
            return [f"{indent}{{}}"]
        lines = [f"{indent}{{"]
        items = list(value.items())
        for index, (key, item) in enumerate(items):
            is_multiline = isinstance(item, str) and "\n" in item
            item_lines = _format_json_lines(
                item,
                indent_level + 2 if is_multiline else indent_level + 1,
            )
            if is_multiline:
                lines.append(
                    f"{child_indent}{json.dumps(str(key), ensure_ascii=False)}:"
                )
                lines.extend(item_lines)
            else:
                first_line = item_lines[0][len(child_indent):]
                lines.append(
                    f"{child_indent}{json.dumps(str(key), ensure_ascii=False)}: {first_line}"
                )
                lines.extend(item_lines[1:])
            if index < len(items) - 1:
                lines[-1] += ","
        lines.append(f"{indent}}}")
        return lines

    if isinstance(value, list):
        if not value:
            return [f"{indent}[]"]
        lines = [f"{indent}["]
        for index, item in enumerate(value):
            item_lines = _format_json_lines(item, indent_level + 1)
            lines.extend(item_lines)
            if index < len(value) - 1:
                lines[-1] += ","
        lines.append(f"{indent}]")
        return lines

    if isinstance(value, str) and "\n" in value:
        content_lines = value.split("\n")
        lines = [f'{indent}"']
        lines.extend(f"{indent}{line}" for line in content_lines[:-1])
        lines.append(f"{indent}{content_lines[-1]}")
        lines.append(f'{indent}"')
        return lines

    if isinstance(value, str):
        return [f'{indent}"{value}"']

    return [f"{indent}{json.dumps(value, ensure_ascii=False, default=str)}"]


def _normalize_json_for_display(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _normalize_json_for_display(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_normalize_json_for_display(item) for item in value]
    if not isinstance(value, str):
        return value

    parsed = _decode_structured_json(value)
    return value if parsed is None else parsed


def _decode_structured_json(value: str) -> Any | None:
    current: Any = value
    while isinstance(current, str):
        try:
            current = json.loads(current)
        except json.JSONDecodeError:
            return None
    if not isinstance(current, (dict, list)):
        return None
    return _normalize_json_for_display(current)


def _format_json(value: Any) -> str:
    normalized = json.loads(json.dumps(value, ensure_ascii=False, default=str))
    display_value = _normalize_json_for_display(normalized)
    return "\n".join(_format_json_lines(display_value))


def format_tool_value(value: Any) -> str:
    if isinstance(value, str):
        parsed = _decode_structured_json(value)
        if parsed is None:
            return value
        return _format_json(parsed)
    return _format_json(value)


def format_tool_arguments(value: Any) -> str:
    if isinstance(value, str):
        parsed = _decode_structured_json(value)
        if parsed is None:
            return value
        return _format_json(parsed)
    return _format_json(value)


def format_tool_call_block(
    tool_name: str,
    arguments: Any,
    *,
    tool_call_id: str = "",
) -> str:
    lines = [f"工具: {tool_name}"]
    if tool_call_id:
        lines.append(f"调用 ID: {tool_call_id}")
    lines.extend(["", "Arguments", "─────────", format_tool_arguments(arguments)])
    return "\n".join(lines)


def format_tool_result_block(
    result: Any,
    *,
    status: str = "",
    error: str = "",
) -> str:
    lines = []
    if status:
        lines.append(f"状态: {status}")
    lines.extend(["Result", "─────────", format_tool_value(result)])
    if error:
        lines.extend(["", "Error", "─────", error])
    return "\n".join(lines)


def tool_result_status(*, is_error: bool, output: Any) -> str:
    text = "" if output is None else str(output)
    if "Plan Mode" in text and ("blocked" in text or "⛔" in text):
        return "blocked"
    return "failed" if is_error else "succeeded"
