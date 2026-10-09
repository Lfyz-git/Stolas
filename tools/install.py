"""Interactive Linux installer. Only the standard library is required on the host."""
import argparse
from contextlib import contextmanager
import copy
import datetime as dt
import getpass
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent.config import DEFAULT, GROUPS, load  # noqa: E402


def ask(label, default="", convert=str, secret=False):
    while True:
        suffix = " [Enter: сохранить/сгенерировать]" if secret else f" [{default}]" if default != "" else ""
        raw = (getpass.getpass if secret else input)(label + suffix + ": ")
        try:
            return convert(raw if raw else default)
        except (ValueError, TypeError, ZoneInfoNotFoundError):
            print("Некорректное значение, повторите ввод.")


def choice(label, values, default):
    def parse(value):
        if value.lower() not in values:
            raise ValueError()
        return value.lower()
    return ask(label + " (" + "/".join(values) + ")", default, parse)


def integer(low, high=None):
    def parse(value):
        result = int(value)
        if result < low or (high is not None and result > high):
            raise ValueError()
        return result
    return parse


def number(value):
    result = float(value)
    if not math.isfinite(result) or not 0 <= result <= 1000000:
        raise ValueError()
    return result


def matching(pattern):
    def parse(value):
        if not re.fullmatch(pattern, value):
            raise ValueError()
        return value
    return parse


def url(value):
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        raise ValueError()
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError()
    if any(c.isspace() for c in value):
        raise ValueError()
    if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("Use HTTPS outside loopback")
    return value.rstrip("/")


def cidrs(value):
    result = [v.strip() for v in value.split(",") if v.strip()]
    if not result:
        raise ValueError()
    for item in result:
        ipaddress.IPv4Network(item)
    return result


def ports(value):
    result = [integer(1, 65535)(v.strip()) for v in value.split(",")]
    if not 1 <= len(result) <= 9:
        raise ValueError()
    return result


def timezone(value):
    ZoneInfo(value)
    return value


def https_url(value):
    if not value.startswith("https://"):
        raise ValueError()
    return url(value)


def optional(value, convert):
    return None if value in ("", "-") else convert(value)


def clean_env():
    # Compose must use the files collected by the wizard, not shell overrides.
    return {k: v for k, v in os.environ.items() if not k.startswith(("STOLAS_", "COMPOSE_"))}


@contextmanager
def without_overrides():
    previous = {k: v for k, v in os.environ.items() if k.startswith("STOLAS_")}
    try:
        for key in previous:
            del os.environ[key]
        yield
    finally:
        os.environ.update(previous)


def validated(path):
    with without_overrides():
        return load(path)


