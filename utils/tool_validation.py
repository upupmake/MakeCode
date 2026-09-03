import json
from collections.abc import Mapping
from typing import Any

from openai import pydantic_function_tool
from pydantic import BaseModel, ConfigDict, ValidationError


class ToolArgumentsModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolArgumentValidationError(ValueError):
    def __init__(self, tool_name: str, message: str):
        self.tool_name = tool_name
        super().__init__(message)


# HITL declines return this prefix instead of raising, so classification must recognise it.
TOOL_DENIAL_MARKER = "User Denied Execution."


def is_tool_error_output(output: Any) -> bool:
    """Classify a tool handler's return value for ``is_error`` reporting.

    Handlers signal failure without raising in three ways: an ``Error:`` prefixed string, the
    HITL denial string, or an error-shaped mapping. Exceptions are flagged by the callers.
    """
    if isinstance(output, str):
        return output.startswith("Error:") or output.startswith(TOOL_DENIAL_MARKER)
    if isinstance(output, Mapping):
        return output.get("status") == "error" or "error" in output
    return False


def _format_location(location: tuple[Any, ...]) -> str:
    if not location:
        return "$"

    result = ""
    for item in location:
        if isinstance(item, int):
            result += f"[{item}]"
        elif result:
            result += f".{item}"
        else:
            result = str(item)
    return result


def _format_validation_error(
        tool_name: str,
        model: type[ToolArgumentsModel],
        errors: list[dict[str, Any]],
) -> str:
    lines = [f"Error: Invalid arguments for {tool_name}."]
    for error in errors:
        location = _format_location(tuple(error.get("loc", ())))
        message = str(error.get("msg", "Invalid value"))
        error_type = error.get("type", "validation_error")
        input_value = "<missing>" if error_type == "missing" else repr(error.get("input"))
        lines.append(
            f"- {location}: {message} [{error_type}]\n"
            f"  Input value: {input_value}"
        )

    expected_fields = tuple(model.model_fields)
    if expected_fields:
        lines.append(f"Expected fields: {', '.join(expected_fields)}.")
    return "\n".join(lines)


def parse_tool_arguments(tool_name: str, raw_arguments: Any) -> dict[str, Any]:
    if isinstance(raw_arguments, Mapping):
        return dict(raw_arguments)

    if raw_arguments is None:
        return {}

    if isinstance(raw_arguments, str):
        payload = raw_arguments.strip()
        if not payload:
            return {}
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError as exc:
            json_error_message = exc.msg
        else:
            if isinstance(parsed, dict):
                return parsed
            raise ToolArgumentValidationError(
                tool_name,
                f"Error: Invalid arguments for {tool_name}.\n"
                f"- $: Arguments must be a JSON object, got {type(parsed).__name__} [object_required]\n"
                f"  Input value: {parsed!r}",
            )
        raise ToolArgumentValidationError(
            tool_name,
            f"Error: Invalid arguments for {tool_name}.\n"
            f"- $: Invalid JSON: {json_error_message} [json_invalid]\n"
            f"  Input value: {raw_arguments!r}",
        )

    raise ToolArgumentValidationError(
        tool_name,
        f"Error: Invalid arguments for {tool_name}.\n"
        f"- $: Arguments must be a JSON object, got {type(raw_arguments).__name__} [object_required]\n"
        f"  Input value: {raw_arguments!r}",
    )


def validate_builtin_tool_arguments(
        tool_name: str,
        raw_arguments: Any,
        model: type[ToolArgumentsModel],
) -> dict[str, Any]:
    arguments = parse_tool_arguments(tool_name, raw_arguments)
    try:
        validated = model.model_validate(arguments)
    except ValidationError as exc:
        validation_message = _format_validation_error(tool_name, model, exc.errors())
    else:
        return validated.model_dump(mode="python")
    raise ToolArgumentValidationError(tool_name, validation_message)


def build_tool_model_registry(
        *models: type[ToolArgumentsModel],
) -> dict[str, type[ToolArgumentsModel]]:
    registry: dict[str, type[ToolArgumentsModel]] = {}
    for model in models:
        if not isinstance(model, type) or not issubclass(model, ToolArgumentsModel):
            raise TypeError(f"Tool model must inherit ToolArgumentsModel: {model!r}")
        tool_name = model.__name__
        if tool_name in registry:
            raise ValueError(f"Duplicate built-in tool model: {tool_name}")
        registry[tool_name] = model
    return registry


def build_tool_definitions(
        *models: type[ToolArgumentsModel],
) -> tuple[list[dict[str, Any]], dict[str, type[ToolArgumentsModel]]]:
    registry = build_tool_model_registry(*models)
    return [pydantic_function_tool(model) for model in models], registry


def merge_tool_model_registries(
        *registries: Mapping[str, type[ToolArgumentsModel]],
) -> dict[str, type[ToolArgumentsModel]]:
    merged: dict[str, type[ToolArgumentsModel]] = {}
    for registry in registries:
        for tool_name, model in registry.items():
            if tool_name in merged:
                raise ValueError(f"Duplicate built-in tool model: {tool_name}")
            if not isinstance(model, type) or not issubclass(model, ToolArgumentsModel):
                raise TypeError(f"Tool model must inherit ToolArgumentsModel: {model!r}")
            merged[tool_name] = model
    return merged
