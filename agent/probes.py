import ipaddress
import json
import math
import socket
import subprocess
import time
import urllib.request


class ProbeError(Exception):
    def __init__(self, kind):
        self.kind = kind
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
        p = subprocess.run([sys.executable, "-c", code], input=json.dumps([cfg, address]), capture_output=True, text=True, timeout=timeout, check=True)
        data = json.loads(p.stdout)
        if "error" in data:
            raise ProbeError(data["error"])
        return data
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        raise ProbeError("route_probe_failed") from e


def guard_main():
    import sys
    cfg, address = json.load(sys.stdin)
    try:
        print(json.dumps(_guard(cfg, address)))
    except ProbeError as e:
        print(json.dumps({"error": e.kind}))


def _guard(cfg, address):
    r = cfg["route"]
    if r["mode"] == "off":
        return {"verified": False, "reason": "explicitly_disabled"}
    if not r["expected_public_cidrs"]:
        raise ProbeError("route_unconfigured")
    try:
        cmd = ["ip", "-j", "-4", "route", "get", address]
        if cfg["bind_address"]:
            cmd += ["from", cfg["bind_address"]]
        route = json.loads(subprocess.run(cmd, capture_output=True, text=True, timeout=3, check=True).stdout)[0]
        if r["interface"] and route.get("dev") != r["interface"]:
            raise ProbeError("route_interface_mismatch")
        if r["gateway"] and route.get("gateway") != r["gateway"]:
            raise ProbeError("route_gateway_mismatch")
        # Ignore proxy environment: the witness must observe this node's direct egress.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(r["public_ip_url"], timeout=5) as response:
            if not response.geturl().startswith("https://"):
                raise ProbeError("route_probe_insecure_redirect")
            public_ip = ipaddress.IPv4Address(response.read(128).decode().strip())
        if not any(public_ip in ipaddress.IPv4Network(n) for n in r["expected_public_cidrs"]):
            raise ProbeError("route_public_ip_mismatch")
        return {"verified": True, "method": "route_and_https_egress", "interface": route.get("dev")}
    except ProbeError:
        raise
    except (OSError, ValueError, KeyError, IndexError, subprocess.SubprocessError) as e:
        raise ProbeError("route_probe_failed") from e


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
