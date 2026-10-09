"""Validated file configuration with JSON environment overrides."""
import copy
import ipaddress
import json
import math
import os
import re
from pathlib import Path


GROUPS = ("primary", "additional", "emergency")
SERVER_FIELDS = {"id", "host", "ports", "min_download_mbps", "min_upload_mbps"}


DEFAULT = {
    "node": "stolas-node", "seconds": 10, "parallel": 4,
    "connect_timeout": 5, "process_timeout": 25, "cycle_timeout": 240,
    "attempts_per_server": 2, "retry_delay": 2, "reserve_every": 8,
    "min_interval": 300, "history_limit": 10000, "bind_address": None,
    "route": {"mode": "required", "public_ip_url": "https://api.ipify.org",
              "expected_public_cidrs": [], "interface": None, "gateway": None},
    "server_groups": {
        "primary": {"selection": "sequential", "servers": [
            {"id": "hostkey-msk", "host": "spd-rudp.hostkey.ru", "ports": list(range(5201, 5210)),
             "min_download_mbps": 500, "min_upload_mbps": 500}]},
        "additional": {"selection": "sequential", "servers": [
            {"id": "mts-msk", "host": "mskst.st.mtsws.net", "ports": [3333],
             "min_download_mbps": 500, "min_upload_mbps": 500}]},
        "emergency": {"selection": "sequential", "servers": [
            {"id": "ruweb-msk", "host": "msk.speed.ruweb.net", "ports": [5201],
             "min_download_mbps": 500, "min_upload_mbps": 300}]},
    },
}


def migrate_servers(supplied):
    """Accept the old flat list without silently dropping custom endpoints."""
    supplied = copy.deepcopy(supplied)
    if "servers" not in supplied:
        return supplied
    if "server_groups" in supplied:
        raise ValueError("Use server_groups or legacy servers, not both")
    servers = supplied.pop("servers")
    if not isinstance(servers, list) or not servers:
        raise ValueError("Expected a nonempty legacy servers list")
    # Preserve the previous traversal order: first / second / all remaining.
    supplied["server_groups"] = {
        name: {"selection": "sequential", "servers": entries}
        for name, entries in zip(GROUPS, (servers[:1], servers[1:2], servers[2:]))
    }
    return supplied


def load(path=None):
    cfg = copy.deepcopy(DEFAULT)
    supplied = json.loads(Path(path).read_text(encoding="utf-8")) if path else {}
    if not isinstance(supplied, dict):
        raise ValueError("Unknown configuration keys")
    supplied = migrate_servers(supplied)
    if supplied.keys() - cfg.keys():
        raise ValueError("Unknown configuration keys")
    cfg.update(supplied)
    for key in DEFAULT:
        env = os.getenv("STOLAS_" + key.upper())
        if env is not None:
            cfg[key] = env if key == "node" else json.loads(env)
    legacy_env = os.getenv("STOLAS_SERVERS")
    if legacy_env is not None:
        if os.getenv("STOLAS_SERVER_GROUPS") is not None:
            raise ValueError("Use STOLAS_SERVER_GROUPS or legacy STOLAS_SERVERS, not both")
        cfg["server_groups"] = migrate_servers({"servers": json.loads(legacy_env)})["server_groups"]
    if not isinstance(cfg["node"], str) or not re.fullmatch(r"[\w.-]{1,64}", cfg["node"]):
        raise ValueError("Invalid node id")
    limits = {"seconds": (1, 60), "parallel": (1, 16), "connect_timeout": (1, 30),
              "process_timeout": (5, 120), "cycle_timeout": (10, 600),
              "attempts_per_server": (1, 9), "retry_delay": (0, 30),
              "reserve_every": (1, 1000), "min_interval": (0, 86400),
              "history_limit": (1, 100000)}
    for key, (lo, hi) in limits.items():
        if type(cfg[key]) is not int or not lo <= cfg[key] <= hi:
            raise ValueError("Invalid " + key)
    if cfg["process_timeout"] < cfg["seconds"] + cfg["connect_timeout"] + 2:
        raise ValueError("process_timeout must allow test and connect time")
    if cfg["bind_address"] is not None:
        ipaddress.IPv4Address(cfg["bind_address"])
    groups = cfg["server_groups"]
    if not isinstance(groups, dict) or set(groups) != set(GROUPS):
        raise ValueError("Expected primary, additional and emergency server_groups")
    for name in GROUPS:
        group = groups[name]
        if not isinstance(group, dict) or set(group) != {"selection", "servers"}:
            raise ValueError("Invalid server group fields: " + name)
        if group["selection"] not in ("random", "sequential"):
            raise ValueError("Invalid server selection: " + name)
        if not isinstance(group["servers"], list) or (name == "primary" and not group["servers"]):
            raise ValueError("Primary group needs servers; other groups may be empty")
    ids, hosts = set(), set()
    for s in (s for name in GROUPS for s in groups[name]["servers"]):
        if not isinstance(s, dict) or set(s) != SERVER_FIELDS:
            raise ValueError("Invalid server fields")
        if not isinstance(s["id"], str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", s["id"]):
            raise ValueError("Invalid server id")
        if not isinstance(s["host"], str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.-]{0,252}", s["host"]):
            raise ValueError("Invalid hostname")
        if s["id"] in ids or s["host"].lower() in hosts:
            raise ValueError("Confirmation servers must have distinct ids and hosts")
        ids.add(s["id"])
        hosts.add(s["host"].lower())
        if not isinstance(s["ports"], list) or not 1 <= len(s["ports"]) <= 9 or any(type(p) is not int or not 1 <= p <= 65535 for p in s["ports"]):
            raise ValueError("Invalid ports")
        for key in ("min_download_mbps", "min_upload_mbps"):
            if type(s[key]) not in (int, float) or not math.isfinite(s[key]) or not 0 <= s[key] <= 1000000:
                raise ValueError("Invalid threshold")
    r = cfg["route"]
    if not isinstance(r, dict) or r.keys() - DEFAULT["route"].keys():
        raise ValueError("Invalid route settings")
    cfg["route"] = r = DEFAULT["route"] | r
    if r["mode"] not in ("required", "off"):
        raise ValueError("route.mode must be required or off")
    if not isinstance(r["public_ip_url"], str) or not r["public_ip_url"].startswith("https://"):
        raise ValueError("Public IP probe must use HTTPS")
    if not isinstance(r["expected_public_cidrs"], list):
        raise ValueError("Expected CIDR list")
    for cidr in r["expected_public_cidrs"]:
        ipaddress.IPv4Network(cidr)
    for key in ("interface", "gateway"):
        if r[key] is not None and (not isinstance(r[key], str) or not re.fullmatch(r"[\w.:-]{1,64}", r[key])):
            raise ValueError("Invalid route " + key)
    return cfg
