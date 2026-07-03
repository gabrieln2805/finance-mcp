"""
test_client.py
A minimal MCP client used to sanity-check server.py without needing
Node.js / the Inspector. It spawns server.py as a subprocess (exactly
like Claude Desktop would), does the protocol handshake, lists the
available tools, then calls a few of them and prints the results.

Run:
    python test_client.py
"""

import asyncio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    # Tells the client HOW to start the server: run "python server.py"
    # as a child process and talk to it over its stdin/stdout.
    params = StdioServerParameters(command="python", args=["server.py"])

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            # Protocol handshake — negotiates capabilities with the server.
            await session.initialize()

            # Ask the server what tools it exposes.
            tools = await session.list_tools()
            print("Available tools:", [t.name for t in tools.tools])
            print("-" * 60)

            # Call list_accounts with no arguments.
            result = await session.call_tool("list_accounts", {})
            print("list_accounts (first 300 chars):")
            print(result.content[0].text[:300])
            print("-" * 60)

            # Call monthly_summary for March 2026.
            result = await session.call_tool(
                "monthly_summary", {"year": 2026, "month": 3}
            )
            print("monthly_summary(2026, 3):")
            print(result.content[0].text)
            print("-" * 60)

            # Call spending_by_category for Q1 2026.
            result = await session.call_tool(
                "spending_by_category",
                {"start_date": "2026-01-01", "end_date": "2026-03-31"},
            )
            print("spending_by_category(Q1 2026), first 400 chars:")
            print(result.content[0].text[:400])


if __name__ == "__main__":
    asyncio.run(main())
