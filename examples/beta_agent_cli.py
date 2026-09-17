"""DeepSeek CLI 多轮对话测试。

使用方式：
    1. 在项目根目录创建 .env：

       DEEPSEEK_API_KEY=你的_API_Key
       DEEPSEEK_MODEL=deepseek-v4-flash

    2. 安装项目依赖：
       pip install -e .

    3. 启动：
       python examples/deepseek_cli.py

这个文件直接通过本地 Python 方式调用 Beta Agent，不经过 HTTP 服务或额外客户端层。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv

from beta_agent import Agent
from beta_agent.adapters import OpenAICompatibleAdapter


async def chat() -> None:
    # 显式读取项目根目录的 .env，避免从不同工作目录启动时找不到配置。
    project_root = Path(__file__).resolve().parents[1]
    load_dotenv(project_root / ".env")

    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError(
            "未找到 DEEPSEEK_API_KEY，请在项目根目录的 .env 中配置：\n"
            "DEEPSEEK_API_KEY=你的_API_Key"
        )

    model_name = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")

    model = OpenAICompatibleAdapter(
        model=model_name,
        api_key=api_key,
        base_url="https://api.deepseek.com",
    )

    # Agent 实例会持续保存历史消息，因此多次 agent.stream(...) 就能形成多轮对话。
    agent = Agent(
        model=model,
        system_prompt=(
            "你是一个中文 AI 助手。回答清晰、准确、简洁；"
            "当用户要求详细解释时，再展开说明。"
        ),
    )

    print(f"Beta Agent CLI 已启动，模型：{model_name}")
    print("输入 exit / quit / q 退出。\n")

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

        print("AI > ", end="", flush=True)

        # message_update 传的是“当前完整 partial message”，不是单独 delta，
        # 所以这里记录已经打印的长度，只输出新增部分，避免重复打印。
        printed_length = 0
        stream = agent.stream(user_input)

        try:
            async for event in stream:
                if (
                    event.type == "message_update"
                    and event.message is not None
                    and event.message.role == "assistant"
                ):
                    text = event.message.text
                    if len(text) >= printed_length:
                        print(text[printed_length:], end="", flush=True)
                        printed_length = len(text)

            messages = await stream.result()

            # 某些 Provider 可能只在 message_end 时给出最终文本。
            # 如果流式 update 没打印完整，就在这里补齐。
            assistants = [message for message in messages if message.role == "assistant"]
            if assistants:
                final_text = assistants[-1].text
                if len(final_text) > printed_length:
                    print(final_text[printed_length:], end="", flush=True)

            print("\n")
        except Exception as exc:
            print(f"\n[请求失败] {exc}\n")


if __name__ == "__main__":
    asyncio.run(chat())
