"""Private bootstrap over stdin; dynamic bound loopback socket; EOF owns shutdown."""

import asyncio
import json
import msvcrt
import os
import socket
import sys
import threading
from pathlib import Path

ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
sys.path[:0] = [str(ROOT), str(ROOT / "protocol")]

import uvicorn  # noqa: E402

from service.app import create_app  # noqa: E402


async def run(bootstrap):
    user_data = Path(bootstrap["user_data"]).resolve()
    user_data.mkdir(parents=True, exist_ok=True)
    # Second direct backend launch with the same profile cannot become another owner.
    lock = (user_data / "service.lock").open("a+b")
    lock.seek(0)
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        lock.close()
        raise RuntimeError("该用户目录已有本地服务") from None
    if lock.seek(0, 2) == 0:
        lock.write(b"0")
        lock.flush()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(32)
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(ROOT, bootstrap["token"], port, user_data),
            host="127.0.0.1",
            port=port,
            workers=1,
            log_level="warning",
            access_log=False,
            loop="asyncio",
            ws="none",
            timeout_graceful_shutdown=2,
        )
    )
    loop = asyncio.get_running_loop()

    def shutdown_on_owner_end():
        # No bearer token in argv, files, renderer, or readiness records.
        for line in sys.stdin:
            if line.strip() == "shutdown":
                break
        loop.call_soon_threadsafe(setattr, server, "should_exit", True)
        deadline = threading.Timer(5, lambda: os._exit(0))
        deadline.daemon = True
        deadline.start()

    threading.Thread(target=shutdown_on_owner_end, daemon=True).start()
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        while not server.started and not task.done():
            await asyncio.sleep(0.02)
        if not server.started:
            await task
            raise RuntimeError("后台未就绪")
        print(
            json.dumps(
                {
                    "ready": True,
                    "api_version": "1",
                    "simulation": True,
                    "read_only": True,
                    "port": port,
                    "pid": os.getpid(),
                    "frozen": bool(getattr(sys, "frozen", False)),
                }
            ),
            flush=True,
        )
        await task
    finally:
        listener.close()
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        lock.close()


if __name__ == "__main__":
    line = sys.stdin.readline(8192)
    bootstrap = json.loads(line)
    if not isinstance(bootstrap.get("token"), str) or len(bootstrap["token"]) != 64:
        raise ValueError("Invalid private bootstrap")
    asyncio.run(run(bootstrap))
