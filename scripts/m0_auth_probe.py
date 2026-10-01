#!/usr/bin/env python3
"""M0 auth probe: does the subscription login survive a scrubbed subprocess?"""
import asyncio, os, shutil, sys

if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
    print("auth: CLAUDE_CODE_OAUTH_TOKEN present (fallback mode)")

removed = [k for k in os.environ if k != "CLAUDE_CODE_OAUTH_TOKEN" and (
    k.startswith("CLAUDE_CODE_") or k in ("CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "ANTHROPIC_API_KEY"))]
for k in removed:
    del os.environ[k]
print(f"scrubbed: {removed}")

cli_path = os.environ.get("LICTOR_CLAUDE") or shutil.which("claude")
if not cli_path:
    print("claude: not found (set $LICTOR_CLAUDE or put claude on PATH)")
    sys.exit(1)

from claude_agent_sdk import (  # noqa: E402
    AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, ResultMessage, SystemMessage, TextBlock,
)


async def main() -> int:
    opts = ClaudeAgentOptions(cli_path=cli_path, setting_sources=[], tools=[], allowed_tools=[],
                               max_turns=1, permission_mode="dontAsk")
    got_pong = False
    try:
        async with ClaudeSDKClient(options=opts) as client:
            await client.query("Reply with the single word: pong")
            async for msg in client.receive_response():
                if isinstance(msg, SystemMessage) and msg.subtype == "init":
                    print(f"init: model={msg.data.get('model')} session_id={msg.data.get('session_id')}")
                elif isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            print(f"text: {block.text}")
                elif isinstance(msg, ResultMessage):
                    for label in ("subtype", "is_error", "num_turns", "session_id", "total_cost_usd", "usage", "result"):
                        print(f"result {label}: {getattr(msg, label)}")
                    got_pong = (not msg.is_error) and bool(msg.result) and "pong" in msg.result.lower()
    except Exception as exc:  # noqa: BLE001
        print(f"exception: {exc!r}")
        if any(t in str(exc).lower() for t in ("auth", "login", "unauthorized", "credential", "token")):
            return 2
        raise
    return 0 if got_pong else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
