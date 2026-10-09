"""Interactive Linux installer. Only the standard library is required on the host."""
import sys
if __name__ == "__main__":
    sys.dont_write_bytecode = True
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
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent.config import DEFAULT, GROUPS, load  # noqa: E402
from tools import environment  # noqa: E402


class Back(Exception):
    pass


class Cancel(Exception):
    pass


class Rescan(Exception):
    pass


def ask(label, default="", convert=str, secret=False):
    while True:
        suffix = " [Enter: сохранить/сгенерировать]" if secret else f" [{default}]" if default != "" else ""
        raw = (getpass.getpass if secret else input)(label + suffix + ": ")
        if raw.strip().lower() == ":back":
            raise Back()
        if raw.strip().lower() == ":cancel":
            raise Cancel()
        if raw.strip().lower() == ":rescan":
            raise Rescan()
        try:
            return convert(raw if raw else default)
        except (ValueError, TypeError, ZoneInfoNotFoundError) as error:
            explanation = str(error) if not secret else getattr(convert, "description", "проверьте формат секрета; введённое значение скрыто")
            print(label + ": " + (explanation or "проверьте формат и допустимые значения") + ". Повторите ввод или :back / :cancel.")


def choice(label, values, default):
    def parse(value):
        if value.isdigit() and 1 <= int(value) <= len(values):
            return values[int(value) - 1]
        if value.lower() not in values:
            raise ValueError("выберите " + ", ".join(values))
        return value.lower()
    return ask(label + " (" + "/".join(f"{i + 1}={v}" for i, v in enumerate(values)) + ")", default, parse)


def integer(low, high=None):
    def parse(value):
        try:
            result = int(value)
        except (TypeError, ValueError):
            raise ValueError("нужно целое число") from None
        if result < low or (high is not None and result > high):
            raise ValueError(f"допустимо от {low}" + (f" до {high}" if high is not None else " и выше"))
        return result
    return parse


def number(value):
    result = float(value)
    if not math.isfinite(result) or not 0 <= result <= 1000000:
        raise ValueError("нужно конечное число от 0 до 1000000")
    return result


def matching(pattern):
    descriptions = {
        r"[A-Za-z0-9_-]{32,256}": "нужно 32–256 символов: латинские буквы, цифры, _ или -",
        r"[^\s]+": "нужно непустое значение без пробелов",
        r"\d+:[A-Za-z0-9_-]+": "нужен токен бота в формате число:секрет",
        r"-?\d+|@[a-zA-Z0-9_]{5,}": "нужен числовой Telegram chat id (для группы возможен минус) или @имя канала",
        r"[a-zA-Z0-9_-]{1,64}": "идентификатор: 1–64 латинских букв, цифр, _ или -",
        r"[a-zA-Z0-9][a-zA-Z0-9.-]{0,252}": "нужно имя хоста или IPv4, без схемы URL и номера порта",
        r"[\w.-]{1,64}": "имя узла: 1–64 букв, цифр, точек, _ или -",
    }
    def parse(value):
        if not re.fullmatch(pattern, value):
            raise ValueError(descriptions.get(pattern, "значение не соответствует указанному формату"))
        return value
    parse.description = descriptions.get(pattern, "проверьте формат секрета; введённое значение скрыто")
    return parse


def url(value):
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("нужен URL с http/https и именем хоста, без логина, query и fragment")
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError()
    if any(c.isspace() for c in value):
        raise ValueError()
    if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("для удалённого хоста нужен HTTPS; HTTP разрешён только для локального доступа/доверенного VPN")
    return value.rstrip("/")


def cidrs(value):
    result = [v.strip() for v in value.split(",") if v.strip()]
    if not result:
        raise ValueError("укажите внешний IPv4 основного WAN, например 203.0.113.10/32; список не может быть пустым")
    for item in result:
        try:
            ipaddress.IPv4Network(item)
        except ValueError:
            raise ValueError("нужен IPv4/CIDR, например 203.0.113.10/32; у диапазона укажите адрес сети без host bits") from None
    return result


