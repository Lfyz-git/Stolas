import datetime as dt
import time
import uuid

from . import probes
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

    def _cycle(self):
        cfg = self.cfg
        start = time.monotonic()
        deadline = start + cfg["cycle_timeout"]
        result = {"schema_version": 1, "id": str(uuid.uuid4()), "node": cfg["node"],
                  "time": utcnow(), "started_epoch": time.time(), "status": "unavailable",
                  "wan_alert": False, "primary": None, "confirmation": None,
                  "attempts": [], "errors": [], "parameters": {"ipv": 4, "protocol": "tcp",
                  "parallel": cfg["parallel"], "seconds": cfg["seconds"]}}
        samples = []
        reserve = (self.store.sequence() + 1) % cfg["reserve_every"] == 0
        try:
            for server in cfg["servers"]:
                primary = next((s for s in samples if s["valid"]), None)
                if primary and not primary["low_directions"] and not reserve:
                    break
                if primary and primary["low_directions"] and any(s["valid"] and s["endpoint"]["address"] != primary["endpoint"]["address"] for s in samples[1:]) and not reserve:
                    break
                reason = "primary" if not result["attempts"] else ("confirmation" if primary and primary["low_directions"] else "reserve" if primary else "fallback")
                try:
                    self._remaining(deadline)
                    address = probes.resolve(server["host"], min(5, self._remaining(deadline)))
                except probes.ProbeError as e:
                    result["errors"].append({"server": server["id"], "reason": e.kind})
                    continue
                for attempt in range(cfg["attempts_per_server"]):
                    port = server["ports"][attempt % len(server["ports"])]
                    sample = {"server": server["id"], "endpoint": {"host": server["host"], "address": address, "port": port},
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
                        samples.append(sample)
                        break
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
