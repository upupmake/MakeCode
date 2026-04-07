import json

from tools.todo import TODO_TOOLS

print(json.dumps(TODO_TOOLS[0], indent=2, ensure_ascii=False))
print("===" * 20)

from fastmcp import Client

with open(".makecode/mcp_config.json", "r", encoding="utf-8") as f:
    config_dict = json.load(f)
client = Client(config_dict)


async def main():
    async with client:
        tools = await client.list_tools()
        for tool in tools:
            print(tool.model_dump_json(indent=2, ensure_ascii=False))
            print("===" * 20)


if __name__ == '__main__':
    import asyncio

    asyncio.run(main())
