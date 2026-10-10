"""Read-only host/Docker discovery. Never return container environment or secrets."""
import errno
import ipaddress
import json
import os
import platform
from pathlib import Path
import re
import shutil
import socket
import subprocess
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from tools import layout


# Select fields at the Docker API boundary; Config.Env never leaves the daemon.
CONTAINER_FORMAT = '''{"id":{{json .Id}},"name":{{json .Name}},"image":{{json .Config.Image}},"command":{{json .Config.Cmd}},"running":{{json .State.Running}},"mode":{{json .HostConfig.NetworkMode}},"networks":{{json .NetworkSettings.Networks}},"project":{{json (index .Config.Labels "com.docker.compose.project")}},"service":{{json (index .Config.Labels "com.docker.compose.service")}},"directory":{{json (index .Config.Labels "com.docker.compose.project.working_dir")}}}'''
NETWORK_FORMAT = '''{"id":{{json .Id}},"name":{{json .Name}},"driver":{{json .Driver}},"internal":{{json .Internal}},"ipam":{{json .IPAM.Config}},"bridge":{{json (index .Options "com.docker.network.bridge.name")}}}'''


def command(args):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("STOLAS_", "COMPOSE_"))}
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=10, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return subprocess.CompletedProcess(args, 1, "", "command_unavailable_or_timeout")


def ipv4(value):
    try:
        return str(ipaddress.IPv4Address(value))
    except (ValueError, TypeError):
        return None


def discover(root):
    root = Path(root).absolute()
    facts = {"hostname": socket.gethostname(), "tools": {name: bool(shutil.which(name)) for name in ("docker", "ip", "curl", "wget", "tar", "python3", "apt-get", "sudo")},
             "system": platform.system(), "architecture": platform.machine(),
             "addresses": [], "n8n": [], "stolas": [], "networks": {}, "warnings": [],
             "docker": {"available": False, "command": [], "reason": "Docker не установлен"},
             "installation": {"directory": str(root), "config": (root / "config/local.json").is_file(),
                              "env": (root / ".env").is_file(), "draft": layout.state_path(root, ".stolas-draft.json").is_file()}}
    zone = os.environ.get("TZ")
    try:
        if not zone and Path("/etc/timezone").is_file():
            zone = Path("/etc/timezone").read_text().strip()
        if not zone:
            localtime = str(Path("/etc/localtime").resolve())
            if "/zoneinfo/" in localtime:
                zone = localtime.split("/zoneinfo/", 1)[1]
        if zone:
            ZoneInfo(zone)
        facts["timezone"] = zone
    except (OSError, ValueError, ZoneInfoNotFoundError):
        facts["timezone"] = None
    if facts["tools"]["ip"]:
        result = command(["ip", "-j", "-4", "address", "show"])
        try:
            for interface in json.loads(result.stdout) if result.returncode == 0 else []:
                for address in interface.get("addr_info", []):
                    value = ipv4(address.get("local"))
                    if value:
                        facts["addresses"].append({"address": value, "interface": interface["ifname"]})
        except (ValueError, KeyError, TypeError):
            facts["warnings"].append("Не удалось прочитать IPv4 интерфейсов Linux")
    else:
        facts["warnings"].append("Нет утилиты ip: адреса интерфейсов не обнаружены; установите iproute2 для автоматического выбора сети")
    for filename, key in ((".stolas-managed.json", "ref"), (".stolas-install-status.json", "stage")):
        path = layout.state_path(root, filename)
        if layout.runtime(root) and filename == ".stolas-install-status.json":
            path = root / ".stolas/state/status.json"
        if path.is_file() and not path.is_symlink():
            try:
                facts["installation"][key] = json.loads(path.read_text()).get(key, "unknown")
            except (ValueError, OSError):
                facts["warnings"].append("Не читается служебное состояние " + filename)
    progress = layout.state_path(root, ".stolas-progress.json")
    if progress.is_file() and not progress.is_symlink():
        try:
            facts["installation"]["wizard_stage"] = json.loads(progress.read_text()).get("stage")
        except (ValueError, OSError):
            facts["warnings"].append("Не читается состояние этапов мастера")
    if not facts["tools"]["docker"]:
        return facts
    docker = ["docker"]
    result = command(docker + ["info", "--format", "{{.OSType}} {{.Architecture}} {{.ServerVersion}}"])
    permission = "permission denied" in result.stderr.lower() or "access denied" in result.stderr.lower()
    if result.returncode and facts["tools"]["sudo"]:
        elevated = ["sudo", "-n", "docker"]
        attempt = command(elevated + ["info", "--format", "{{.OSType}} {{.Architecture}} {{.ServerVersion}}"])
        if attempt.returncode == 0:
            docker, result = elevated, attempt
    if result.returncode:
        facts["docker"]["reason"] = "Нет прав к Docker socket; настройте доступ или заранее выполните sudo -v" if permission else "Docker API недоступен: проверьте службу Docker и выбранный context"
        return facts
    context = command(docker + ["context", "inspect", "--format", "{{.Endpoints.docker.Host}}"])
    endpoint = os.environ.get("DOCKER_HOST") or context.stdout.strip()
    if context.returncode or not endpoint.startswith("unix://"):
        facts["docker"]["reason"] = "Выбран удалённый Docker context; для локального n8n нужен Docker этого Linux-хоста"
        return facts
    info = result.stdout.split()
    if not info or info[0] != "linux":
        facts["docker"]["reason"] = "Требуется Linux Docker Engine"
        return facts
    compose = command(docker + ["compose", "version", "--short"])
    facts["docker"] = {"available": True, "command": docker, "reason": "", "version": " ".join(info),
                       "compose": compose.stdout.strip() if compose.returncode == 0 else None}
    listed = command(docker + ["ps", "-q"])
    if listed.returncode:
        facts["warnings"].append("Не удалось перечислить запущенные контейнеры Docker")
        return facts
    identifiers = listed.stdout.split()
    if not identifiers:
        return facts
    inspected = command(docker + ["inspect", "--format", CONTAINER_FORMAT, *identifiers])
    if inspected.returncode:
        facts["warnings"].append("Контейнеры изменились или inspect недоступен; повторите обнаружение")
        return facts
    network_ids = set()
    for line in inspected.stdout.splitlines():
        try:
            container = json.loads(line)
            container["name"] = container["name"].lstrip("/")
            cmd = container.pop("command", None) or []
            if not container.get("running"):
                continue
            if container.get("service") == "stolas" and container.get("directory") and Path(container["directory"]).resolve() == root.resolve():
                facts["stolas"].append(container)
            image = container.get("image", "").split("@")[0]
            hints = " ".join(str(container.get(k) or "") for k in ("name", "service")).lower()
            image_name = image.rsplit("/", 1)[-1].split(":")[0]
            official = image_name == "n8n"
            if "worker" in cmd or "webhook" in cmd or "task-runners" in image or re.search(r"(?:^|[ _-])(worker|webhook|runner)(?:$|[ _-])", hints):
                continue
            infrastructure = image_name in ("postgres", "redis", "valkey", "mysql", "mariadb", "nginx", "traefik", "caddy", "rabbitmq")
            explicit_command = bool(cmd and isinstance(cmd[0], str) and Path(cmd[0]).name == "n8n")
            if official or explicit_command or (not infrastructure and re.search(r"(?:^|[ _-])n8n(?:$|[ _-])", hints)):
                container["confidence"] = "image" if official else "metadata"
                facts["n8n"].append(container)
                for network in (container.get("networks") or {}).values():
                    if network.get("NetworkID"):
                        network_ids.add(network["NetworkID"])
        except (ValueError, KeyError, TypeError):
            facts["warnings"].append("Неполные метаданные контейнера; он пропущен")
    if network_ids:
        result = command(docker + ["network", "inspect", "--format", NETWORK_FORMAT, *sorted(network_ids)])
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                try:
                    network = json.loads(line)
                    facts["networks"][network["id"]] = network
                except (ValueError, KeyError, TypeError):
                    facts["warnings"].append("Неполные метаданные Docker-сети")
        else:
            facts["warnings"].append("Не удалось прочитать Docker-сети")
    return facts