def ports(value):
    result = [integer(1, 65535)(v.strip()) for v in value.split(",")]
    if not 1 <= len(result) <= 9:
        raise ValueError()
    return result


def timezone(value):
    if value == "Etc/UTC":
        return value
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError:
        raise ValueError("неизвестный IANA timezone или отсутствует пакет tzdata (Ubuntu: sudo apt-get install tzdata)") from None
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


def select_named(label, items, names, default=0, automatic=True):
    if len(items) == 1 and automatic:
        print(label + ": " + names[0] + " (обнаружено автоматически)")
        return items[0]
    for index, name in enumerate(names, 1):
        print(f"  {index}. {name}")
    index = ask(label + " — номер варианта", default + 1, integer(1, len(items)))
    return items[index - 1]


def propose_port(api, facts, root):
    address, initial = api["STOLAS_LISTEN"], int(api["STOLAS_PORT"])
    for port in range(initial, min(65536, initial + 100)):
        state = environment.port_state(address, port)
        if state == "free":
            if port != initial:
                print(f"Порт {initial} занят. Предлагается свободный порт {port}.")
            api["STOLAS_PORT"] = str(port)
            return
        if state == "busy" and port == initial and facts["stolas"] and (root / ".env").exists():
            try:
                old = read_api(root)
                if old["STOLAS_PORT"] == str(port) and old["STOLAS_LISTEN"] == address:
                    reply = request_json(f"http://{address}:{port}", "/healthz", old["STOLAS_API_TOKEN"])
                    if reply.get("status") == "ready":
                        print(f"Порт {port} принадлежит существующему Stolas; будет использован повторно.")
                        return
            except (ValueError, RuntimeError):
                pass
        if state == "not_local":
            raise ValueError(f"Адрес {address} отсутствует на Linux-хосте. Повторите обнаружение или выберите другую схему подключения")
        if state == "denied":
            raise ValueError(f"ОС не разрешает привязку к {address}:{port}; выберите непривилегированный порт в разделе api")
    raise ValueError(f"Нет свободного порта в диапазоне {initial}–{min(65535, initial + 99)}; измените порт в разделе api")


