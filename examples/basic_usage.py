"""
basic_usage.py — Quickstart example for opencode-free-tier-py.

Run:
    pip install httpx
    python examples/basic_usage.py
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from opencode_free import OpenCodeClient


async def simple_stream() -> None:
    """Stream a response from GLM-5.3-Flash (Free Tier)."""
    client = OpenCodeClient()

    print("─" * 60)
    print("Model: glm-5.3-flash-free")
    print("─" * 60)

    async for event in client.stream(
        model="glm-5.3-flash-free",
        messages=[{"role": "user", "content": "Explain quantum entanglement in one paragraph."}],
        reasoning_effort="high",
    ):
        if event.kind == "reasoning":
            # Thinking tokens (yellow)
            print(f"\033[33m{event.text}\033[0m", end="", flush=True)
        elif event.kind == "content":
            print(event.text, end="", flush=True)
        elif event.kind == "usage":
            print(f"\n\n[tokens] {event.usage}")
        elif event.kind == "error":
            print(f"\n[error] {event.error}")

    print("\n" + "─" * 60)


async def multi_model_flip() -> None:
    """Demonstrate automatic model flip on 429 + per-user project isolation."""
    # project_seed isolates quota per logical user/tenant
    client = OpenCodeClient(
        project_seed="demo-user-42",
        max_retries=4,
        burst_sleep=3.0,
        allow_model_flip=True,  # GLM ↔ DeepSeek auto-switch on 429
    )

    result = await client.complete(
        model="deepseek-v4.1-flash-free",
        messages=[{"role": "user", "content": "What is 2+2?"}],
        reasoning_effort="low",
    )

    print(f"Success : {result['success']}")
    print(f"Content : {result['content']}")
    print(f"Usage   : {result['usage']}")


async def main() -> None:
    await simple_stream()
    await multi_model_flip()


if __name__ == "__main__":
    asyncio.run(main())