def host_candidates(container, facts):
    """Only verified local addresses; no inferred 172.x/host.docker.internal."""
    if container["mode"] == "host":
        return [{"address": "127.0.0.1", "interface": "lo", "network": "сеть хоста", "container": container["name"]}], []
    candidates, reasons = [], []
    for name, attachment in (container.get("networks") or {}).items():
        network = facts["networks"].get(attachment.get("NetworkID"))
        if not network:
            reasons.append(f"{name}: метаданные сети недоступны")
            continue
        if network["driver"] != "bridge":
            reasons.append(f"{name}: {network['driver']} не гарантирует доступ контейнера к хосту (macvlan/ipvlan изолируют хост)")
            continue
        gateway = ipv4(attachment.get("Gateway"))
        if not gateway:
            reasons.append(f"{name}: IPv4 gateway отсутствует")
            continue
        address = ipaddress.IPv4Address(gateway)
        if address.is_global or address.is_loopback or address.is_unspecified or address.is_multicast or address.is_reserved:
            reasons.append(f"{name}: gateway не является допустимым локальным IPv4")
            continue
        configured = {ipv4(item.get("Gateway")) for item in network.get("ipam") or [] if item.get("Gateway")}
        if configured and gateway not in configured:
            reasons.append(f"{name}: gateway контейнера противоречит настройкам сети")
            continue
        interfaces = [a["interface"] for a in facts["addresses"] if a["address"] == gateway]
        if not interfaces:
            reasons.append(f"{name}: gateway {gateway} отсутствует на интерфейсах Linux-хоста")
            continue
        candidates.append({"address": gateway, "interface": interfaces[0], "network": name, "container": container["name"]})
    return candidates, reasons


def port_state(address, port):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind((address, int(port)))
        return "free"
    except OSError as error:
        if error.errno in (errno.EADDRINUSE, 10048):
            return "busy"
        if error.errno in (errno.EADDRNOTAVAIL, 10049):
            return "not_local"
        return "denied"