def collect_topology(api, facts=None, root=ROOT, previous=None):
    facts = facts if facts is not None else environment.discover(root)
    previous = previous or {}
    print("docker — обнаруженный n8n на этом хосте; native — n8n в ОС; lan/proxy — удалённый HTTPS; vpn — доверенный VPN.")
    default = previous.get("topology", "docker" if facts["n8n"] else "")
    topology = choice("Где работает n8n", ("docker", "native", "lan", "vpn", "proxy"), default)
    options = {"topology": topology}
    if topology == "docker":
        if not facts["docker"]["available"]:
            raise ValueError(facts["docker"]["reason"] + ". Исправьте доступ и повторите обнаружение (rescan), либо выберите другую схему")
        if not facts["n8n"]:
            raise ValueError("Запущенный n8n не обнаружен по образу/Compose метаданным. Запустите его и выберите rescan, либо выберите удалённое подключение")
        saved = next((c for c in facts["n8n"] if c["name"] == previous.get("docker_container")), None)
        container = saved or select_named("Экземпляр n8n", facts["n8n"],
            [f"{c['name']} — {c['image']}, проект {c.get('project') or 'без Compose'}" for c in facts["n8n"]],
            automatic=len(facts["n8n"]) == 1 and facts["n8n"][0]["confidence"] == "image")
        candidates, reasons = environment.host_candidates(container, facts)
        if not candidates:
            raise ValueError("Не найден подтверждённый адрес хоста для n8n: " + "; ".join(reasons or ["контейнер не подключён к подходящей сети"]) + ". Используйте HTTPS proxy/VPN; сети n8n не изменены")
        saved_network = next((c for c in candidates if c["network"] == previous.get("network") and c["address"] == api["STOLAS_LISTEN"]), None)
        selected = saved_network or select_named("Сеть для связи n8n со Stolas", candidates,
                    [f"{c['network']} — интерфейс {c['interface']} ({c['address']})" for c in candidates])
        options.update(docker_container=container["name"], docker_id=container["id"], network=selected["network"], network_mode=container["mode"])
        api["STOLAS_LISTEN"] = selected["address"]
    elif topology == "vpn":
        candidates = [item for item in facts["addresses"] if not ipaddress.IPv4Address(item["address"]).is_loopback and not ipaddress.IPv4Address(item["address"]).is_global]
        if not candidates:
            raise ValueError("IPv4 интерфейсов не обнаружены. Сначала настройте доверенный VPN и установите iproute2; мастер не создаёт туннель")
        print("Выберите интерфейс заранее настроенного доверенного VPN. Наличие адреса само по себе не доказывает, что сеть доверенная.")
        selected = select_named("Интерфейс VPN", candidates, [f"{item['interface']} ({item['address']})" for item in candidates], automatic=False)
        api["STOLAS_LISTEN"] = selected["address"]
    else:
        api["STOLAS_LISTEN"] = "127.0.0.1"
    propose_port(api, facts, root)
    if topology in ("lan", "proxy"):
        print("Нужен ваш уже настроенный HTTPS reverse proxy. Stolas будет доступен proxy на 127.0.0.1:" + api["STOLAS_PORT"] + ".")
        print("Caddy: your.domain { reverse_proxy 127.0.0.1:" + api["STOLAS_PORT"] + " }. Настройте DNS/сертификат и доступ только от n8n; мастер не меняет proxy/firewall.")
        options["endpoint"] = ask("Ваш HTTPS адрес Stolas на reverse proxy", previous.get("endpoint", ""), https_url)
    else:
        options["endpoint"] = f"http://{api['STOLAS_LISTEN']}:{api['STOLAS_PORT']}"
        connection_url(options["endpoint"], "native" if options.get("network_mode") == "host" else topology)
        print("Адрес Stolas для n8n сформирован автоматически:", options["endpoint"])
    return options


def collect_n8n(api, facts=None, root=ROOT, previous=None, checkpoint=None):
    facts = facts if facts is not None else environment.discover(root)
    previous = previous or {}
    existing_state = (root / "n8n/install-state.json").exists()
    modes = ("later", "export", "api", "keep") if previous.get("endpoint") else ("later", "export", "api")
    print("n8n: later — отложить; export — подготовить импорт без ключей; api — подключить через API существующего n8n." + (" keep — сохранить существующую интеграцию." if "keep" in modes else ""))
    default = "keep" if existing_state and "keep" in modes else previous.get("mode", "later")
    resuming = previous.get("_incomplete", False)
    mode = previous["mode"] if resuming else choice("Интеграция n8n", modes, default if default in modes else "later")
    if mode == "later":
        return {"mode": mode}
    if mode == "keep":
        options = copy.deepcopy(previous)
        options["mode"] = "keep"
        return options
    options = {"mode": mode, **collect_topology(api, facts, root, previous)}
    options["chat_id"] = previous["chat_id"] if resuming and previous.get("chat_id") else ask("Telegram chat id", previous.get("chat_id", ""), matching(r"-?\d+|@[a-zA-Z0-9_]{5,}"))
    options["hours"] = previous.get("hours", 3)
    options["timezone"] = previous.get("timezone", facts.get("timezone") or "Etc/UTC")
    options["notification_mode"] = choice("Telegram: только проблемы / каждый замер / сводка за сутки", ("alerts_only", "every_measurement", "daily_summary"), previous.get("notification_mode", "alerts_only"))
    options["summary_hour"] = ask("Час ежедневной сводки", previous.get("summary_hour", 9), integer(0, 23)) if options["notification_mode"] == "daily_summary" else 9
    if mode == "api":
        for field, label, converter, secret in (("url", "Адрес существующего n8n (без /api/v1)", url, False),
                ("key", "n8n API key", matching(r"[^\s]+"), True),
                ("bot_token", "Telegram bot token", matching(r"\d+:[A-Za-z0-9_-]+"), True)):
            options[field] = previous[field] if resuming and previous.get(field) else ask(label, previous.get(field, ""), converter, secret=secret)
            if checkpoint:
                checkpoint({**options, "_incomplete": True})
    return options


