import datetime as dt
import random
import time
import uuid

from . import probes
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
        for attempt in range(cfg["attempts_per_server"]):
            self._remaining(deadline)
            port = server["ports"][attempt % len(server["ports"])]
            sample = {"server": server["id"], "group": group,
                      "selection": cfg["server_groups"][group]["selection"],
                      "endpoint": {"host": server["host"], "address": address, "port": port},
                      "time": utcnow(), "reason": reason, "attempt": attempt + 1,
                      "download": None, "upload": None, "valid": False, "errors": [],
                      "route_checks": [], "low_directions": []}
            result["attempts"].append(sample)
            attempt_start = time.monotonic()
            try:
                for direction, reverse in (("download", True), ("upload", False)):
                    sample["route_checks"].append(probes.guard(cfg, address, min(12, self._remaining(deadline))))
                    sample[direction] = probes.measure(cfg, address, port, reverse, min(cfg["process_timeout"], self._remaining(deadline)))
                    sample["route_checks"].append(probes.guard(cfg, address, min(12, self._remaining(deadline))))
                sample["valid"] = True
                sample["low_directions"] = [d for d in ("download", "upload") if sample[d]["mbps"] < server["min_" + d + "_mbps"]]
                return sample
            except probes.ProbeError as e:
                sample["errors"].append(e.kind)
                if e.kind.startswith("route_"):
                    result["status"] = "route_blocked"
                    raise
                if e.kind == "cycle_timeout":
                    raise
            finally:
                sample["duration_seconds"] = round(time.monotonic() - attempt_start, 3)
            if attempt + 1 < cfg["attempts_per_server"]:
                time.sleep(min(cfg["retry_delay"], self._remaining(deadline)))
        return None

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
                    result["wan_alert"] = cfg["route"]["mode"] == "required"
                else:
                    result["status"] = "server_disagreement"
        result["duration_seconds"] = round(time.monotonic() - start, 3)
        self.store.save(result, cfg["history_limit"])
        return result

    @staticmethod
    def _remaining(deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise probes.ProbeError("cycle_timeout")
        return remaining
