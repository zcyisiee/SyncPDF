"""Drain text subprocesses incrementally without blocking on stdin or stderr."""
from __future__ import annotations

import codecs
import contextlib
import queue
import subprocess
import threading
import time
from collections.abc import Callable

import psutil


def run_stream(argv: list[str], *, input: str, timeout: float,  # noqa: A002
               on_stdout: Callable[[str], None], cwd=None) -> subprocess.CompletedProcess:
    # Inherit the bdt job's process group so cancellation reaches all descendants.
    proc = subprocess.Popen(  # noqa: S603 - explicit argv, never shell
        argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, cwd=cwd)
    messages: queue.Queue = queue.Queue()
    stderr = []
    stdout = []

    def write():
        try:
            proc.stdin.write(input.encode("utf-8"))
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass

    def read(pipe, name):
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        try:
            while chunk := pipe.read1(65536):
                messages.put((name, decoder.decode(chunk)))
            messages.put((name, decoder.decode(b"", final=True)))
        finally:
            pipe.close()
            messages.put((name, None))

    threads = [threading.Thread(target=write, daemon=True),
               threading.Thread(target=read, args=(proc.stdout, "stdout"), daemon=True),
               threading.Thread(target=read, args=(proc.stderr, "stderr"), daemon=True)]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + timeout
    closed = 0
    try:
        while closed < 2:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(argv, timeout)
            try:
                name, text = messages.get(timeout=remaining)
            except queue.Empty:
                raise subprocess.TimeoutExpired(argv, timeout) from None
            if text is None:
                closed += 1
            elif name == "stdout":
                stdout.append(text)
                if text:
                    on_stdout(text)
            else:
                stderr.append(text)
        proc.wait(timeout=max(0.001, deadline - time.monotonic()))
    except BaseException:
        kill_process_tree(proc)
        raise
    return subprocess.CompletedProcess(argv, proc.returncode, "".join(stdout), "".join(stderr))


def kill_process_tree(proc):
    """Stop only this child and its descendants; preserve the enclosing job group."""
    with contextlib.suppress(psutil.NoSuchProcess):
        children = psutil.Process(proc.pid).children(recursive=True)
        for child in reversed(children):
            with contextlib.suppress(psutil.NoSuchProcess):
                child.kill()
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    proc.wait()
