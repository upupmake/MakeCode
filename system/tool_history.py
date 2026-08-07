import json
import threading
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any


TOOL_STATUS_RUNNING = "running"
TOOL_STATUS_SUCCEEDED = "succeeded"
TOOL_STATUS_FAILED = "failed"
TOOL_STATUS_BLOCKED = "blocked"
TOOL_STATUS_COMPACTED = "compacted"
TOOL_STATUS_INCOMPLETE = "incomplete"


def _now() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def _contains_multiline_string(value: Any) -> bool:
    if isinstance(value, str):
        return "\n" in value
    if isinstance(value, dict):
        return any(_contains_multiline_string(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_multiline_string(item) for item in value)
    return False


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
    if not _contains_multiline_string(display_value):
        return json.dumps(display_value, ensure_ascii=False, indent=2, default=str)
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


def tool_result_status(*, is_error: bool, output: Any) -> str:
    text = "" if output is None else str(output)
    if "Plan Mode" in text and ("blocked" in text or "⛔" in text):
        return TOOL_STATUS_BLOCKED
    return TOOL_STATUS_FAILED if is_error else TOOL_STATUS_SUCCEEDED


@dataclass(frozen=True)
class ToolExecutionRecord:
    sequence: int
    execution_id: str
    tool_call_id: str
    tool_name: str
    source: str
    actor: str
    task_id: str | None
    started_at: str | None
    finished_at: str | None
    duration_ms: int | None
    status: str
    arguments: Any
    result: Any = None
    error: str = ""
    recovered: bool = False


@dataclass(frozen=True)
class ToolExecutionSummary:
    tool_name: str
    total: int
    succeeded: int
    failed: int
    blocked: int
    running: int
    last_sequence: int


@dataclass(frozen=True)
class ToolOutputTokenUsage:
    tool_name: str
    output_count: int
    tokens: int


class ToolExecutionHistory:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._records: dict[str, ToolExecutionRecord] = {}
        self._started_monotonic: dict[str, float] = {}
        self._next_sequence = 1

    def start(
        self,
        tool_name: str,
        arguments: Any,
        *,
        tool_call_id: str = "",
        source: str = "orchestrator",
        actor: str = "Orchestrator",
        task_id: str | None = None,
    ) -> str:
        with self._lock:
            execution_id = uuid.uuid4().hex
            record = ToolExecutionRecord(
                sequence=self._next_sequence,
                execution_id=execution_id,
                tool_call_id=tool_call_id,
                tool_name=tool_name or "Unknown tool",
                source=source,
                actor=actor,
                task_id=task_id,
                started_at=_now(),
                finished_at=None,
                duration_ms=None,
                status=TOOL_STATUS_RUNNING,
                arguments=arguments,
            )
            self._next_sequence += 1
            self._records[execution_id] = record
            self._started_monotonic[execution_id] = time.monotonic()
            return execution_id

    def finish(
        self,
        execution_id: str,
        result: Any = None,
        *,
        status: str = TOOL_STATUS_SUCCEEDED,
        error: str = "",
    ) -> ToolExecutionRecord:
        with self._lock:
            record = self._records[execution_id]
            started = self._started_monotonic.pop(execution_id, None)
            duration_ms = None if started is None else max(0, round((time.monotonic() - started) * 1000))
            updated = replace(
                record,
                finished_at=_now(),
                duration_ms=duration_ms,
                status=status,
                result=result,
                error=error,
            )
            self._records[execution_id] = updated
            return updated

    def clear(self) -> None:
        with self._lock:
            self._records.clear()
            self._started_monotonic.clear()
            self._next_sequence = 1

    def snapshot(self, *, newest_first: bool = True) -> list[ToolExecutionRecord]:
        with self._lock:
            records = list(self._records.values())
        return sorted(records, key=lambda item: item.sequence, reverse=newest_first)

    def query(
        self,
        *,
        text: str = "",
        tool_name: str = "",
        status: str = "",
        source: str = "",
        newest_first: bool = True,
    ) -> list[ToolExecutionRecord]:
        query_text = text.strip().casefold()
        records = self.snapshot(newest_first=newest_first)
        filtered = []
        for record in records:
            if tool_name and record.tool_name != tool_name:
                continue
            if status and record.status != status:
                continue
            if source and record.source != source:
                continue
            if query_text and query_text not in self._search_text(record):
                continue
            filtered.append(record)
        return filtered

    def summaries(
        self,
        *,
        text: str = "",
        status: str = "",
        source: str = "",
    ) -> list[ToolExecutionSummary]:
        grouped: dict[str, list[ToolExecutionRecord]] = {}
        for record in self.query(text=text, status=status, source=source):
            grouped.setdefault(record.tool_name, []).append(record)
        summaries = []
        for tool_name, records in grouped.items():
            summaries.append(
                ToolExecutionSummary(
                    tool_name=tool_name,
                    total=len(records),
                    succeeded=sum(item.status == TOOL_STATUS_SUCCEEDED for item in records),
                    failed=sum(item.status == TOOL_STATUS_FAILED for item in records),
                    blocked=sum(item.status == TOOL_STATUS_BLOCKED for item in records),
                    running=sum(item.status == TOOL_STATUS_RUNNING for item in records),
                    last_sequence=max(item.sequence for item in records),
                )
            )
        return sorted(summaries, key=lambda item: (-item.total, -item.last_sequence, item.tool_name.casefold()))

    def output_token_usage(
        self,
        token_counter,
        *,
        source: str = "orchestrator",
    ) -> tuple[list[ToolOutputTokenUsage], int]:
        grouped: dict[str, list[int]] = {}
        for record in self.query(source=source, newest_first=False):
            if record.result is None:
                continue
            output = (
                record.result
                if isinstance(record.result, str)
                else json.dumps(record.result, ensure_ascii=False, default=str)
            )
            values = grouped.setdefault(record.tool_name, [0, 0])
            values[0] += 1
            values[1] += token_counter(output)

        usage = [
            ToolOutputTokenUsage(
                tool_name=tool_name,
                output_count=values[0],
                tokens=values[1],
            )
            for tool_name, values in grouped.items()
        ]
        usage.sort(key=lambda item: (-item.tokens, item.tool_name.casefold()))
        return usage, sum(item.tokens for item in usage)

    def rebuild_from_messages(self, messages: list[dict[str, Any]]) -> int:
        recovered: list[ToolExecutionRecord] = []
        by_call_id: dict[str, int] = {}
        sequence = 1

        for message_index, message in enumerate(messages):
            role = message.get("role")
            if role == "assistant":
                for call_index, tool_call in enumerate(message.get("tool_calls") or []):
                    call_id, name, arguments = self._tool_call_parts(tool_call)
                    call_id = call_id or f"conversation-{message_index}-{call_index}"
                    recovered.append(
                        ToolExecutionRecord(
                            sequence=sequence,
                            execution_id=f"recovered-{call_id}-{sequence}",
                            tool_call_id=call_id,
                            tool_name=name or "Unknown tool",
                            source="orchestrator",
                            actor="Orchestrator",
                            task_id=None,
                            started_at=None,
                            finished_at=None,
                            duration_ms=None,
                            status=TOOL_STATUS_INCOMPLETE,
                            arguments=arguments,
                            recovered=True,
                        )
                    )
                    by_call_id[call_id] = len(recovered) - 1
                    sequence += 1
            elif message.get("type") == "function_call":
                call_id = message.get("call_id") or f"conversation-{message_index}"
                recovered.append(
                    ToolExecutionRecord(
                        sequence=sequence,
                        execution_id=f"recovered-{call_id}-{sequence}",
                        tool_call_id=call_id,
                        tool_name=message.get("name") or "Unknown tool",
                        source="orchestrator",
                        actor="Orchestrator",
                        task_id=None,
                        started_at=None,
                        finished_at=None,
                        duration_ms=None,
                        status=TOOL_STATUS_INCOMPLETE,
                        arguments=message.get("arguments", ""),
                        recovered=True,
                    )
                )
                by_call_id[call_id] = len(recovered) - 1
                sequence += 1
            elif role in {"tool", "function"} or message.get("type") == "function_call_output":
                call_id = message.get("tool_call_id") or message.get("call_id") or ""
                result = message.get("content", message.get("output"))
                record_index = by_call_id.get(call_id)
                if record_index is None:
                    recovered.append(
                        ToolExecutionRecord(
                            sequence=sequence,
                            execution_id=f"recovered-result-{message_index}-{sequence}",
                            tool_call_id=call_id,
                            tool_name=message.get("name") or "Unknown tool",
                            source="orchestrator",
                            actor="Orchestrator",
                            task_id=None,
                            started_at=None,
                            finished_at=None,
                            duration_ms=None,
                            status=self._recovered_status(message, result),
                            arguments={},
                            result=result,
                            error=str(result) if message.get("is_error") is True else "",
                            recovered=True,
                        )
                    )
                    sequence += 1
                    continue
                record = recovered[record_index]
                recovered[record_index] = replace(
                    record,
                    status=self._recovered_status(message, result),
                    result=result,
                    error=str(result) if message.get("is_error") is True else "",
                )

        with self._lock:
            self._records = {record.execution_id: record for record in recovered}
            self._started_monotonic.clear()
            self._next_sequence = sequence
        return len(recovered)

    def replace_with(self, other: "ToolExecutionHistory") -> None:
        with other._lock:
            records = dict(other._records)
            next_sequence = other._next_sequence
        with self._lock:
            self._records = records
            self._started_monotonic.clear()
            self._next_sequence = next_sequence

    @staticmethod
    def _tool_call_parts(tool_call: Any) -> tuple[str, str, Any]:
        if not isinstance(tool_call, dict):
            return "", "", ""
        function = tool_call.get("function")
        if isinstance(function, dict):
            return (
                str(tool_call.get("id") or ""),
                str(function.get("name") or tool_call.get("name") or ""),
                function.get("arguments", tool_call.get("arguments", "")),
            )
        return (
            str(tool_call.get("id") or tool_call.get("call_id") or ""),
            str(tool_call.get("name") or ""),
            tool_call.get("arguments", ""),
        )

    @staticmethod
    def _recovered_status(message: dict[str, Any], result: Any) -> str:
        text = "" if result is None else str(result)
        if text.startswith("[Previous ") and " result cleared" in text:
            return TOOL_STATUS_COMPACTED
        return tool_result_status(is_error=message.get("is_error") is True, output=result)

    @staticmethod
    def _search_text(record: ToolExecutionRecord) -> str:
        values = [
            record.tool_name,
            record.source,
            record.actor,
            record.task_id or "",
            record.status,
            format_tool_arguments(record.arguments),
            format_tool_value(record.result),
            record.error,
        ]
        return "\n".join(values).casefold()


TOOL_EXECUTION_HISTORY = ToolExecutionHistory()
