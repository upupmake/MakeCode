"""
测试 LLM 客户端流输出的脚本。
使用前请填写 api_key、base_url、model_id。
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncio
import os
import time
import json
from openai import OpenAI, AsyncOpenAI
from utils.llm_client import ChatAPIClient, AsyncChatAPIClient

# ============ 配置 ============
# 通过环境变量设置，或在项目根目录创建 test.env 文件（已 gitignore）写入：
#   MAKECODE_TEST_API_KEY=your_api_key
#   MAKECODE_TEST_BASE_URL=your_base_url
#   MAKECODE_TEST_MODEL=your_model_id
API_KEY = os.getenv("MAKECODE_TEST_API_KEY")
BASE_URL = os.getenv("MAKECODE_TEST_BASE_URL")
MODEL_ID = os.getenv("MAKECODE_TEST_MODEL")

if not API_KEY or not BASE_URL or not MODEL_ID:
    print("请设置环境变量 MAKECODE_TEST_API_KEY / MAKECODE_TEST_BASE_URL / MAKECODE_TEST_MODEL，"
          "或在项目根目录创建 test.env 文件。")
    exit(1)
# ================================


def _make_sync_client() -> ChatAPIClient:
    raw = OpenAI(
        base_url=BASE_URL,
        api_key=API_KEY,
        max_retries=3,
        default_headers={"User-Agent": "MakeCode Agent"},
    )
    return ChatAPIClient(raw, MODEL_ID)


def _make_async_client() -> AsyncChatAPIClient:
    raw = AsyncOpenAI(
        base_url=BASE_URL,
        api_key=API_KEY,
        max_retries=3,
        default_headers={"User-Agent": "MakeCode Agent"},
    )
    return AsyncChatAPIClient(raw, MODEL_ID)


MESSAGES = [
    {"role": "system", "content": "你是一个乐于助人的助手。"},
    {"role": "user", "content": "请用中文写一首关于春天的短诗，并解释你创作这首诗的思路。"},
]


# ── 同步流输出测试 ──

def test_sync_stream():
    print("=" * 60)
    print("【同步流输出测试】ChatAPIClient.generate_stream")
    print("=" * 60)

    client = _make_sync_client()
    start = time.time()

    text_parts = []
    reasoning_parts = []
    done_event = None
    chunk_count = 0

    for event in client.generate_stream(MESSAGES):
        chunk_count += 1
        t = event.get("type")

        if t == "text":
            text_parts.append(event["content"])
            print(event["content"], end="", flush=True)

        elif t == "reasoning":
            reasoning_parts.append(event["content"])
            # reasoning 不逐字打印，避免与正文混杂

        elif t == "tool_calls":
            print("\n[stream] tool_calls delta received")

        elif t == "done":
            done_event = event

    elapsed = time.time() - start
    print()  # 换行结束正文
    print("-" * 60)

    text, tool_calls, raw_message = done_event["content"]

    print(f"总耗时: {elapsed:.2f}s | chunk 数: {chunk_count}")
    print(f"完整文本长度: {len(text)} 字符")
    print(f"tool_calls 数: {len(tool_calls)}")
    if reasoning_parts:
        full_reasoning = "".join(reasoning_parts)
        print(f"reasoning 长度: {len(full_reasoning)} 字符")
        print(f"reasoning 摘要: {full_reasoning[:200]}...")

    # 验证：流式拼接的文本与 done 事件中的文本一致
    streamed_text = "".join(text_parts)
    if streamed_text == text:
        print("✓ 流式拼接文本与 done 事件文本一致")
    else:
        print(f"✗ 不一致！流式长度={len(streamed_text)}, done长度={len(text)}")

    # raw_message 结构检查
    print(f"raw_message keys: {list(raw_message.keys())}")
    if tool_calls:
        for tc in tool_calls:
            print(f"  tool_call: id={tc['id']}, name={tc['name']}, arguments长度={len(tc['arguments'])}")

    print()


# ── 异步流输出测试 ──

async def test_async_stream():
    print("=" * 60)
    print("【异步流输出测试】AsyncChatAPIClient.generate_stream")
    print("=" * 60)

    client = _make_async_client()
    start = time.time()

    text_parts = []
    reasoning_parts = []
    done_event = None
    chunk_count = 0

    async for event in client.generate_stream(MESSAGES):
        chunk_count += 1
        t = event.get("type")

        if t == "text":
            text_parts.append(event["content"])
            print(event["content"], end="", flush=True)

        elif t == "reasoning":
            reasoning_parts.append(event["content"])

        elif t == "tool_calls":
            print("\n[stream] tool_calls delta received")

        elif t == "done":
            done_event = event

    elapsed = time.time() - start
    print()
    print("-" * 60)

    text, tool_calls, raw_message = done_event["content"]

    print(f"总耗时: {elapsed:.2f}s | chunk 数: {chunk_count}")
    print(f"完整文本长度: {len(text)} 字符")
    print(f"tool_calls 数: {len(tool_calls)}")
    if reasoning_parts:
        full_reasoning = "".join(reasoning_parts)
        print(f"reasoning 长度: {len(full_reasoning)} 字符")

    streamed_text = "".join(text_parts)
    if streamed_text == text:
        print("✓ 流式拼接文本与 done 事件文本一致")
    else:
        print(f"✗ 不一致！流式长度={len(streamed_text)}, done长度={len(text)}")

    print(f"raw_message keys: {list(raw_message.keys())}")
    print()


# ── 非流式（同步）测试 ──

def test_sync_non_stream():
    print("=" * 60)
    print("【同步非流式测试】ChatAPIClient.generate")
    print("=" * 60)

    client = _make_sync_client()
    start = time.time()

    response = client.generate(MESSAGES)
    elapsed = time.time() - start

    text, tool_calls, raw_message = client.parse_response(response)

    print(f"总耗时: {elapsed:.2f}s")
    print(f"文本长度: {len(text)} 字符")
    print(f"tool_calls 数: {len(tool_calls)}")
    print(f"文本内容:\n{text[:500]}...")
    print()


# ── 异步非流式测试 ──

async def test_async_non_stream():
    print("=" * 60)
    print("【异步非流式测试】AsyncChatAPIClient.generate")
    print("=" * 60)

    client = _make_async_client()
    start = time.time()

    response = await client.generate(MESSAGES)
    elapsed = time.time() - start

    text, tool_calls, raw_message = client.parse_response(response)

    print(f"总耗时: {elapsed:.2f}s")
    print(f"文本长度: {len(text)} 字符")
    print(f"tool_calls 数: {len(tool_calls)}")
    print(f"文本内容:\n{text[:500]}...")
    print()


if __name__ == "__main__":
    test_sync_stream()
    asyncio.run(test_async_stream())
    test_sync_non_stream()
    asyncio.run(test_async_non_stream())
    print("全部测试完成。")
