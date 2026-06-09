import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from openai import AsyncOpenAI, pydantic_function_tool
from pydantic import BaseModel, Field

from utils.llm_client import AsyncChatAPIClient


class EchoArgs(BaseModel):
    text: str = Field(..., description="Text to echo back through the test tool.")


def _read_config(args: argparse.Namespace) -> tuple[str, str, str]:
    base_url = args.base_url or os.getenv("MAKECODE_TEST_BASE_URL")
    api_key = args.api_key or os.getenv("MAKECODE_TEST_API_KEY")
    model = args.model or os.getenv("MAKECODE_TEST_MODEL")
    missing = [
        name
        for name, value in (
            ("base_url", base_url),
            ("api_key", api_key),
            ("model", model),
        )
        if not value
    ]
    if missing:
        raise SystemExit(
            "Missing required config: "
            + ", ".join(missing)
            + ". Provide CLI args or MAKECODE_TEST_BASE_URL / MAKECODE_TEST_API_KEY / MAKECODE_TEST_MODEL."
        )
    return base_url, api_key, model


def _describe_done(label: str, done_content: Any) -> bool:
    print(f"\n[{label}] done content type: {type(done_content).__name__}")
    if not isinstance(done_content, tuple) or len(done_content) != 3:
        print(f"[{label}] FAIL: expected a 3-tuple, got {done_content!r}")
        return False

    text_content, tool_calls, raw_message = done_content
    print(f"[{label}] text_content type: {type(text_content).__name__}, length: {len(text_content or '')}")
    print(f"[{label}] tool_calls type: {type(tool_calls).__name__}, count: {len(tool_calls) if isinstance(tool_calls, list) else 'n/a'}")
    print(f"[{label}] raw_message type: {type(raw_message).__name__}")

    ok = True
    if not isinstance(text_content, str):
        print(f"[{label}] FAIL: text_content is not str")
        ok = False
    if not isinstance(tool_calls, list):
        print(f"[{label}] FAIL: tool_calls is not list")
        ok = False
    if not isinstance(raw_message, dict):
        print(f"[{label}] FAIL: raw_message is not dict")
        ok = False

    if isinstance(raw_message, dict):
        print(f"[{label}] raw_message keys: {sorted(raw_message.keys())}")
    if isinstance(tool_calls, list):
        for idx, call in enumerate(tool_calls):
            print(f"[{label}] tool_call[{idx}] keys: {sorted(call.keys()) if isinstance(call, dict) else type(call).__name__}")
            if isinstance(call, dict):
                print(f"[{label}] tool_call[{idx}] name: {call.get('name')!r}, arguments type: {type(call.get('arguments')).__name__}")

    print(f"[{label}] {'PASS' if ok else 'FAIL'}")
    return ok


async def _collect_done(client: AsyncChatAPIClient, label: str, messages: list[dict], tools: list | None = None) -> Any:
    done_content = None
    text_chunks = 0
    reasoning_chunks = 0
    async for event in client.generate_stream(messages=messages, tools=tools):
        event_type = event.get("type")
        if event_type == "text":
            text_chunks += 1
        elif event_type == "reasoning":
            reasoning_chunks += 1
        elif event_type == "done":
            done_content = event.get("content")
            break
    print(f"[{label}] streamed text chunks: {text_chunks}, reasoning chunks: {reasoning_chunks}")
    if done_content is None:
        raise RuntimeError(f"{label}: stream ended without done event")
    return done_content


async def main() -> int:
    parser = argparse.ArgumentParser(description="Validate AsyncChatAPIClient.generate_stream done tuple shape.")
    parser.add_argument("--base-url")
    parser.add_argument("--api-key")
    parser.add_argument("--model")
    parser.add_argument("--skip-tool-call", action="store_true")
    args = parser.parse_args()

    base_url, api_key, model = _read_config(args)
    raw_client = AsyncOpenAI(
        base_url=base_url,
        api_key=api_key,
        max_retries=1,
        default_headers={"User-Agent": "MakeCode Agent Stream Tuple Test"},
    )
    client = AsyncChatAPIClient(raw_client, model)

    try:
        plain_done = await _collect_done(
            client,
            "plain",
            [
                {"role": "system", "content": "You are a concise test assistant."},
                {"role": "user", "content": "Reply with exactly: stream tuple ok"},
            ],
        )
        success = _describe_done("plain", plain_done)

        if not args.skip_tool_call:
            formatted_tools = client.format_tools([pydantic_function_tool(EchoArgs)])
            tool_done = await _collect_done(
                client,
                "tool",
                [
                    {"role": "system", "content": "You are testing tool calling. When asked, call the provided EchoArgs tool."},
                    {"role": "user", "content": "Call the EchoArgs tool with text set to 'stream tuple tool ok'. Do not answer directly."},
                ],
                tools=formatted_tools,
            )
            success = _describe_done("tool", tool_done) and success

        return 0 if success else 1
    finally:
        await raw_client.close()


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        raise SystemExit(130)