def write_private(path, content):
    """Atomic replace, owner-only permissions, dated backup when changing a file."""
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise ValueError("Отказ записи через symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError(f"Отказ записи через symlink: {path}")
    if path.exists():
        backup = path.with_name(path.name + ".bak-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(3))
        with backup.open("x", encoding="utf-8", newline="\n") as file:
            os.chmod(backup, 0o600)
            file.write(path.read_text(encoding="utf-8"))
    fd, name = tempfile.mkstemp(prefix=".stolas-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as file:
            file.write(content)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def collect_config(existing):
    cfg = copy.deepcopy(existing)
    print("\nНастройки измерений. Enter оставляет предложенное значение.")
    cfg["node"] = ask("Имя узла", cfg["node"], matching(r"[\w.-]{1,64}"))
    fields = [
        ("seconds", "Секунд на каждое направление", 1, 60),
        ("parallel", "Параллельных потоков", 1, 16),
        ("connect_timeout", "Таймаут подключения, секунд", 1, 30),
        ("process_timeout", "Таймаут одного iperf3, секунд", 5, 120),
        ("cycle_timeout", "Общий таймаут цикла, секунд", 10, 600),
        ("attempts_per_server", "Попыток при ошибках измерения на сервер", 1, 9),
        ("busy_attempts_per_server", "Максимум попыток занятых портов на сервер", 1, 9),
        ("retry_delay", "Пауза между попытками, секунд", 0, 30),
        ("reserve_every", "Проверять резервы каждый N-й цикл", 1, 1000),
        ("min_interval", "Минимальная пауза между запусками, секунд (не расписание)", 0, 86400),
        ("history_limit", "Сколько циклов хранить", 1, 100000),
    ]
    for key, label, low, high in fields:
        cfg[key] = ask(label, cfg[key], integer(low, high))
    minimum = cfg["seconds"] + cfg["connect_timeout"] + 2
    while cfg["process_timeout"] < minimum:
        print(f"Таймаут одного iperf3 должен быть не меньше {minimum} секунд.")
        cfg["process_timeout"] = ask("Таймаут одного iperf3", minimum, integer(minimum, 120))
    cfg["bind_address"] = ask("Локальный IPv4 для iperf3 (-: автоматически)", cfg["bind_address"] or "", lambda v: optional(v, lambda ip: str(ipaddress.IPv4Address(ip))))
    print("\nWAN guard сравнивает внешний IPv4 с разрешёнными CIDR до/после тестов.")
    print("Для строгого запрета тестирования через LTE закрепите трафик на маршрутизаторе.")
    route = cfg["route"]
    route["mode"] = choice("Проверка основного WAN; off отключает также WAN-alert", ("required", "off"), route["mode"])
    route["public_ip_urls"] = ask("HTTPS источники внешнего IPv4 через запятую", ",".join(route["public_ip_urls"]), lambda value: list(dict.fromkeys(https_url(item.strip()) for item in value.split(","))))
    route["verification_timeout"] = ask("Общий бюджет одной проверки WAN, секунд", route["verification_timeout"], integer(2, 60))
    route["source_timeout"] = ask("Бюджет одного источника IPv4, секунд", route["source_timeout"], integer(1, 30))
    route["min_confirmations"] = ask("Минимум согласных ответов источников", min(route["min_confirmations"], len(route["public_ip_urls"])), integer(1, len(route["public_ip_urls"])))
    route["expected_public_cidrs"] = ask("Внешний IPv4 основного WAN /32 или CIDR через запятую" + (" (-: очистить)" if route["mode"] == "off" else ""), ",".join(route["expected_public_cidrs"]), lambda v: [] if route["mode"] == "off" and v in ("", "-") else cidrs(v))
    for key, label in (("interface", "Интерфейс Linux"), ("gateway", "Шлюз Linux")):
        route[key] = ask(label + " (-: не проверять)", route[key] or "", lambda v: optional(v, matching(r"[\w.:-]{1,64}")))
    print("\nГруппы серверов iperf3. В каждой можно задать любое количество серверов.")
    print("sequential — очередь с сохранением позиции; random — случайный порядок без повторов в цикле.")
    descriptions = {
        "primary": "Основные: обычный замер; сначала перебираются серверы этой группы",
        "additional": "Дополнительные: подтверждение низкой скорости или подмена недоступных основных",
        "emergency": "Аварийные: последняя подмена/подтверждение, если предыдущие группы не справились",
    }
    all_servers = []
    for name in GROUPS:
        print(descriptions[name])
        group = cfg["server_groups"][name]
        group["selection"] = choice("Выбор сервера в группе " + name, ("sequential", "random"), group["selection"])
        count = ask("Количество серверов в " + name + " (без верхнего ограничения)", len(group["servers"]), integer(1 if name == "primary" else 0))
        result = []
        for index in range(count):
            server = copy.deepcopy(group["servers"][index]) if index < len(group["servers"]) else dict(id=f"{name}-{index + 1}", host="", ports=[5201], min_download_mbps=500, min_upload_mbps=500)
            print(f"{name}: сервер {index + 1}")
            for key, label, pattern in (("id", "Идентификатор", r"[a-zA-Z0-9_-]{1,64}"), ("host", "Hostname или IPv4", r"[a-zA-Z0-9][a-zA-Z0-9.-]{0,252}")):
                while True:
                    server[key] = ask(label, server[key], matching(pattern))
                    if all(s[key].lower() != server[key].lower() for s in all_servers):
                        break
                    print("Серверы во всех группах должны иметь разные имена и адреса.")
            server["ports"] = ask("Порты через запятую (максимум 9)", ",".join(map(str, server["ports"])), ports)
            for key, label in (("min_download_mbps", "Минимальный download, Mbps"), ("min_upload_mbps", "Минимальный upload, Mbps")):
                server[key] = ask(label, server[key], number)
            result.append(server)
            all_servers.append(server)
        group["servers"] = result
    return cfg


def collect_api(root):
    previous = {}
    if (root / ".env").exists():
        for line in (root / ".env").read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep:
                previous[key] = value.strip().strip("'\"")
    print("\nHTTP API. По умолчанию доступен только на localhost.")
    listen = ask("Адрес прослушивания (IPv4)", previous.get("STOLAS_LISTEN", "127.0.0.1"), lambda v: str(ipaddress.IPv4Address(v)))
    port = ask("Порт API", previous.get("STOLAS_PORT", "8080"), integer(1, 65535))
    token = ask("API-токен: минимум 32 символа A–Z/a–z/0–9/_/-", previous.get("STOLAS_API_TOKEN") or secrets.token_urlsafe(32), matching(r"[A-Za-z0-9_-]{32,256}"), secret=True)
    if listen != "127.0.0.1":
        print("API будет слушать сеть. Обеспечьте TLS/VPN и ограничьте доступ firewall.")
    return {"STOLAS_API_TOKEN": token, "STOLAS_LISTEN": listen, "STOLAS_PORT": str(port)}


def read_api(root):
    values = {}
    for line in (root / ".env").read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and key in ("STOLAS_API_TOKEN", "STOLAS_LISTEN", "STOLAS_PORT"):
            values[key] = value.strip().strip("'\"")
    if set(values) != {"STOLAS_API_TOKEN", "STOLAS_LISTEN", "STOLAS_PORT"}:
        raise RuntimeError("Для обновления сначала завершите настройку .env через мастер")
    matching(r"[A-Za-z0-9_-]{32,256}")(values["STOLAS_API_TOKEN"])
    ipaddress.IPv4Address(values["STOLAS_LISTEN"])
    integer(1, 65535)(values["STOLAS_PORT"])
    return values


def private_address(value):
    address = ipaddress.IPv4Address(value)
    if address.is_global or address.is_loopback or address.is_unspecified or address.is_multicast or address.is_reserved:
        raise ValueError()
    return str(address)


def connection_url(value, topology):
    if topology in ("docker", "vpn"):
        parsed = urllib.parse.urlsplit(value)
        if parsed.scheme == "http" and parsed.hostname:
            address = ipaddress.IPv4Address(parsed.hostname)
            if not address.is_global and not address.is_loopback and not address.is_unspecified and not address.is_multicast and not address.is_reserved and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment and not any(c.isspace() for c in value):
                if parsed.port is not None and not 1 <= parsed.port <= 65535:
                    raise ValueError()
                return value.rstrip("/")
    result = url(value)
    if topology in ("lan", "proxy") and not result.startswith("https://"):
        raise ValueError()
    if topology == "docker" and urllib.parse.urlsplit(result).hostname in ("localhost", "127.0.0.1", "::1"):
        raise ValueError()
    return result


def collect_topology(api):
    print("native — n8n в ОС; docker — отдельный Docker-контейнер на этом хосте; lan — другая машина через HTTPS; vpn — доверенный VPN; proxy — HTTPS reverse proxy.")
    topology = choice("Схема подключения n8n", ("native", "docker", "lan", "vpn", "proxy"), "native")
    options = {"topology": topology}
    default = "http://127.0.0.1:" + api["STOLAS_PORT"] if topology == "native" else ""
    if topology == "docker":
        options["docker_container"] = ask("Имя локального контейнера n8n (для проверки сети; -: проверить вручную)", "-", lambda value: "" if value == "-" else matching(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*")(value))
        print("Укажите IPv4 Linux-хоста в Docker bridge-сети n8n. Его можно найти в Gateway сети контейнера через docker inspect; localhost контейнера не подходит.")
    if topology in ("docker", "vpn"):
        address = ask("IPv4 хоста Stolas в выбранной Docker/VPN сети (без 0.0.0.0)", "", private_address)
        api["STOLAS_LISTEN"] = address
        default = f"http://{address}:{api['STOLAS_PORT']}"
        print("API будет слушать только", address, "— ограничьте firewall адресами n8n/доверенного VPN.")
    if topology in ("lan", "proxy"):
        print("Нужен уже настроенный TLS reverse proxy на этом хосте. Stolas остаётся на localhost; чужие сервисы мастер не меняет.")
        print("Пример Caddy: your.domain { reverse_proxy 127.0.0.1:" + api["STOLAS_PORT"] + " }. DNS должен указывать на этот хост; разрешите доступ только n8n.")
        api["STOLAS_LISTEN"] = "127.0.0.1"
    options["endpoint"] = ask("URL Stolas, доступный из n8n", default, lambda value: connection_url(value, topology))
    return options


def collect_n8n(api=None):
    print("\nn8n: later — отложить, export — файл для ручного импорта, api — подключить существующий n8n.")
    mode = choice("Настроить n8n", ("later", "export", "api"), "later")
    if mode == "later":
        return {"mode": mode}
    options = {"mode": mode, **collect_topology(api or {"STOLAS_LISTEN": "127.0.0.1", "STOLAS_PORT": "8080"})}
    options["chat_id"] = ask("Telegram chat id", "", matching(r"-?\d+|@[a-zA-Z0-9_]{5,}"))
    options["hours"] = ask("Интервал расписания, часов", 3, integer(1, 23))
    options["timezone"] = ask("Часовой пояс IANA", "Europe/Moscow", timezone)
    options["notification_mode"] = choice("Telegram-уведомления", ("alerts_only", "every_measurement", "daily_summary"), "alerts_only")
    options["summary_hour"] = ask("Час ежедневной сводки", 9, integer(0, 23)) if options["notification_mode"] == "daily_summary" else 9
    if mode == "api":
        options["url"] = ask("Базовый URL существующего n8n (без /api/v1)", "", url)
        options["key"] = ask("n8n API key (Settings → n8n API)", "", matching(r"[^\s]+"), secret=True)
        options["bot_token"] = ask("Telegram bot token", "", matching(r"\d+:[A-Za-z0-9_-]+"), secret=True)
    return options


def workflow(root, options):
    data = json.loads((root / "n8n/stolas.json").read_text(encoding="utf-8"))
    data["name"] = f"Stolas - {options['hours']}h monitoring"
    data["settings"]["timezone"] = options["timezone"]
    nodes = {n["name"]: n for n in data["nodes"]}
    nodes["Every 3 hours"]["parameters"]["rule"]["interval"][0]["hoursInterval"] = options["hours"]
    # JSON encoding avoids injecting user input into the JavaScript Code node.
    settings_code = "return [{json: " + json.dumps({"endpoint": options["endpoint"], "chatId": options["chat_id"], "notificationMode": options.get("notification_mode", "alerts_only")}, ensure_ascii=False) + "}];"
    for name in ("Settings", "Summary settings"):
        nodes[name]["parameters"]["jsCode"] = settings_code
    nodes["Daily summary"]["disabled"] = options.get("notification_mode") != "daily_summary"
    nodes["Daily summary"]["parameters"]["rule"]["interval"][0]["triggerAtHour"] = options.get("summary_hour", 9)
    return data


def run(args, root, capture=False, check=True):
    result = subprocess.run(args, cwd=root, env=clean_env(), text=True, encoding="utf-8", capture_output=capture)
    if check and result.returncode:
        raise RuntimeError("Команда завершилась ошибкой: " + " ".join(map(str, args[:4])))
    return result


def docker_command(root):
    command = ["docker"]
    if not shutil.which("docker"):
        print("Docker отсутствует. Будут установлены Docker Engine и Compose из официального apt-репозитория.")
        if choice("Установить зависимости (Ubuntu/Debian)", ("yes", "no"), "yes") == "no":
            raise RuntimeError("Установите Docker Engine и Compose v2 и повторите запуск.")
        prefix = [] if os.geteuid() == 0 else ["sudo"]
        run(prefix + ["sh", str(root / "tools/install-docker.sh")], root)
    if run(command + ["info"], root, capture=True, check=False).returncode:
        if os.geteuid() != 0 and shutil.which("sudo"):
            command = ["sudo", "docker"]
        if run(command + ["info"], root, capture=True, check=False).returncode:
            raise RuntimeError("Docker Engine недоступен. Запустите службу Docker и проверьте права доступа.")
    run(command + ["compose", "version"], root)
    context = run(command + ["context", "inspect", "--format", "{{.Endpoints.docker.Host}}"], root, capture=True).stdout.strip()
    if not context.startswith("unix://") or os.environ.get("DOCKER_HOST", "unix://").startswith(("tcp:", "ssh:")):
        raise RuntimeError("Нужен локальный Docker Engine через Unix socket на целевом Linux-хосте.")
    arch = run(command + ["info", "--format", "{{.Architecture}}"], root, capture=True).stdout.strip()
    if arch not in ("x86_64", "amd64", "aarch64", "arm64"):
        raise RuntimeError("Production-образ поддерживает только amd64/arm64.")
    env_path = root / ".env"
    env_text = env_path.read_text(encoding="utf-8")
    if not any(line.startswith("STOLAS_PROJECT_NAME=") for line in env_text.splitlines()):
        owned = run(command + ["ps", "-a", "--filter", "label=com.docker.compose.project.working_dir=" + str(root), "--format", '{{.Label "com.docker.compose.project"}}'], root, capture=True).stdout.split()
        names = set(owned)
        if len(names) > 1:
            raise RuntimeError("Для каталога найдено несколько Compose проектов; проверьте их вручную")
        project = next(iter(names)) if names else "stolas-" + hashlib.sha256(str(root).encode()).hexdigest()[:10]
        matching(r"[a-z0-9][a-z0-9_-]*")(project)
        write_private(env_path, env_text.rstrip() + "\nSTOLAS_PROJECT_NAME=" + project + "\n")
    project = next(line.partition("=")[2].strip() for line in env_path.read_text(encoding="utf-8").splitlines() if line.startswith("STOLAS_PROJECT_NAME="))
    matching(r"[a-z0-9][a-z0-9_-]*")(project)
    owners = run(command + ["ps", "-a", "--filter", "label=com.docker.compose.project=" + project,
                            "--format", '{{.Label "com.docker.compose.project.working_dir"}}'], root, capture=True).stdout.splitlines()
    if any(Path(owner).resolve() != root.resolve() for owner in owners):
        raise RuntimeError("Имя Compose проекта занято другим каталогом; чужие контейнеры не изменены")
    return command + ["compose", "--project-directory", str(root), "--env-file", str(root / ".env"), "-f", str(root / "compose.yaml")]


def first_test(compose, root):
    # Single cycle, same persistent volume and configuration as the API.
    print("\nПервичный тест скорости через CLI (может занять несколько минут)…", flush=True)
    result = run(compose + ["exec", "-T", "stolas", "python3", "-m", "agent", "run"], root, capture=True, check=False)
    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
    if result.returncode not in (0, 2):
        raise RuntimeError("CLI-тест не запущен: проверьте конфигурацию, блокировку и cooldown; повтор: docker compose exec stolas python3 -m agent run")
    data = json.loads(result.stdout)
    print("Статус первого цикла:", data["status"])
    for name in ("primary", "confirmation"):
        sample = data.get(name)
        if sample:
            print(f"  {sample['server']} [{sample.get('group', 'legacy')}]: DL {sample['download']['mbps']} / UL {sample['upload']['mbps']} Mbps")
    return data


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Do not forward API keys or bearer tokens across redirects.
        return None


def request_json(base, path, token, body=None, n8n=False):
    headers = {"X-N8N-API-KEY" if n8n else "Authorization": token if n8n else "Bearer " + token}
    payload = None if body is None else json.dumps(body).encode("utf-8")
    if payload is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=payload, headers=headers)
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=20) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"HTTP {error.code} при обращении к {path}. Проверьте URL, API key и права доступа.") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise RuntimeError("Сервис недоступен; проверьте URL, TLS и сетевой доступ.") from None


def check_connection(root, options, api, compose):
    container = options.get("docker_container")
    if options.get("topology") == "docker" and container:
        code = "let s='';process.stdin.on('data',x=>s+=x);process.stdin.on('end',async()=>{try{const p=JSON.parse(s);const r=await fetch(p.url+'/healthz',{headers:{Authorization:'Bearer '+p.token},redirect:'error',signal:AbortSignal.timeout(8000)});const j=await r.json();process.exit(r.status===200&&j.status==='ready'?0:1)}catch{process.exit(1)}})"
        docker = compose[:compose.index("compose")]
        try:
            result = subprocess.run(docker + ["exec", "-i", container, "node", "-e", code], cwd=root, env=clean_env(),
                                    input=json.dumps({"url": options["endpoint"], "token": api["STOLAS_API_TOKEN"]}),
                                    text=True, capture_output=True, timeout=15)
        except subprocess.TimeoutExpired:
            raise RuntimeError("Проверка сети n8n превысила 15 секунд; проверьте Docker и доступность endpoint") from None
        if result.returncode:
            raise RuntimeError("n8n Docker не достигает /healthz. Проверьте IP bridge, адрес STOLAS_LISTEN и firewall; контейнер n8n не изменён")
        print("Доступ к API проверен из network namespace контейнера n8n.")
    else:
        request_json(options["endpoint"], "/healthz", api["STOLAS_API_TOKEN"])
        print("/healthz доступен с установочного хоста. Из удалённого n8n/VPN или task runner проверьте Manual test; это отдельное сетевое окружение.")


def connect_n8n(root, options, api, data):
    local = root / "n8n/local.json"
    write_private(local, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    print(f"Workflow для импорта: {local} (неактивен, без секретов).")
    if options["mode"] == "export":
        print("Импортируйте файл в n8n; выберите Header Auth (Authorization: Bearer <токен из .env>) и Telegram credentials.")
        print("Проверьте Manual test и затем включите расписание.")
        return
    print("Проверка API Stolas с этого хоста; доступность ИЗ n8n нужно проверить Manual test.")
    request_json(options["endpoint"], "/healthz", api["STOLAS_API_TOKEN"])
    base = options["url"] + "/api/v1"
    for kind, fields in (("httpHeaderAuth", ("name", "value")), ("telegramApi", ("accessToken",))):
        schema = request_json(base, "/credentials/schema/" + kind, options["key"], n8n=True)
        if any(field not in schema.get("properties", {}) for field in fields):
            raise RuntimeError("n8n API вернул несовместимую схему credentials. Используйте режим export; существующие credentials не изменены")
    # Save created resource IDs after each mutation. Do not automatically retry creates.
    state_path = root / "n8n/install-state.json"
    if state_path.exists():
        raise RuntimeError("n8n/install-state.json уже существует. Проверьте ранее созданные ресурсы в n8n; для ручной настройки используйте export. Автоматическое создание дубликатов остановлено.")
    state = {"url": options["url"], "credentials": []}
    write_private(state_path, json.dumps(state, indent=2) + "\n")
    credentials = [
        ("httpHeaderAuth", "Stolas API", {"name": "Authorization", "value": "Bearer " + api["STOLAS_API_TOKEN"]}),
        ("telegramApi", "Stolas Telegram", {"accessToken": options["bot_token"], "baseUrl": "https://api.telegram.org"}),
    ]
    for kind, name, values in credentials:
        created = request_json(base, "/credentials", options["key"], {"name": name, "type": kind, "data": values}, n8n=True)
        ref = {"id": str(created["id"]), "name": name}
        state["credentials"].append({"type": kind, **ref})
        write_private(state_path, json.dumps(state, indent=2) + "\n")
        for target in (("Run Stolas", "Read summary") if kind == "httpHeaderAuth" else ("Telegram alert",)):
            next(n for n in data["nodes"] if n["name"] == target)["credentials"] = {kind: ref}
    payload = {k: data[k] for k in ("name", "nodes", "connections", "settings")}
    created = request_json(base, "/workflows", options["key"], payload, n8n=True)
    state["workflow_id"] = str(created["id"])
    write_private(state_path, json.dumps(state, indent=2) + "\n")
    print("Создан НЕАКТИВНЫЙ workflow:", options["url"] + "/workflow/" + urllib.parse.quote(state["workflow_id"], safe=""))
    print("В n8n выполните Manual test и проверьте credentials, доступ к Stolas и Telegram.")
    print("После проверки включите расписание в n8n. API key и bot token на диске не сохраняются.")


def install(root=ROOT, configure_only=False, reuse=False):
    current = root / "config/local.json"
    existing = validated(current) if current.exists() else copy.deepcopy(DEFAULT)
    cfg = existing if reuse else collect_config(existing)
    api = read_api(root) if reuse else collect_api(root)
    options = {"mode": "later"} if reuse else collect_n8n(api)
    generated = workflow(root, options) if options["mode"] != "later" else None
    # Validate before touching existing configuration or installing anything.
    with tempfile.TemporaryDirectory() as directory:
        check_path = Path(directory) / "config.json"
        check_path.write_text(json.dumps(cfg), encoding="utf-8")
        validated(check_path)
    if not reuse:
        write_private(current, json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")
        os.chmod(current, 0o644)
        previous_env = (root / ".env").read_text(encoding="utf-8") if (root / ".env").exists() else ""
        project_lines = [line for line in previous_env.splitlines() if line.startswith("STOLAS_PROJECT_NAME=")]
        write_private(root / ".env", "".join(k + "=" + v + "\n" for k, v in api.items()) + "".join(line + "\n" for line in project_lines))
    if options["mode"] != "later":
        write_private(root / "n8n/settings.json", json.dumps({k: v for k, v in options.items() if k not in ("key", "bot_token")}, ensure_ascii=False, indent=2) + "\n")
    print("\nНастройки сохранены. Старые версии файлов сохранены рядом как .bak-*.")
    if configure_only:
        if generated:
            write_private(root / "n8n/local.json", json.dumps(generated, ensure_ascii=False, indent=2) + "\n")
        print("Только конфигурация: установка, сеть, n8n API и тест скорости не запускались.")
        return 0
    compose = docker_command(root)
    run(compose + ["config", "--quiet"], root)
    run(compose + ["build", "stolas"], root)
    run(compose + ["run", "--rm", "--no-deps", "stolas", "validate"], root)
    run(compose + ["up", "-d", "--force-recreate", "--wait", "--wait-timeout", "90", "stolas"], root)
    if reuse:
        host = api["STOLAS_LISTEN"] if api["STOLAS_LISTEN"] != "0.0.0.0" else "127.0.0.1"
        request_json(f"http://{host}:{api['STOLAS_PORT']}", "/healthz", api["STOLAS_API_TOKEN"])
        result = None
        print("Сервис обновлён; конфигурация, история и n8n сохранены. Дополнительный нагрузочный тест не запускался.")
    else:
        result = first_test(compose, root)
    if generated:
        check_connection(root, options, api, compose)
        connect_n8n(root, options, api, generated)
    elif not reuse:
        print("n8n отложен. Выполнен один CLI-цикл; автоматическое расписание не включено.")
    print("API Stolas запущен. История: docker compose exec stolas python3 -m agent history")
    if result and result["status"] in ("route_blocked", "unavailable"):
        print("Установка завершена, но измерение не получено. Проверьте ошибки в JSON выше.")
        return 2
    return 0


def main():
    parser = argparse.ArgumentParser(description="Интерактивная установка Stolas на Linux")
    parser.add_argument("--configure-only", action="store_true", help="только записать настройки без установки и тестирования")
    parser.add_argument("--reuse-config", action="store_true", help="сохранить настройки и существующий n8n при обновлении")
    args = parser.parse_args()
    if not sys.platform.startswith("linux"):
        parser.error("Запускайте установщик на целевом Linux-хосте.")
    if sys.version_info < (3, 10):
        parser.error("Для установщика требуется Python 3.10+.")
    try:
        return install(configure_only=args.configure_only, reuse=args.reuse_config)
    except (KeyboardInterrupt, EOFError):
        print("\nУстановка прервана. Сохранённые файлы и запущенный сервис остаются на месте.", file=sys.stderr)
        return 1
    except (ValueError, RuntimeError, OSError, KeyError) as error:
        print("\nОшибка установки:", str(error), file=sys.stderr)
        print("Исправьте причину и повторите запуск. Не публикуйте .env и его резервные копии.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
