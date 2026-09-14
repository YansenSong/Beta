"""Interactive CLI harness for the product-layer Coding Agent."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from beta_agent.adapters import OpenAICompatibleAdapter
from coding_agent import CodingAgentOptions, create_coding_agent
from coding_agent.extensions import permission_gate_extension, plan_mode_extension, subagent_extension


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Beta's Coding Agent in a workspace")
    parser.add_argument("--cwd", type=Path, default=Path.cwd(), help="Workspace directory (default: current directory)")
    parser.add_argument("--session", type=Path, default=None, help="Optional JSONL session file")
    parser.add_argument(
        "--no-permission-gate",
        action="store_true",
        help="Do not load the demonstration dangerous-command policy",
    )
    return parser.parse_args()


def _short_args(args: dict[str, Any] | None) -> str:
    if not args:
        return "{}"
    encoded = json.dumps(args, ensure_ascii=False, separators=(",", ":"))
    return encoded if len(encoded) <= 160 else encoded[:157] + "..."


async def _chat(arguments: argparse.Namespace) -> None:
    project_root = Path(__file__).resolve().parents[1]
    load_dotenv(project_root / ".env")

    api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("未找到 OPENAI_API_KEY 或 DEEPSEEK_API_KEY，请在项目根目录的 .env 中配置。")
    model_name = os.environ.get("OPENAI_MODEL") or os.environ.get("DEEPSEEK_MODEL") or "gpt-4.1-mini"
    base_url = os.environ.get("OPENAI_BASE_URL") or (
        "https://api.deepseek.com" if os.environ.get("DEEPSEEK_API_KEY") else "https://api.openai.com/v1"
    )

    def make_model() -> OpenAICompatibleAdapter:
        return OpenAICompatibleAdapter(model=model_name, api_key=api_key, base_url=base_url)

    extensions = [plan_mode_extension, subagent_extension]
    if not arguments.no_permission_gate:
        extensions.insert(0, permission_gate_extension)

    runtime = await create_coding_agent(
        CodingAgentOptions(
            cwd=arguments.cwd,
            model=make_model(),
            model_name=model_name,
            extensions=extensions,
            child_model_factory=make_model,
            session_file=arguments.session,
        )
    )

    print(f"Beta Coding Agent 已启动，模型：{model_name}")
    print(f"工作区：{runtime.cwd}")
    print("输入 /plan 切换只读 Plan Mode；输入 exit / quit / q 退出。\n")
    try:
        while True:
            try:
                user_input = input("你 > ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n已退出。")
                return
            if not user_input:
                continue
            if user_input.lower() in {"exit", "quit", "q"}:
                print("已退出。")
                return
            if user_input.startswith("/"):
                try:
                    await runtime.run_command(user_input)
                    print(f"[command] {user_input} 已执行。\n")
                except Exception as exc:
                    print(f"[command error] {exc}\n")
                continue

            print("AI > ", end="", flush=True)
            printed_length = 0
            stream = runtime.stream(user_input)
            try:
                async for event in stream:
                    if (
                        event.type == "message_update"
                        and event.message is not None
                        and event.message.role == "assistant"
                    ):
                        text = event.message.content
                        if len(text) >= printed_length:
                            print(text[printed_length:], end="", flush=True)
                            printed_length = len(text)
                    elif event.type == "tool_execution_start":
                        print(f"\n[tool start] {event.tool_name} {_short_args(event.args)}")
                    elif event.type == "tool_execution_end" and event.error:
                        print(f"[tool error] {event.tool_name}: {event.error}")
                messages = await stream.result()
                assistants = [message for message in messages if message.role == "assistant"]
                if assistants and len(assistants[-1].content) > printed_length:
                    print(assistants[-1].content[printed_length:], end="", flush=True)
                print("\n")
            except Exception as exc:
                print(f"\n[请求失败] {exc}\n")
            finally:
                await runtime.save_session()
    finally:
        runtime.close()


def main() -> None:
    asyncio.run(_chat(_arguments()))


if __name__ == "__main__":
    main()