def save_draft(root, plan):
    path = root / ".stolas-draft.json"
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise ValueError("Черновик не записывается через symlink")
    fd, name = tempfile.mkstemp(prefix=".stolas-draft-", dir=root)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(plan, file, ensure_ascii=False)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def validate_plan(plan):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "config.json"
        path.write_text(json.dumps(plan["config"]), encoding="utf-8")
        validated(path)
    api = plan["api"]
    matching(r"[A-Za-z0-9_-]{32,256}")(api["STOLAS_API_TOKEN"])
    ipaddress.IPv4Address(api["STOLAS_LISTEN"])
    integer(1, 65535)(api["STOLAS_PORT"])
    options = plan["n8n"]
    if options["mode"] != "later":
        connection_url(options["endpoint"], "native" if options.get("network_mode") == "host" else options["topology"])
        matching(r"-?\d+|@[a-zA-Z0-9_]{5,}")(options["chat_id"])
        integer(1, 23)(options["hours"])
        timezone(options["timezone"])
        if options["notification_mode"] not in ("alerts_only", "every_measurement", "daily_summary"):
            raise ValueError("Неизвестная политика Telegram")
        if options["mode"] == "api" and not options.get("_incomplete"):
            url(options["url"])
            matching(r"[^\s]+")(options["key"])
            matching(r"\d+:[A-Za-z0-9_-]+")(options["bot_token"])


def validate_discovered_endpoint(plan, facts):
    options = plan["n8n"]
    if options["mode"] == "later" or options.get("topology") != "docker":
        return
    if not facts["docker"]["available"]:
        raise ValueError(facts["docker"]["reason"] + "; повторите rescan")
    selected = next((c for c in facts["n8n"] if c["name"] == options.get("docker_container")), None)
    if selected is None:
        raise ValueError("Сохранённый контейнер n8n больше не обнаружен; выберите раздел n8n или rescan")
    choices, reasons = environment.host_candidates(selected, facts)
    host = urllib.parse.urlsplit(options["endpoint"]).hostname
    if not any(c["address"] == host for c in choices):
        raise ValueError("Сохранённый endpoint не соответствует обнаруженным сетям n8n; выберите раздел n8n. " + "; ".join(reasons))
    if host != plan["api"]["STOLAS_LISTEN"] or urllib.parse.urlsplit(options["endpoint"]).port != int(plan["api"]["STOLAS_PORT"]):
        raise ValueError("Endpoint n8n отличается от адреса/порта Stolas; обновите интеграцию в разделе n8n")
    options["docker_id"] = selected["id"]
    options["network_mode"] = selected["mode"]


def print_facts(facts):
    print("\nОбнаружение окружения (только чтение)")
    print("Каталог:", facts["installation"]["directory"])
    print("Docker:", facts["docker"].get("version") or facts["docker"]["reason"])
    print("n8n в Docker:", ", ".join(c["name"] + " (" + c["image"] + ")" for c in facts["n8n"]) or "запущенные экземпляры не найдены")
    state = facts["installation"]
    print("Stolas:", "сохранённые настройки" if state["config"] else "новая/незавершённая настройка", "; версия:", state.get("ref", "checkout/неизвестна"), "; этап:", state.get("stage", "нет"))
    if state.get("wizard_stage"):
        print("Последний завершённый этап:", state["wizard_stage"])
    for warning in facts["warnings"]:
        print("Диагностика:", warning)


