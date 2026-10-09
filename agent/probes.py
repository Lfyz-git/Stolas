import ipaddress
import json
import math
import socket
import subprocess
import time
import urllib.request
import urllib.error


class ProbeError(Exception):
    def __init__(self, kind, details=None):
        self.kind = kind
        self.details = details or {}
        super().__init__(kind)


def resolve(host, timeout=5):
    # Resolution runs in a child so a broken system resolver cannot hang a cycle.
    import sys
    code = "import socket,sys; print(socket.gethostbyname(sys.argv[1]))"
    try:
        p = subprocess.run([sys.executable, "-c", code, host], capture_output=True, text=True, timeout=timeout, check=True)
        return str(ipaddress.IPv4Address(p.stdout.strip()))
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        raise ProbeError("dns_error") from e


def guard(cfg, address, timeout=12):
    if cfg["route"]["mode"] == "off":
        return {"verified": False, "reason": "explicitly_disabled"}
    import sys
    code = "from agent.probes import guard_main; guard_main()"
    try:
        budget = min(timeout, cfg["route"]["verification_timeout"])
        p = subprocess.run([sys.executable, "-c", code], input=json.dumps([cfg, address, max(0.01, budget - 0.2)]), capture_output=True, text=True, timeout=budget, check=True)
        data = json.loads(p.stdout)
        if "error" in data:
            raise ProbeError(data["error"], data.get("details"))
        return data
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        raise ProbeError("route_verification_unavailable", {"verified": False, "verification_status": "unavailable",
                         "sources": [{"url": url, "status": "error", "reason": "guard_process_failed_or_timed_out"} for url in cfg["route"]["public_ip_urls"]]}) from e


def guard_main():
    import sys
    cfg, address, timeout = json.load(sys.stdin)
    try:
        print(json.dumps(_guard(cfg, address, timeout)))
    except ProbeError as e:
        print(json.dumps({"error": e.kind, "details": e.details}))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _read_public_ip(url, timeout):
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(url, timeout=timeout) as response:
            return str(ipaddress.IPv4Address(response.read(128).decode("ascii").strip()))
    except urllib.error.HTTPError:
        raise ProbeError("http_error") from None
    except urllib.error.URLError as error:
        reason = "dns_error" if isinstance(error.reason, socket.gaierror) else "timeout" if isinstance(error.reason, TimeoutError) else "connection_error"
        raise ProbeError(reason) from None
    except TimeoutError:
        raise ProbeError("timeout") from None
    except (ValueError, UnicodeError):
        raise ProbeError("invalid_ipv4") from None
    except OSError:
        raise ProbeError("connection_error") from None


def public_ip_main():
    import sys
    url, timeout = json.load(sys.stdin)
    try:
        print(json.dumps({"ip": _read_public_ip(url, timeout)}))
    except ProbeError as error:
        print(json.dumps({"error": error.kind}))


def probe_public_ip(url, timeout):
    # Each witness gets a separate bounded child, including its DNS resolution.
    import sys
    try:
        result = subprocess.run([sys.executable, "-c", "from agent.probes import public_ip_main; public_ip_main()"],
                                input=json.dumps([url, timeout]), capture_output=True, text=True, timeout=timeout, check=True)
        data = json.loads(result.stdout)
        if "error" in data:
            raise ProbeError(data["error"])
        return str(ipaddress.IPv4Address(data["ip"]))
    except subprocess.TimeoutExpired:
        raise ProbeError("timeout") from None
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        raise ProbeError("probe_failed") from None


