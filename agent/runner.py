import datetime as dt
import random
import time
import uuid

from . import probes
from .events import emit
from .config import GROUPS
from .storage import Busy


class Cooldown(Exception):
    pass


def utcnow():
    return dt.datetime.now(dt.timezone.utc).isoformat()


class Runner:
    def __init__(self, cfg, store):
        self.cfg, self.store = cfg, store

    def run(self):
        with self.store.lock():
            last = self.store.latest()
            if last and time.time() - last["started_epoch"] < self.cfg["min_interval"]:
                raise Cooldown("Minimum interval has not elapsed")
            return self._cycle()

    def _ordered_servers(self, name, deadline):
        group = self.cfg["server_groups"][name]
        servers = group["servers"]
        if group["selection"] == "random":
            ordered = list(servers)
            random.shuffle(ordered)
            for server in ordered:
                self._remaining(deadline)
                yield server
        else:
            for _ in servers:
                self._remaining(deadline)
                yield servers[self.store.next_server_index(name, len(servers))]

    def _measure_server(self, server, address, group, reason, result, deadline):
        cfg = self.cfg
        busy_ports = set()
        measurement_errors = 0
        total_limit = cfg["attempts_per_server"] + min(len(server["ports"]), cfg["busy_attempts_per_server"])
        for attempt in range(total_limit):
            self._remaining(deadline)
            available = [p for p in server["ports"] if p not in busy_ports]
            if not available:
                break
            port = next((server["ports"][(attempt + offset) % len(server["ports"])] for offset in range(len(server["ports"])) if server["ports"][(attempt + offset) % len(server["ports"])] not in busy_ports))
            sample = {"server": server["id"], "group": group,
                      "selection": cfg["server_groups"][group]["selection"],
                      "endpoint": {"host": server["host"], "address": address, "port": port},
                      "time": utcnow(), "reason": reason, "attempt": attempt + 1,
                      "download": None, "upload": None, "valid": False, "errors": [],
                      "route_checks": [], "low_directions": []}
            result["attempts"].append(sample)
            if attempt:
                emit("measurement_retry", "WARNING", test_id=result["id"], server=server["id"], attempt=attempt + 1)
            attempt_start = time.monotonic()
            try:
                for direction, reverse in (("download", True), ("upload", False)):
                    self._route_check(sample, cfg, address, deadline, result)
                    sample[direction] = probes.measure(cfg, address, port, reverse, min(cfg["process_timeout"], self._remaining(deadline)))
                    self._route_check(sample, cfg, address, deadline, result)
                sample["valid"] = True
                sample["low_directions"] = [d for d in ("download", "upload") if sample[d]["mbps"] < server["min_" + d + "_mbps"]]
                return sample
            except probes.ProbeError as e:
                sample["errors"].append(e.kind)
                event = "wan_check_failed" if e.kind.startswith("route_") else "server_busy" if e.kind == "server_busy" else "server_error"
                emit(event, "ERROR" if event == "wan_check_failed" else "WARNING", test_id=result["id"], server=server["id"], reason=e.kind)
                if e.kind.startswith("route_"):
                    result["status"] = "route_blocked"
                    raise
                if e.kind == "cycle_timeout":
                    raise
                if e.kind == "server_busy":
                    busy_ports.add(port)
                    exhausted = len(busy_ports) >= min(len(server["ports"]), cfg["busy_attempts_per_server"])
                    sample["switch_reason"] = "busy_ports_exhausted" if exhausted else "next_port_busy"
                else:
                    measurement_errors += 1
                    exhausted = measurement_errors >= cfg["attempts_per_server"]
                    sample["switch_reason"] = "measurement_attempts_exhausted" if exhausted else "retry_measurement"
                if exhausted:
                    break
            finally:
                sample["duration_seconds"] = round(time.monotonic() - attempt_start, 3)
            if attempt + 1 < total_limit:
                time.sleep(min(cfg["retry_delay"], self._remaining(deadline)))
        return None

    def _route_check(self, sample, cfg, address, deadline, result):
        try:
            check = probes.guard(cfg, address, min(cfg["route"]["verification_timeout"], self._remaining(deadline)))
            sample["route_checks"].append(check)
            if check.get("warning") and check["warning"] not in result.setdefault("warnings", []):
                result["warnings"].append(check["warning"])
                emit("wan_check_warning", "WARNING", test_id=result["id"], server=sample["server"], reason=check["warning"])
        except probes.ProbeError as error:
            sample["route_checks"].append({"verified": False, "reason": error.kind, **error.details})
            raise

    def _cycle(self):
        cfg = self.cfg
        start = time.monotonic()
        deadline = start + cfg["cycle_timeout"]
        result = {"schema_version": 1, "id": str(uuid.uuid4()), "node": cfg["node"],
                  "time": utcnow(), "started_epoch": time.time(), "status": "unavailable",
                  "wan_alert": False, "primary": None, "confirmation": None,
                  "attempts": [], "errors": [], "parameters": {"ipv": 4, "protocol": "tcp",
                  "parallel": cfg["parallel"], "seconds": cfg["seconds"],
                  "server_selection": {name: cfg["server_groups"][name]["selection"] for name in GROUPS}}}
        samples = []
        emit("measurement_started", "DEBUG", test_id=result["id"])
        reserve = (self.store.sequence() + 1) % cfg["reserve_every"] == 0
        try:
            for group in GROUPS:
                primary = samples[0] if samples else None
                needs_confirmation = bool(primary and primary["low_directions"] and not any(s["endpoint"]["address"] != primary["endpoint"]["address"] for s in samples[1:]))
                if primary and not needs_confirmation and not reserve:
                    break
                for server in self._ordered_servers(group, deadline):
                    reason = "confirmation" if needs_confirmation else "reserve" if primary else "fallback" if result["attempts"] or result["errors"] else "primary"
                    try:
                        address = probes.resolve(server["host"], min(5, self._remaining(deadline)))
                    except probes.ProbeError as e:
                        emit("server_unavailable", "WARNING", test_id=result["id"], server=server["id"], reason=e.kind)
                        result["errors"].append({"server": server["id"], "group": group, "reason": e.kind})
                        if e.kind == "cycle_timeout":
                            raise
                        continue
                    if needs_confirmation and address == primary["endpoint"]["address"]:
                        result["errors"].append({"server": server["id"], "group": group, "reason": "confirmation_not_independent"})
                        continue
                    sample = self._measure_server(server, address, group, reason, result, deadline)
                    if sample:
                        samples.append(sample)
                        break  # One valid representative per group, not a full-group audit.
        except probes.ProbeError as e:
            result["errors"].append({"reason": e.kind})
        valid = [s for s in samples if s["valid"]]
        if valid:
            result["primary"] = valid[0]
            if len(valid) > 1 and valid[0]["low_directions"]:
                # Different host aliases resolving to one address are not independent evidence.
                result["confirmation"] = next((s for s in valid[1:] if s["endpoint"]["address"] != valid[0]["endpoint"]["address"]), None)
            if result["status"] != "route_blocked":
                first, second = result["primary"], result["confirmation"]
                if not first["low_directions"]:
                    result["status"] = "ok"
                elif second is None:
                    result["status"] = "low_unconfirmed"
                elif set(first["low_directions"]) & set(second["low_directions"]):
                    result["status"] = "low_confirmed"
                    # Explicitly disabled route checks cannot justify a WAN alert.
                    result["wan_alert"] = cfg["route"]["mode"] == "required" and all(
                        check.get("verified", False) for sample in (first, second) for check in sample["route_checks"])
                else:
                    result["status"] = "server_disagreement"
        result["duration_seconds"] = round(time.monotonic() - start, 3)
        try:
            self.store.save(result, cfg["history_limit"])
        except Exception:
            emit("history_error", "ERROR", test_id=result["id"], operation="save")
            raise
        primary = result["primary"]
        emit("measurement_completed", test_id=result["id"], status=result["status"],
             duration_ms=round(result["duration_seconds"] * 1000),
             server=primary["server"] if primary else None,
             download_mbps=primary["download"]["mbps"] if primary else None,
             upload_mbps=primary["upload"]["mbps"] if primary else None)
        if result["status"] == "low_confirmed":
            emit("speed_degradation_confirmed", "WARNING", test_id=result["id"], status=result["status"])
        return result

    @staticmethod
    def _remaining(deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise probes.ProbeError("cycle_timeout")
        return remaining
