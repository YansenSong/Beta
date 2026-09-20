from __future__ import annotations

import asyncio
import math
import os
import signal
from pathlib import Path

from pydantic import BaseModel, Field

from beta_agent.tools import Tool, ToolExecutionContext
from beta_agent.types import ToolResult

BASH_MAX_BYTES = 64 * 1024


class BashArgs(BaseModel):
    command: str = Field(description="POSIX-like shell command to execute in the workspace")
    timeout: float | None = Field(default=None, gt=0, description="Optional finite timeout in seconds")


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return

    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    else:
        process.terminate()

    try:
        await asyncio.wait_for(process.wait(), timeout=1.0)
        return
    except (asyncio.TimeoutError, ProcessLookupError):
        pass

    if process.returncode is None:
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            process.kill()
        await process.wait()


def _truncate_tail(data: bytes, max_bytes: int = BASH_MAX_BYTES) -> tuple[str, bool]:
    if len(data) <= max_bytes:
        return data.decode("utf-8", errors="replace"), False

    marker = "[Output truncated; showing the tail of the command output.]\n"
    marker_bytes = marker.encode("utf-8")
    tail_budget = max(0, max_bytes - len(marker_bytes))
    tail = data[-tail_budget:].decode("utf-8", errors="ignore") if tail_budget else ""
    return marker + tail, True


def create_bash_tool(cwd: str | Path) -> Tool[BashArgs]:
    workspace = Path(cwd).expanduser().resolve()

    async def bash(args: BashArgs, ctx: ToolExecutionContext) -> ToolResult:
        # Agent abort 会把这次 cooperative check 与真实的 task cancellation 结合起来，
        # 因此 cancellation 发生后绝不会再启动 command。
        ctx.cancellation.throw_if_cancelled()
        if args.timeout is not None and (not math.isfinite(args.timeout) or args.timeout <= 0):
            raise ValueError("timeout must be a finite positive number")

        try:
            process = await asyncio.create_subprocess_shell(
                args.command,
                cwd=str(workspace),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=os.name == "posix",
            )
        except OSError as exc:
            raise OSError(f"Unable to start command {args.command!r}: {exc}") from exc

        communicate_task = asyncio.create_task(process.communicate())
        try:
            if args.timeout is None:
                stdout, _ = await asyncio.shield(communicate_task)
            else:
                stdout, _ = await asyncio.wait_for(asyncio.shield(communicate_task), args.timeout)
        except asyncio.TimeoutError as exc:
            await _terminate_process(process)
            stdout, _ = await communicate_task
            output, truncated = _truncate_tail(stdout or b"")
            suffix = f"Command timed out after {args.timeout} seconds (exit code {process.returncode})."
            if output:
                suffix += f"\nOutput:\n{output}"
            raise TimeoutError(suffix) from exc
        except asyncio.CancelledError:
            await _terminate_process(process)
            if not communicate_task.done():
                try:
                    await communicate_task
                except asyncio.CancelledError:
                    pass
            raise

        output, truncated = _truncate_tail(stdout or b"")
        return_code = process.returncode
        details = {"exit_code": return_code, "truncated": truncated, "cwd": str(workspace)}
        if return_code != 0:
            raise RuntimeError(f"Command exited with code {return_code}:\n{output}")
        return ToolResult(content=output, details=details)

    return Tool(
        name="bash",
        description="Run a POSIX-like shell command in the workspace and return combined stdout/stderr.",
        args_model=BashArgs,
        handler=bash,
        execution_mode="sequential",
        replay_policy="unsafe",
    )
