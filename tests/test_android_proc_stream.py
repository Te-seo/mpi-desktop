import concurrent.futures
import subprocess
import sys
import threading

import pytest

from mpi.android import adb, proc_stream


def mock_adb_shell(monkeypatch, body):
    """Exercise real bidirectional OS pipes, including EOF and termination."""
    launch = subprocess.Popen
    processes = []

    def open_shell(args, **kwargs):
        assert args[:5] == ["adb", "-s", "serial", "shell", "-T"]
        marker = args[-1].split("_MPI_SAMPLE_END_")[1][:32]
        program = "import sys, time\nmarker='_MPI_SAMPLE_END_" + marker + "'\n" + body
        process = launch([sys.executable, "-u", "-c", program], **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(proc_stream.subprocess, "Popen", open_shell)
    return processes


def test_reuses_shell_returns_only_fresh_blocks_and_replaces_pid_command(monkeypatch):
    children = mock_adb_shell(monkeypatch, """
for tick, _ in enumerate(sys.stdin):
    print(tick)
    print(marker, flush=True)
""")
    stream = proc_stream.ProcStream("serial")
    try:
        assert stream.read("read pid 15") == "0\n"
        assert stream.read("read pid 15") == "1\n"
        assert len(children) == 1
        assert stream.read("read pid 20") == "0\n"
        assert children[0].poll() is not None
    finally:
        stream.close()
    assert len(children) == 2
    assert all(child.poll() is not None for child in children)


def test_truncated_block_falls_back_without_publishing_partial_sample(monkeypatch):
    children = mock_adb_shell(monkeypatch, "sys.stdin.readline()\nprint('partial', flush=True)")
    calls = []

    def fallback(command, **kwargs):
        calls.append((command, kwargs["serial"]))
        return "complete"

    monkeypatch.setattr(adb, "shell", fallback)
    stream = proc_stream.ProcStream("serial")
    try:
        assert stream.read("probe") == stream.read("probe") == "complete"
        assert len(children) == 1
        assert calls == [("probe", "serial")] * 2
    finally:
        stream.close()


def test_stop_interrupts_blocked_read_without_opening_another_shell(monkeypatch):
    children = mock_adb_shell(monkeypatch, "sys.stdin.readline()\ntime.sleep(20)")
    stream = proc_stream.ProcStream("serial")
    started = threading.Event()
    read = proc_stream._Channel.read

    def waiting(channel, timeout):
        started.set()
        return read(channel, timeout)

    monkeypatch.setattr(proc_stream._Channel, "read", waiting)
    with concurrent.futures.ThreadPoolExecutor(1) as worker:
        pending = worker.submit(stream.read, "probe")
        try:
            assert started.wait(3)
            stream.close()
            with pytest.raises(adb.AdbError, match="closed"):
                pending.result(timeout=2)
        finally:
            stream.close()
    assert children[0].poll() is not None
