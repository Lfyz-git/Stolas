"""Bounded command capture and stderr activity for long installer operations."""
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time

from tools.terminal import Terminal


def passthrough(args, root, env, timeout):
    """Preserve machine streams while bounding Compose and its local children."""
    # Keep the controlling terminal/session so sudo's TTY credential cache is
    # usable; isolate only the process group for bounded cleanup (Python 3.10+).
    process = subprocess.Popen(args, cwd=root, env=env, stdin=subprocess.DEVNULL,
                               preexec_fn=os.setpgrp if os.name == "posix" else None)
    try:
        return process.wait(timeout=timeout)
    finally:
        stop(process)


def stop(process):
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except ProcessLookupError:
        process.wait()
        return
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        process.wait()


def run(args, root, env, title, timeout, machine=False, stream=None):
    terminal = Terminal(stream or sys.stderr)
    interactive = terminal.stream.isatty() and sys.stdout.isatty() and not os.getenv("CI") and os.getenv("TERM") != "dumb"
    started = time.monotonic()
    terminal.line(title + "…")
    terminal.stream.flush()
    state = "ошибка"
    process = None
    phase = ""
    # Child output goes to disk; only bounded excerpts are ever read into memory.
    with tempfile.TemporaryDirectory(prefix="stolas-progress-") as directory, \
            open(Path(directory) / "stdout", "w+b") as output, \
            open(Path(directory) / "stderr", "w+b") as errors, \
            open(Path(directory) / "stderr", "rb") as reader:
        try:
            process = subprocess.Popen(args, cwd=root, env=env, stdin=subprocess.DEVNULL,
                                       stdout=output, stderr=errors,
                                       preexec_fn=os.setpgrp if os.name == "posix" else None)
            while True:
                elapsed = time.monotonic() - started
                if elapsed >= timeout:
                    raise subprocess.TimeoutExpired(args, timeout)
                try:
                    process.wait(timeout=min(.25, timeout - elapsed))
                    break
                except subprocess.TimeoutExpired:
                    if interactive:
                        # Show only known Docker operations, never arbitrary log text,
                        # URLs, build arguments, credentials or configuration values.
                        size = os.fstat(errors.fileno()).st_size
                        reader.seek(max(0, size - 4096))
                        chunk = reader.read(4096).decode("utf-8", errors="replace")
                        operations = re.findall(r"(?m)^#\d+ .*?\b(load build definition|load metadata|load build context|exporting to image|RUN|COPY|DONE)\b", chunk)
                        if operations:
                            phase = " · " + operations[-1]
                        frame = "|/-\\"[int(elapsed * 4) % 4]
                        terminal.stream.write("\r" + f"{frame} {title} · {elapsed:.0f} с{phase}"[:terminal.width - 1].ljust(terminal.width - 1))
                        terminal.stream.flush()
            limit = 16 * 1024 * 1024 if machine else 64 * 1024
            if machine and os.fstat(output.fileno()).st_size > limit:
                raise RuntimeError("Ответ команды превышает допустимые 16 MiB")
            output.seek(max(0, os.fstat(output.fileno()).st_size - limit))
            errors.seek(max(0, os.fstat(errors.fileno()).st_size - 65536))
            result = subprocess.CompletedProcess(args, process.returncode,
                                                 output.read(limit).decode("utf-8", errors="replace"),
                                                 errors.read(65536).decode("utf-8", errors="replace"))
            state = "готово" if result.returncode == 0 else f"код {result.returncode}"
            return result
        except KeyboardInterrupt:
            state = "прервано"
            raise
        except subprocess.TimeoutExpired:
            state = "превышен лимит времени"
            raise
        finally:
            if process is not None:
                stop(process)
            if interactive:
                terminal.stream.write("\r" + " " * (terminal.width - 1) + "\r")
            terminal.line(f"{title}: {state} · {time.monotonic() - started:.1f} с")
            terminal.stream.flush()
