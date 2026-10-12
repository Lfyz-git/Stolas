"""Cycle-scoped probe isolation; no WAN evidence is cached between checks."""
from contextvars import ContextVar
import ipaddress
import json
import queue
import socket
import subprocess
import sys
import threading
import time

current_session = ContextVar("probe_session", default=None)
_pending = set()
_pending_lock = threading.Lock()
MAX_MESSAGE = 1024 * 1024


def bounded(function, timeout):
    """Bound DNS and response reading even when socket timeouts are ignored."""
    if timeout <= 0:
        raise TimeoutError()
    deadline = time.monotonic() + timeout
    replies = queue.Queue(maxsize=1)

    def run():
        try:
            reply = (True, function())
        except Exception as error:
            reply = (False, error)
        finally:
            with _pending_lock:
                _pending.discard(threading.current_thread())
        replies.put(reply)

    thread = threading.Thread(target=run, daemon=True)
    with _pending_lock:
        _pending.add(thread)
    try:
        thread.start()
    except Exception:
        with _pending_lock:
            _pending.discard(thread)
        raise
    try:
        ok, value = replies.get(timeout=max(0, deadline - time.monotonic()))
    except queue.Empty:
        raise TimeoutError() from None
    if time.monotonic() >= deadline:
        raise TimeoutError()
    if not ok:
        raise value
    return value


def worker_main():
    from agent import probes
    print(json.dumps({"ready": True}), flush=True)
    while True:
        line = sys.stdin.readline(MAX_MESSAGE)
        if not line:
            return
        if not line.endswith("\n"):
            return
        request = json.loads(line)
        remaining = max(0, request["deadline"] - time.monotonic())
        try:
            if request["op"] == "resolve":
                value = bounded(lambda: str(ipaddress.IPv4Address(socket.gethostbyname(request["args"][0]))), remaining)
            elif request["op"] == "guard":
                value = probes._guard(*request["args"], timeout=remaining)
            else:
                return
            reply = {"value": value}
        except probes.ProbeError as error:
            reply = {"error": error.kind, "details": error.details}
        except (OSError, ValueError, TimeoutError):
            reply = {"error": "dns_error" if request["op"] == "resolve" else "route_verification_unavailable"}
        with _pending_lock:
            reply["retire"] = bool(_pending)
        print(json.dumps(reply), flush=True)
        # A stalled resolver must not accumulate across checks in a reused worker.
        if reply["retire"]:
            return


class WorkerError(Exception):
    pass


class ProbeSession:
    def __init__(self):
        self.process = None
        self.timings = dict(dns_seconds=0.0, wan_verification_seconds=0.0,
                            local_route_seconds=0.0, https_witness_seconds=0.0,
                            probe_startup_seconds=0.0, probe_process_starts=0,
                            iperf_seconds=0.0, retry_delay_seconds=0.0)

    def __enter__(self):
        self.token = current_session.set(self)
        return self

    def __exit__(self, *args):
        current_session.reset(self.token)
        self.close()

    def close(self):
        process, self.process = self.process, None
        if process is not None:
            try:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                pass  # Cleanup must not replace the probe's failure classification.
            finally:
                process.stdin.close()
                process.stdout.close()

    def request(self, operation, arguments, timeout):
        started = time.monotonic()
        fresh = self.process is None
        startup_recorded = False
        try:
            if fresh:
                self.process = subprocess.Popen([sys.executable, "-c", "from agent.probe_worker import worker_main; worker_main()"],
                                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
                self.timings["probe_process_starts"] += 1
            process = self.process
            deadline = started + max(0, timeout - (0.2 if operation == "guard" else 0.05))
            message = (json.dumps(dict(op=operation, args=arguments, deadline=deadline)) + "\n").encode()
            if len(message) > MAX_MESSAGE:
                raise WorkerError()
            replies = queue.Queue(maxsize=1)

            def exchange():
                startup = None
                try:
                    if fresh:
                        if json.loads(process.stdout.readline(MAX_MESSAGE)) != {"ready": True}:
                            raise WorkerError()
                        startup = time.monotonic() - started
                    # Even a full pipe during startup is covered by the caller's deadline.
                    view = memoryview(message)
                    while view:
                        written = process.stdin.write(view)
                        if not written:
                            raise WorkerError()
                        view = view[written:]
                    data = json.loads(process.stdout.readline(MAX_MESSAGE))
                    if not isinstance(data, dict) or not ("value" in data or "error" in data):
                        raise WorkerError()
                    replies.put((data, startup))
                except Exception:
                    replies.put((None, startup))

            threading.Thread(target=exchange, daemon=True).start()
            data, startup = replies.get(timeout=max(0, started + timeout - time.monotonic()))
            if fresh:
                self.timings["probe_startup_seconds"] += startup if startup is not None else time.monotonic() - started
                startup_recorded = True
            if data is None or time.monotonic() >= started + timeout:
                raise WorkerError()
            if data.get("retire"):
                self.close()
            return data
        except (OSError, ValueError, queue.Empty, WorkerError):
            if fresh and not startup_recorded:
                self.timings["probe_startup_seconds"] += time.monotonic() - started
            self.close()
            raise WorkerError() from None

    def resolve(self, host, timeout):
        from agent.probes import ProbeError
        started = time.monotonic()
        try:
            data = self.request("resolve", [host], timeout)
            if "error" in data:
                raise ProbeError(data["error"])
            return str(ipaddress.IPv4Address(data["value"]))
        except (WorkerError, ValueError, TypeError):
            raise ProbeError("dns_error") from None
        finally:
            self.timings["dns_seconds"] += time.monotonic() - started

    def guard(self, cfg, address, timeout):
        from agent.probes import ProbeError
        started = time.monotonic()
        try:
            data = self.request("guard", [cfg, address], min(timeout, cfg["route"]["verification_timeout"]))
            details = data.get("value", data.get("details", {}))
            for source, target in (("local_route_seconds", "local_route_seconds"), ("https_witness_seconds", "https_witness_seconds")):
                self.timings[target] += details.get("timings", {}).get(source, 0)
            if "error" in data:
                raise ProbeError(data["error"], data.get("details"))
            return data["value"]
        except WorkerError:
            raise ProbeError("route_verification_unavailable", {"verified": False, "allow_measurement": False,
                             "verification_status": "unavailable", "sources": [
                                 {"url": url, "status": "error", "reason": "guard_process_failed_or_timed_out"}
                                 for url in cfg["route"]["public_ip_urls"]]}) from None
        finally:
            self.timings["wan_verification_seconds"] += time.monotonic() - started