def print_plan(plan, facts, reuse=False):
    cfg, api, options = plan["config"], plan["api"], plan["n8n"]
    print("\nПредлагаемая конфигурация — секреты скрыты")
    print(f"Узел: {cfg['node']}; измерение: IPv4 TCP, {cfg['parallel']} потоков × {cfg['seconds']} сек. на направление")
    print("Серверы:", "; ".join(f"{name}: {len(cfg['server_groups'][name]['servers'])} ({cfg['server_groups'][name]['selection']})" for name in GROUPS))
    print("WAN:", cfg["route"]["mode"], "; разрешённые CIDR:", ",".join(cfg["route"]["expected_public_cidrs"]) or "не заданы")
    print("API:", api["STOLAS_LISTEN"] + ":" + api["STOLAS_PORT"], "; Bearer: сохранён/сгенерирован, не отображается")
    print("n8n:", options["mode"], "; endpoint:", options.get("endpoint", "не настраивается"))
    if options["mode"] != "later":
        print("Telegram:", options["notification_mode"], "; интервал:", options["hours"], "ч.; timezone:", options["timezone"])
    print("После подтверждения:", "пересборка Stolas без изменения настроек/нагрузочного теста" if reuse else "сохранение настроек, запуск Stolas, один CLI-тест; workflow останется неактивным")
    if not facts["docker"]["available"]:
        print("Docker потребуется установить/восстановить доступ:", facts["docker"]["reason"])
    print("До подтверждения сохраняется только приватный черновик 0600; сервисы и действующая конфигурация не меняются.")


