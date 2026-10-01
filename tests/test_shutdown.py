import asyncio
import contextlib
import signal
import sys

import pytest


@pytest.mark.skipif(sys.platform == "win32", reason="Unix SIGTERM lifecycle")
async def test_sigterm_runs_cleanup_and_exits_successfully():
    script = """
import asyncio
import waifu_bot.main as entry
async def fake(check=False):
    print("READY", flush=True)
    try:
        await asyncio.sleep(60)
    finally:
        print("CLEANED", flush=True)
entry.run = fake
entry.main()
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        line = await asyncio.wait_for(process.stdout.readline(), 10)
        assert line == b"READY\n"
        process.send_signal(signal.SIGTERM)
        stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
        assert process.returncode == 0, stderr.decode()
        assert b"CLEANED" in stdout
    finally:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
