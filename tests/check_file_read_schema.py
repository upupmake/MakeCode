import copy
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from openai import pydantic_function_tool
from pydantic import BaseModel, Field

from utils.common import FileRead
from utils.llm_client import AsyncChatAPIClient


class GeoPoint(BaseModel):
    lat: float = Field(..., description="Latitude.")
    lng: float = Field(..., description="Longitude.")


class Address(BaseModel):
    city: str
    street: str
    point: GeoPoint


class Contact(BaseModel):
    name: str
    phones: list[str]
    address: Address | None = None


class Department(BaseModel):
    name: str
    owner: Contact
    members: list[Contact]
    offices: dict[str, Address]


class ComplexToolArgs(BaseModel):
    request_id: str
    primary_contact: Contact
    departments: list[Department]
    metadata: dict[str, str] = Field(default_factory=dict)


def _inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    schema = copy.deepcopy(schema)
    root = copy.deepcopy(schema)

    def resolve_ref(ref: str) -> Any:
        if not ref.startswith("#/"):
            raise ValueError(f"Only local refs are supported: {ref}")

        current: Any = root
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            current = current[part]
        return walk(copy.deepcopy(current))

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                resolved = resolve_ref(node["$ref"])
                siblings = {k: walk(v) for k, v in node.items() if k != "$ref"}
                if isinstance(resolved, dict):
                    resolved.update(siblings)
                return resolved
            return {
                k: walk(v)
                for k, v in node.items()
                if k not in {"$defs", "definitions"}
            }
        if isinstance(node, list):
            return [walk(item) for item in node]
        return node

    return walk(schema)


def _inline_refs_with_jsonref(schema: dict[str, Any]) -> dict[str, Any] | None:
    if importlib.util.find_spec("jsonref") is None:
        return None

    import jsonref

    return jsonref.replace_refs(schema, proxies=False)


def _contains_key(node: Any, key: str) -> bool:
    if isinstance(node, dict):
        return key in node or any(_contains_key(value, key) for value in node.values())
    if isinstance(node, list):
        return any(_contains_key(item, key) for item in node)
    return False


def _print_result(label: str, schema: dict[str, Any]) -> None:
    flat_schema = _inline_refs(schema)
    jsonref_schema = _inline_refs_with_jsonref(schema)

    print(f"{label} contains $ref:", _contains_key(schema, "$ref"))
    print(f"{label} flattened by custom function contains $ref:", _contains_key(flat_schema, "$ref"))
    assert _contains_key(schema, "$ref")
    assert not _contains_key(flat_schema, "$ref")

    if jsonref_schema is None:
        print(f"{label} flattened by jsonref: skipped, jsonref is not installed")
    else:
        print(f"{label} flattened by jsonref contains $ref:", _contains_key(jsonref_schema, "$ref"))
        assert not _contains_key(jsonref_schema, "$ref")


def main() -> None:
    model_schema = FileRead.model_json_schema()
    tool = pydantic_function_tool(FileRead)
    parameters = tool["function"]["parameters"]
    complex_schema = ComplexToolArgs.model_json_schema()
    complex_tool = pydantic_function_tool(ComplexToolArgs)
    complex_parameters = complex_tool["function"]["parameters"]
    formatted_complex_parameters = AsyncChatAPIClient(None, "test").format_tools([complex_tool])[0]["function"]["parameters"]

    _print_result("FileRead.model_json_schema()", model_schema)
    _print_result("pydantic_function_tool(FileRead) parameters", parameters)
    _print_result("ComplexToolArgs.model_json_schema()", complex_schema)
    _print_result("pydantic_function_tool(ComplexToolArgs) parameters", complex_parameters)

    flat_parameters = _inline_refs(parameters)
    jsonref_parameters = _inline_refs_with_jsonref(parameters)
    flat_complex_schema = _inline_refs(complex_schema)

    print("\n=== FileRead full parameters after custom flatten ===")
    print(json.dumps(flat_parameters, ensure_ascii=False, indent=2))
    if jsonref_parameters is None:
        print("\n=== FileRead full parameters after jsonref flatten ===")
        print("skipped, jsonref is not installed")
    else:
        print("\n=== FileRead full parameters after jsonref flatten ===")
        print(json.dumps(jsonref_parameters, ensure_ascii=False, indent=2))

    print("\n=== ComplexToolArgs parameters after AsyncChatAPIClient.format_tools ===")
    print(json.dumps(formatted_complex_parameters, ensure_ascii=False, indent=2))

    assert not _contains_key(formatted_complex_parameters, "$ref")
    assert not _contains_key(formatted_complex_parameters, "$defs")
    assert not _contains_key(formatted_complex_parameters, "definitions")
    assert formatted_complex_parameters["properties"]["departments"]["type"] == "array"
    assert formatted_complex_parameters["properties"]["departments"]["items"]["properties"]["members"]["items"]["properties"]["phones"]["items"]["type"] == "string"
    assert formatted_complex_parameters["properties"]["departments"]["items"]["properties"]["offices"]["additionalProperties"]["properties"]["point"]["properties"]["lng"]["type"] == "number"

    assert flat_parameters["properties"]["regions"]["type"] == "array"
    assert flat_parameters["properties"]["regions"]["minItems"] == 1
    assert flat_parameters["properties"]["regions"]["items"]["properties"]["start"]["type"] == "integer"
    assert flat_parameters["properties"]["regions"]["items"]["properties"]["end"]["type"] == "integer"
    assert flat_complex_schema["properties"]["primary_contact"]["properties"]["address"]["anyOf"][0]["properties"]["point"]["properties"]["lat"]["type"] == "number"
    assert flat_complex_schema["properties"]["departments"]["items"]["properties"]["members"]["items"]["properties"]["phones"]["items"]["type"] == "string"
    assert flat_complex_schema["properties"]["departments"]["items"]["properties"]["offices"]["additionalProperties"]["properties"]["city"]["type"] == "string"


if __name__ == "__main__":
    main()