def collect_plan(root, existing, facts, reuse=False):
    api = read_api(root) if (root / ".env").exists() else {"STOLAS_API_TOKEN": secrets.token_urlsafe(32), "STOLAS_LISTEN": "127.0.0.1", "STOLAS_PORT": "8080"}
    options = {"mode": "later"}
    settings = root / "n8n/settings.json"
    if settings.is_file():
        options = json.loads(settings.read_text(encoding="utf-8"))
        options["mode"] = "keep"
    plan = {"config": existing, "api": api, "n8n": options, "completed": []}
    draft = root / ".stolas-draft.json"
    if draft.exists() and not reuse:
        if draft.is_symlink():
            raise ValueError("Черновик является symlink; чтение остановлено")
        saved = json.loads(draft.read_text(encoding="utf-8"))
        validate_plan(saved)
        print("Найден приватный черновик: завершённые шаги —", ", ".join(saved.get("completed", [])) or "нет")
        if choice("Продолжить черновик или использовать сохранённую конфигурацию", ("resume", "saved"), "resume") == "resume":
            plan = saved
    elif not (root / "config/local.json").exists():
        plan["config"]["node"] = re.sub(r"[^\w.-]", "-", facts["hostname"])[:64] or "stolas-node"
    print("Enter принимает предложенное. :back — назад; :rescan — повторить обнаружение; :cancel — отменить. Ctrl+C сохранит черновик.")
    configured = (root / "config/local.json").exists() and (root / ".env").exists() and not draft.exists()
    stages = [] if reuse or configured else [stage for stage in ("wan", "n8n") if stage not in plan["completed"]]
    position = 0
    while True:
        section = stages[position] if position < len(stages) else None
        try:
            if section is None:
                if plan["n8n"]["mode"] not in ("later", "keep") and plan["n8n"].get("topology") not in ("lan", "proxy"):
                    plan["n8n"]["endpoint"] = f"http://{plan['api']['STOLAS_LISTEN']}:{plan['api']['STOLAS_PORT']}"
                print_plan(plan, facts, reuse)
                values = ("apply", "cancel") if reuse else ("apply", "wan", "measurements", "api", "n8n", "schedule", "rescan", "cancel")
                section = choice("Применить или исправить раздел", values, "apply")
                if section == "cancel":
                    raise Cancel()
                if section == "apply":
                    if plan["config"]["route"]["mode"] == "required" and not plan["config"]["route"]["expected_public_cidrs"]:
                        raise ValueError("Основной WAN не задан. Выберите раздел wan и введите разрешённый IPv4/CIDR; проверка не отключается автоматически")
                    validate_plan(plan)
                    before = copy.deepcopy(plan["api"])
                    propose_port(plan["api"], facts, root)
                    if reuse and plan["api"] != before:
                        plan["api"] = before
                        raise RuntimeError("Порт сохранённой установки занят другим сервисом. Отмените обновление и выберите повторную настройку")
                    if plan["n8n"]["mode"] not in ("later", "keep") and plan["n8n"].get("topology") not in ("lan", "proxy"):
                        plan["n8n"]["endpoint"] = f"http://{plan['api']['STOLAS_LISTEN']}:{plan['api']['STOLAS_PORT']}"
                    if plan["api"] != before:
                        print("Порт изменился после обнаружения; проверьте сводку ещё раз.")
                        continue
                    validate_discovered_endpoint(plan, facts)
                    save_draft(root, plan)
                    return plan
            if section == "rescan":
                facts.clear()
                facts.update(environment.discover(root))
                print_facts(facts)
                continue
            candidate = copy.deepcopy(plan)
            if section == "wan":
                route = candidate["config"]["route"]
                print("Укажите разрешённый внешний IPv4/CIDR ОСНОВНОГО WAN. Текущий внешний адрес сам по себе не доказывает, что это не LTE.")
                route["mode"] = choice("WAN: required — проверять; off — осознанно отключить", ("required", "off"), route["mode"])
                if route["mode"] == "required":
                    route["expected_public_cidrs"] = ask("Разрешённый основной WAN: IPv4/32 или CIDR через запятую", ",".join(route["expected_public_cidrs"]), cidrs)
            elif section == "n8n":
                def partial(options):
                    candidate["n8n"] = options
                    plan["api"] = copy.deepcopy(candidate["api"])
                    plan["n8n"] = copy.deepcopy(options)
                    save_draft(root, candidate)
                candidate["n8n"] = collect_n8n(candidate["api"], facts, root, candidate["n8n"], checkpoint=partial)
                propose_port(candidate["api"], facts, root)
            elif section == "measurements":
                candidate["config"] = collect_config(candidate["config"])
            elif section == "api":
                print("Для Docker/n8n адрес определяется обнаружением. Меняйте здесь порт и токен; после смены токена обновите credential n8n.")
                candidate["api"]["STOLAS_PORT"] = str(ask("Порт Stolas", candidate["api"]["STOLAS_PORT"], integer(1024, 65535)))
                candidate["api"]["STOLAS_API_TOKEN"] = ask("API-токен (Enter сохраняет)", candidate["api"]["STOLAS_API_TOKEN"], matching(r"[A-Za-z0-9_-]{32,256}"), secret=True)
                propose_port(candidate["api"], facts, root)
                if candidate["n8n"]["mode"] not in ("later", "keep") and candidate["n8n"].get("topology") not in ("lan", "proxy"):
                    candidate["n8n"]["endpoint"] = f"http://{candidate['api']['STOLAS_LISTEN']}:{candidate['api']['STOLAS_PORT']}"
            elif section == "schedule":
                if candidate["n8n"]["mode"] == "later":
                    print("Сначала выберите интеграцию n8n.")
                    continue
                candidate["n8n"]["hours"] = ask("Интервал замеров, часов", candidate["n8n"]["hours"], integer(1, 23))
                candidate["n8n"]["timezone"] = ask("Часовой пояс IANA", candidate["n8n"]["timezone"], timezone)
            validate_plan(candidate)
            plan = candidate
            if section not in plan["completed"]:
                plan["completed"].append(section)
            save_draft(root, plan)
            if position < len(stages):
                position += 1
        except Back:
            if position < len(stages) and position:
                position -= 1
        except Rescan:
            facts.clear()
            facts.update(environment.discover(root))
            print_facts(facts)
        except Cancel:
            if draft.exists() and not draft.is_symlink():
                draft.unlink()
            raise
        except (ValueError, RuntimeError) as error:
            print("Шаг не завершён:", str(error))
            if position < len(stages) and section == "n8n":
                print("Повторите шаг, :rescan, :back или :cancel; другой раздел доступен из сводки.")


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
    container = options.get("docker_id") or options.get("docker_container")
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
            if result.returncode in (126, 127) or "executable file not found" in result.stderr.lower():
                raise RuntimeError("В контейнере n8n не удалось запустить Node.js. Проверка из его сети НЕ выполнена; проверьте HTTP Request через Manual test. Экспорт сохранён в n8n/local.json")
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
    state = json.loads(state_path.read_text()) if state_path.exists() else {"url": options["url"], "credentials": []}
    if state["url"] != options["url"]:
        raise RuntimeError("Состояние относится к другому n8n. Используйте export; существующие ресурсы не изменены")
    if state.get("pending"):
        raise RuntimeError("Предыдущий запрос n8n мог создать ресурс, но его ID не получен. Проверьте n8n/install-state.json и ресурсы в n8n; используйте export, автоматический повтор остановлен")
    if state.get("workflow_id"):
        print("Существующий workflow сохранён. Для изменения его настроек импортируйте n8n/local.json вручную; дубликат не создаётся.")
        return
    write_private(state_path, json.dumps(state, indent=2) + "\n")
    credentials = [
        ("httpHeaderAuth", "Stolas API", {"name": "Authorization", "value": "Bearer " + api["STOLAS_API_TOKEN"]}),
        ("telegramApi", "Stolas Telegram", {"accessToken": options["bot_token"], "baseUrl": "https://api.telegram.org"}),
    ]
    for kind, name, values in credentials:
        ref = next(({k: c[k] for k in ("id", "name")} for c in state["credentials"] if c["type"] == kind), None)
        if ref is None:
            state["pending"] = kind
            write_private(state_path, json.dumps(state, indent=2) + "\n")
            created = request_json(base, "/credentials", options["key"], {"name": name, "type": kind, "data": values}, n8n=True)
            ref = {"id": str(created["id"]), "name": name}
            state["credentials"].append({"type": kind, **ref})
            state.pop("pending")
            write_private(state_path, json.dumps(state, indent=2) + "\n")
        for target in (("Run Stolas", "Read summary") if kind == "httpHeaderAuth" else ("Telegram alert",)):
            next(n for n in data["nodes"] if n["name"] == target)["credentials"] = {kind: ref}
    payload = {k: data[k] for k in ("name", "nodes", "connections", "settings")}
    state["pending"] = "workflow"
    write_private(state_path, json.dumps(state, indent=2) + "\n")
    created = request_json(base, "/workflows", options["key"], payload, n8n=True)
    state["workflow_id"] = str(created["id"])
    state.pop("pending")
    write_private(state_path, json.dumps(state, indent=2) + "\n")
    print("Создан НЕАКТИВНЫЙ workflow:", options["url"] + "/workflow/" + urllib.parse.quote(state["workflow_id"], safe=""))
    print("В n8n выполните Manual test и проверьте credentials, доступ к Stolas и Telegram.")
    print("После проверки включите расписание в n8n. Секреты не входят в export; приватный черновик удаляется после успешного завершения.")