def _guard(cfg, address, timeout=None):
    r = cfg["route"]
    if r["mode"] == "off":
        return {"verified": False, "reason": "explicitly_disabled"}
    if not r["expected_public_cidrs"]:
        raise ProbeError("route_unconfigured")
    deadline = time.monotonic() + (timeout if timeout is not None else r["verification_timeout"])
    details = {"verified": False, "verification_status": "unavailable", "sources": []}
    try:
        cmd = ["ip", "-j", "-4", "route", "get", address]
        if cfg["bind_address"]:
            cmd += ["from", cfg["bind_address"]]
        route = json.loads(subprocess.run(cmd, capture_output=True, text=True, timeout=min(3, max(0.01, deadline - time.monotonic())), check=True).stdout)[0]
        if r["interface"] and route.get("dev") != r["interface"]:
            raise ProbeError("route_interface_mismatch")
        if r["gateway"] and route.get("gateway") != r["gateway"]:
            raise ProbeError("route_gateway_mismatch")
        observations = []
        for index, url in enumerate(r["public_ip_urls"]):
            remaining = deadline - time.monotonic()
            witness = {"url": url}
            details["sources"].append(witness)
            if remaining <= 0.05:
                witness.update(status="error", reason="budget_exhausted")
                continue
            try:
                value = probe_public_ip(url, min(r["source_timeout"], remaining / (len(r["public_ip_urls"]) - index)))
                observations.append(value)
                witness.update(status="ok", ip=value)
            except ProbeError as error:
                witness.update(status="error", reason=error.kind)
        if len(set(observations)) > 1:
            details["verification_status"] = "conflict"
            raise ProbeError("route_verification_conflict", details)
        if observations and not any(ipaddress.IPv4Address(observations[0]) in ipaddress.IPv4Network(n) for n in r["expected_public_cidrs"]):
            details["verification_status"] = "mismatch"
            raise ProbeError("route_public_ip_mismatch", details)
        if len(observations) < r["min_confirmations"]:
            raise ProbeError("route_verification_unavailable", details)
        return {**details, "verified": True, "verification_status": "verified", "public_ip": observations[0], "method": "route_and_https_egress", "interface": route.get("dev")}
    except ProbeError as error:
        if not error.details:
            error.details = {**details, "verification_status": "mismatch" if error.kind.endswith("_mismatch") else "unavailable"}
        raise
    except (OSError, ValueError, KeyError, IndexError, subprocess.SubprocessError) as e:
        raise ProbeError("route_verification_unavailable", details) from e


def parse_iperf(data):
    if data.get("error"):
        message = str(data["error"]).lower()
        raise ProbeError("server_busy" if "busy" in message else "iperf_error")
    try:
        end = data["end"]
        receiver = end["sum_received"]
        sender = end["sum_sent"]
        bps, seconds = receiver["bits_per_second"], receiver["seconds"]
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in (bps, seconds)) or bps < 0 or seconds <= 0:
            raise ValueError()
        retransmits = sender.get("retransmits")
        if retransmits is not None and (type(retransmits) is not int or retransmits < 0):
            raise ValueError()
        return {"mbps": round(bps / 1000000, 3), "tcp_retransmits": retransmits,
                "measured_seconds": seconds, "latency_ms": None, "latency_method": None}
    except (KeyError, TypeError, ValueError) as e:
        raise ProbeError("invalid_iperf_json") from e


def measure(cfg, address, port, reverse, timeout):
    cmd = ["iperf3", "-4", "-c", address, "-p", str(port), "-J", "-P", str(cfg["parallel"]),
           "-t", str(cfg["seconds"]), "--connect-timeout", str(cfg["connect_timeout"] * 1000)]
    if reverse:
        cmd.append("-R")
    if cfg["bind_address"]:
        cmd += ["-B", cfg["bind_address"]]
    start = time.monotonic()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        data = json.loads(p.stdout)
        if not isinstance(data, dict):
            raise ValueError()
        result = parse_iperf(data)
        if p.returncode:
            raise ProbeError("iperf_exit_error")
        result["duration_seconds"] = round(time.monotonic() - start, 3)
        return result
    except subprocess.TimeoutExpired as e:
        raise ProbeError("test_timeout") from e
    except FileNotFoundError as e:
        raise ProbeError("iperf_not_installed") from e
    except (OSError, ValueError) as e:
        raise ProbeError("invalid_iperf_json") from e
