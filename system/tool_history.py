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
            item_lines = _format_json_lines(item, indent_level + 1)
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
        encoded_lines = [
            json.dumps(line, ensure_ascii=False)[1:-1]
            for line in value.split("\n")
        ]
        lines = [f'{indent}"']
        lines.extend(f"{indent}{line}" for line in encoded_lines[:-1])
        lines.append(f'{indent}{encoded_lines[-1]}"')
        return lines

    return [f"{indent}{json.dumps(value, ensure_ascii=False, default=str)}"]


def _format_json(value: Any) -> str:
    normalized = json.loads(json.dumps(value, ensure_ascii=False, default=str))
    if not _contains_multiline_string(normalized):
        return json.dumps(normalized, ensure_ascii=False, indent=2, default=str)
    return "\n".join(_format_json_lines(normalized))


def format_tool_value(value: Any) -> str:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return value
        return _format_json(parsed)
    return _format_json(value)


def format_tool_arguments(value: Any) -> str:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            pass
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