def install(root=ROOT, configure_only=False, reuse=False, recover=False):
    current = root / "config/local.json"
    existing = validated(current) if current.exists() else copy.deepcopy(DEFAULT)
    facts = environment.discover(root)
    print_facts(facts)
    plan = {"config": existing, "api": read_api(root), "n8n": {"mode": "later"}, "completed": []} if recover else collect_plan(root, existing, facts, reuse)
    validate_plan(plan)
    cfg, api, options = plan["config"], plan["api"], plan["n8n"]
    generated = workflow(root, options) if options["mode"] not in ("later", "keep") else None
    progress_path = root / ".stolas-progress.json"
    previous_progress = json.loads(progress_path.read_text()) if progress_path.exists() else {}
    fingerprint = hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()
    first_status = previous_progress.get("first_status")
    measured = (previous_progress.get("config_sha256") == fingerprint and first_status is not None
                and previous_progress.get("stage") != "complete"
                and (previous_progress.get("stage") == "measured" or previous_progress.get("measurement_done")))
    def checkpoint(stage, **extra):
        save_draft(root, plan)
        write_private(progress_path, json.dumps({"stage": stage, "config_sha256": fingerprint,
                      "measurement_done": bool(measured), "first_status": first_status, **extra}) + "\n")
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
    if options["mode"] not in ("later", "keep"):
        write_private(root / "n8n/settings.json", json.dumps({k: v for k, v in options.items() if k not in ("key", "bot_token")}, ensure_ascii=False, indent=2) + "\n")
    if generated:
        write_private(root / "n8n/local.json", json.dumps(generated, ensure_ascii=False, indent=2) + "\n")
        print("Workflow без секретов сохранён:", root / "n8n/local.json")
    checkpoint("configured")
    print("\nНастройки сохранены. Старые версии файлов сохранены рядом как .bak-*.")
    if configure_only:
        if generated:
            write_private(root / "n8n/local.json", json.dumps(generated, ensure_ascii=False, indent=2) + "\n")
        print("Только конфигурация: установка, сеть, n8n API и тест скорости не запускались.")
        (root / ".stolas-draft.json").unlink()
        return 0
    compose = docker_command(root)
    run(compose + ["config", "--quiet"], root)
    run(compose + ["build", "stolas"], root)
    run(compose + ["run", "--rm", "--no-deps", "stolas", "validate"], root)
    run(compose + ["up", "-d", "--force-recreate", "--wait", "--wait-timeout", "90", "stolas"], root)
    checkpoint("deployed")
    if options["mode"] != "later":
        check_connection(root, options, api, compose)
    if reuse:
        host = api["STOLAS_LISTEN"] if api["STOLAS_LISTEN"] != "0.0.0.0" else "127.0.0.1"
        request_json(f"http://{host}:{api['STOLAS_PORT']}", "/healthz", api["STOLAS_API_TOKEN"])
        result = None
        print("Сервис обновлён; конфигурация, история и n8n сохранены. Дополнительный нагрузочный тест не запускался.")
    elif measured:
        result = {"status": first_status}
        print("Первичный CLI-цикл уже завершён до прерывания; повторный нагрузочный тест не запускается.")
    else:
        result = first_test(compose, root)
    if result:
        first_status, measured = result["status"], True
    checkpoint("measured", first_status=result["status"] if result else None)
    if generated:
        connect_n8n(root, options, api, generated)
    elif not reuse:
        print("n8n отложен. Выполнен один CLI-цикл; автоматическое расписание не включено.")
    print("API Stolas запущен. История: docker compose exec stolas python3 -m agent history")
    checkpoint("complete", first_status=result["status"] if result else None)
    (root / ".stolas-draft.json").unlink()
    if result and result["status"] in ("route_blocked", "unavailable"):
        print("Установка завершена, но измерение не получено. Проверьте ошибки в JSON выше.")
        return 2
    return 0


