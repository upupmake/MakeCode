import json
from pathlib import Path


def read_memory_records(memory_file: Path, *, include_deleted: bool = False) -> list[dict]:
    if not memory_file.exists():
        return []

    records = []
    with memory_file.open("r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            if include_deleted or record.get("status") == "active":
                records.append(record)
    return records


def sort_memory_records(records: list[dict]) -> list[dict]:
    return sorted(
        records,
        key=lambda record: record.get("updated_at") or record.get("created_at") or "",
    )
