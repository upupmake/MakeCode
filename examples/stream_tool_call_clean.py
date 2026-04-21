"""
流式工具调用 - 简洁版
流过程中只显示文本，流结束后再显示工具调用
"""

import json
from openai import OpenAI
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown

console = Console()


def stream_and_wait(
    client: OpenAI,
    model: str,
    messages: list,
    tools: list,
) -> tuple[str, list]:
    """
    流式输出，等结束后返回完整响应
    
    特点：
    - 流过程中：只渲染文本内容
    - 流结束后：返回 tool_calls（由调用方决定何时打印）
    """
    stream = client.chat.completions.create(
        model=model,
        messages=messages,
        tools=tools,
        stream=True
    )
    
    # 累积变量
    full_text = ""
    tool_calls: dict[str, dict] = {}
    current_id = None
    
    with Live(console=console, refresh_per_second=15) as live:
        for chunk in stream:
            if not chunk.choices:
                continue
            
            choice = chunk.choices[0]
            
            # 1. 文本内容 - 实时渲染
            if choice.delta.content:
                full_text += choice.delta.content
                live.update(Markdown(full_text))
            
            # 2. 工具调用 - 只累积，不打印
            if choice.delta.tool_calls:
                for tc in chunk.choices[0].delta.tool_calls:
                    if tc.id:
                        current_id = tc.id
                        if current_id not in tool_calls:
                            tool_calls[current_id] = {"name": "", "args": ""}
                    
                    if tc.function:
                        if tc.function.name:
                            tool_calls[current_id]["name"] = tc.function.name
                        if tc.function.arguments:
                            tool_calls[current_id]["args"] += tc.function.arguments
    
    # 流结束，返回完整数据
    result_calls = [
        {"id": tc_id, "name": tc["name"], "arguments": tc["args"]}
        for tc_id, tc in tool_calls.items()
    ]
    
    return full_text, result_calls


def execute_tools(tool_calls: list, handlers: dict) -> list:
    """
    执行工具调用（流结束后调用）
    返回工具结果列表
    """
    results = []
    
    for tc in tool_calls:
        console.print(f"\n[cyan]🛠️ 调用工具: {tc['name']}[/cyan]")
        
        try:
            args = json.loads(tc['arguments'])
            console.print(f"[dim]参数: {args}[/dim]")
            
            handler = handlers.get(tc['name'])
            if handler:
                output = handler(**args)
            else:
                output = f"Unknown tool: {tc['name']}"
            
            console.print(f"[green]结果: {output}[/green]")
            results.append({
                "tool_call_id": tc['id'],
                "name": tc['name'],
                "content": output if isinstance(output, str) else json.dumps(output)
            })
        except Exception as e:
            error = f"Error: {e}"
            console.print(f"[red]{error}[/red]")
            results.append({
                "tool_call_id": tc['id'],
                "name": tc['name'],
                "content": error
            })
    
    return results


# ========== 使用示例 ==========
if __name__ == "__main__":
    client = OpenAI(api_key="sk-cp-2BXaDga6mUQs0U24KGzDtJRWAC1NSNT7hCquSCM1q297pAgaailxCYLvuWjoX1wwl2ho2olBGUvXJKFgQyiKZbklGAM6YrCeUKpZxjqgjzFaeX0rYFAhYbs", base_url="https://api.minimaxi.com/v1")
    
    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "获取城市天气",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "city": {"type": "string", "description": "城市名"}
                    },
                    "required": ["city"]
                }
            }
        }
    ]
    
    messages = [{"role": "user", "content": "北京今天天气怎么样？"}]
    
    # 工具处理器
    handlers = {
        "get_weather": lambda city: {"city": city, "weather": "晴天", "temp": 25}
    }
    
    # 1. 流式获取响应（过程中只显示文本）
    text, tool_calls = stream_and_wait(client, "MiniMax-M2.7-highspeed", messages, tools)
    
    console.print()  # 空行
    
    # 2. 流结束后，判断是否有工具调用
    if tool_calls:
        console.print(f"[yellow]📋 检测到 {len(tool_calls)} 个工具调用[/yellow]")
        
        # 执行工具
        tool_results = execute_tools(tool_calls, handlers)
        
        # 3. 添加工具结果到消息
        messages.append({
            "role": "assistant",
            "content": text,
            "tool_calls": [
                {"id": tc["id"], "type": "function", 
                 "function": {"name": tc["name"], "arguments": tc["arguments"]}}
                for tc in tool_calls
            ]
        })
        for res in tool_results:
            messages.append({
                "role": "tool",
                "tool_call_id": res["tool_call_id"],
                "name": res["name"],
                "content": res["content"]
            })
        
        # 4. 继续对话获取最终回复
        console.print("\n[dim]发送工具结果给模型...[/dim]")
        text2, _ = stream_and_wait(client, "MiniMax-M2.7-highspeed", messages, [])
        console.print(f"\n[bold green]最终回复: {text2}[/bold green]")
    else:
        console.print(f"[green]回复: {text}[/green]")