def main():
    parser = argparse.ArgumentParser(description="Интерактивная установка Stolas на Linux")
    parser.add_argument("--configure-only", action="store_true", help="только записать настройки без установки и тестирования")
    parser.add_argument("--reuse-config", action="store_true", help="сохранить настройки и существующий n8n при обновлении")
    parser.add_argument("--recover", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--diagnose", action="store_true", help="обнаружение без изменений, JSON без секретов")
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not sys.platform.startswith("linux"):
        parser.error("Запускайте установщик на целевом Linux-хосте.")
    if sys.version_info < (3, 10):
        parser.error("Для установщика требуется Python 3.10+.")
    try:
        if args.diagnose:
            print(json.dumps(environment.discover(args.root), ensure_ascii=False, indent=2))
            return 0
        return install(root=args.root, configure_only=args.configure_only, reuse=args.reuse_config, recover=args.recover)
    except Cancel:
        print("\nОтменено до применения. Действующая конфигурация и контейнеры не изменены.")
        return 3
    except (KeyboardInterrupt, EOFError):
        print("\nУстановка прервана. Завершённые шаги сохранены в приватном черновике. Повторите команду для продолжения; действующие сервисы не удаляются.", file=sys.stderr)
        return 130
    except (ValueError, RuntimeError, OSError, KeyError) as error:
        print("\nОшибка установки:", str(error), file=sys.stderr)
        print("Исправьте причину и повторите запуск. Не публикуйте .env и его резервные копии.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
