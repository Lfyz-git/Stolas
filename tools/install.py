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
from tools import environment, layout  # noqa: E402
from tools.terminal import ui  # noqa: E402


class Back(Exception):
    pass


class Cancel(Exception):
    pass


class Rescan(Exception):
    pass


LABELS = {
    "required": "Да, проверять (рекомендуется)", "off": "Нет, пропускать проверку",
    "apply": "Установить / применить", "cancel": "Отменить", "wan": "Проверка интернет-канала",
    "measurements": "Параметры измерений и серверы", "api": "Доступ к API",
    "rescan": "Повторить обнаружение", "resume": "Продолжить", "saved": "Начать заново с сохранённых настроек",
    "sequential": "По очереди", "random": "В случайном порядке",
    "export": "Подготовить файл для импорта", "connect": "Подключить", "create": "Создать credential Stolas API", "later": "Отложить", "keep": "Сохранить",
    "docker": "Клиент в Docker на этой машине", "native": "Клиент на этой машине",
    "lan": "Клиент на другой машине через HTTPS", "proxy": "Через HTTPS reverse proxy",
    "vpn": "Клиент в доверенной VPN-сети",
    "alerts_only": "Только проблемы", "every_measurement": "Каждый результат", "daily_summary": "Сводка за сутки",
    "local": "Только на этой машине (рекомендуется)", "network": "В доверенной сети",
}
GROUP_LABELS = {"primary": "Основные", "additional": "Дополнительные", "emergency": "Аварийные"}


def ask(label, default="", convert=str, secret=False):
    terminal = ui()
    terminal.line()
    terminal.line(label)
    if secret:
        terminal.line("Ввод скрыт." + (" Enter сохраняет значение." if default else ""))
    while True:
        suffix = f" [{default}]" if default != "" and not secret else ""
        if len(suffix) + 8 > terminal.width:
            terminal.line("По умолчанию: " + str(default))
            suffix = " [Enter]"
        prompt = terminal.style("Ввод" + suffix + ": ", "prompt")
        raw = (getpass.getpass if secret else input)(prompt)
        navigation = raw.strip().lower()
        if navigation in (":back", "назад"):
            raise Back()
        if navigation in (":cancel", "отмена"):
            raise Cancel()
        if navigation in (":rescan", "обновить"):
            raise Rescan()
        try:
            return convert(raw.strip() if raw.strip() else default)
        except (ValueError, TypeError, ZoneInfoNotFoundError) as error:
            explanation = str(error) if not secret else getattr(convert, "description", "проверьте формат секрета")
            terminal.line()
            terminal.result(explanation or "проверьте формат значения", "error")


def choice(label, values, default):
    terminal = ui()
    terminal.line()
    terminal.line(label)
    terminal.line()
    for i, value in enumerate(values, 1):
        terminal.line(f"  {i}. {LABELS.get(value, value)}")
    def parse(value):
        if str(value).isdigit() and 1 <= int(value) <= len(values):
            return values[int(value) - 1]
        if value.lower() not in values:
            raise ValueError(f"введите номер от 1 до {len(values)}")
        return value.lower()
    number = str(values.index(default) + 1) if default in values else ""
    return ask("Выберите вариант", number, parse)


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
    from tools.timezones import validate
    return validate(value)


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
    layout.directory(path.parent)
    if path.is_symlink():
        raise ValueError(f"Отказ записи через symlink: {path}")
    runtime_root = next((parent for parent in path.parents if layout.runtime(parent)), None)
    if path.exists() and (runtime_root is None or path == runtime_root / ".env" or path == runtime_root / "config/local.json"):
        suffix = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(3)
        backup = path.with_name(path.name + ".bak-" + suffix)
        if runtime_root:
            directory = layout.bounded(runtime_root, ".stolas/backups/settings/" + suffix)
            layout.private_dir(directory)
            backup = directory / path.name
        with backup.open("x", encoding="utf-8", newline="\n") as file:
            os.chmod(backup, 0o600)
            file.write(path.read_text(encoding="utf-8"))
        layout.keep_owner(backup, layout.owner_for(path))
    fd, name = tempfile.mkstemp(prefix=".stolas-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as file:
            file.write(content)
        layout.keep_owner(Path(name), layout.owner_for(path))
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
        ui().result(f"Таймаут одного iperf3 должен быть не меньше {minimum} секунд.", "error")
        cfg["process_timeout"] = ask("Таймаут одного iperf3", minimum, integer(minimum, 120))
    cfg["bind_address"] = ask("Локальный IPv4 для iperf3 (-: автоматически)", cfg["bind_address"] or "", lambda v: optional(v, lambda ip: str(ipaddress.IPv4Address(ip))))
    ui().line("\nПроверка канала сравнивает внешний IP до и после измерений.")
    ui().line("Для строгого запрета тестирования через LTE закрепите трафик на маршрутизаторе.")
    route = cfg["route"]
    route["mode"] = choice("Проверять основной канал перед тестами?", ("required", "off"), route["mode"])
    route["public_ip_urls"] = ask("HTTPS источники внешнего IPv4 через запятую", ",".join(route["public_ip_urls"]), lambda value: list(dict.fromkeys(https_url(item.strip()) for item in value.split(","))))
    route["verification_timeout"] = ask("Общий бюджет одной проверки WAN, секунд", route["verification_timeout"], integer(2, 60))
    route["source_timeout"] = ask("Бюджет одного источника IPv4, секунд", route["source_timeout"], integer(1, 30))
    route["min_confirmations"] = ask("Минимум согласных ответов источников", min(route["min_confirmations"], len(route["public_ip_urls"])), integer(1, len(route["public_ip_urls"])))
    route["expected_public_cidrs"] = ask("Внешний IPv4 основного WAN /32 или CIDR через запятую" + (" (-: очистить)" if route["mode"] == "off" else ""), ",".join(route["expected_public_cidrs"]), lambda v: [] if route["mode"] == "off" and v in ("", "-") else cidrs(v))
    for key, label in (("interface", "Интерфейс Linux"), ("gateway", "Шлюз Linux")):
        route[key] = ask(label + " (-: не проверять)", route[key] or "", lambda v: optional(v, matching(r"[\w.:-]{1,64}")))
    ui().line("\nГруппы серверов iperf3. В каждой можно задать любое количество серверов.")
    ui().line("По очереди — с сохранением позиции; случайно — без повторов внутри цикла.")
    descriptions = {
        "primary": "Основные: обычный замер; сначала перебираются серверы этой группы",
        "additional": "Дополнительные: подтверждение низкой скорости или подмена недоступных основных",
        "emergency": "Аварийные: последняя подмена/подтверждение, если предыдущие группы не справились",
    }
    all_servers = []
    for name in GROUPS:
        ui().line(descriptions[name])
        group = cfg["server_groups"][name]
        group["selection"] = choice("Выбор сервера — " + GROUP_LABELS[name].lower(), ("sequential", "random"), group["selection"])
        count = ask("Количество серверов — " + GROUP_LABELS[name].lower(), len(group["servers"]), integer(1 if name == "primary" else 0))
        result = []
        for index in range(count):
            server = copy.deepcopy(group["servers"][index]) if index < len(group["servers"]) else dict(id=f"{name}-{index + 1}", host="", ports=[5201], min_download_mbps=500, min_upload_mbps=500)
            ui().line(f"{GROUP_LABELS[name]}: сервер {index + 1}")
            for key, label, pattern in (("id", "Идентификатор", r"[a-zA-Z0-9_-]{1,64}"), ("host", "Hostname или IPv4", r"[a-zA-Z0-9][a-zA-Z0-9.-]{0,252}")):
                while True:
                    server[key] = ask(label, server[key], matching(pattern))
                    if all(s[key].lower() != server[key].lower() for s in all_servers):
                        break
                    ui().result("Серверы во всех группах должны иметь разные имена и адреса.", "error")
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
        if separator and key in ("STOLAS_API_TOKEN", "STOLAS_LISTEN", "STOLAS_PORT", "STOLAS_LOG_LEVEL", "STOLAS_LOG_FORMAT"):
            values[key] = value.strip().strip("'\"")
    if not {"STOLAS_API_TOKEN", "STOLAS_LISTEN", "STOLAS_PORT"} <= set(values):
        raise RuntimeError("Для обновления сначала завершите настройку .env через мастер")
    matching(r"[A-Za-z0-9_-]{32,256}")(values["STOLAS_API_TOKEN"])
    ipaddress.IPv4Address(values["STOLAS_LISTEN"])
    integer(1, 65535)(values["STOLAS_PORT"])
    return values


def select_named(label, items, names, default=0, automatic=True):
    if len(items) == 1 and automatic:
        ui().result(label + ": " + names[0])
        return items[0]
    for index, name in enumerate(names, 1):
        ui().line(f"  {index}. {name}")
    index = ask(label + " — номер варианта", default + 1, integer(1, len(items)))
    return items[index - 1]


def propose_port(api, facts, root):
    address, initial = api["STOLAS_LISTEN"], int(api["STOLAS_PORT"])
    if (root / ".env").is_file():
        saved = read_api(root)
        if saved["STOLAS_LISTEN"] == address and saved["STOLAS_PORT"] == str(initial):
            ui().line(f"Сохранённый endpoint {address}:{initial} не изменяется автоматически.")
            return
    for port in range(initial, min(65536, initial + 100)):
        state = environment.port_state(address, port)
        if state == "free":
            if port != initial:
                ui().result(f"Порт {initial} занят. Предлагается свободный порт {port}.", "warning")
            api["STOLAS_PORT"] = str(port)
            return
        if state == "not_local":
            raise ValueError(f"Адрес {address} отсутствует на Linux-хосте. Повторите обнаружение или выберите другую схему подключения")
        if state == "denied":
            raise ValueError(f"ОС не разрешает привязку к {address}:{port}; выберите непривилегированный порт в разделе api")
    raise ValueError(f"Нет свободного порта в диапазоне {initial}–{min(65535, initial + 99)}; измените порт в разделе api")


def check_api_port(api, facts, allow_existing=False):
    """Before stopping the old service: distinguish verified ownership from unknown."""
    address, port = api["STOLAS_LISTEN"], int(api["STOLAS_PORT"])
    if not facts["docker"]["available"]:
        raise RuntimeError(facts["docker"]["reason"] + ". Старый сервис не остановлен; восстановите доступ к Docker")
    bindings = [b for b in facts.get("port_bindings", []) if b["port"] == port and b["address"] in (address, "0.0.0.0", "::", "")]
    if any(not b["owned"] for b in bindings):
        raise RuntimeError(f"Конфликт {address}:{port} подтверждён Docker: порт опубликован посторонним контейнером. Старый Stolas не остановлен; освободите endpoint или явно измените настройку")
    state = environment.port_state(address, port)
    if state == "free":
        return
    if state == "busy" and allow_existing and facts.get("stolas"):
        return  # /healthz failure is not evidence of foreign ownership.
    if state == "busy":
        raise RuntimeError(f"Endpoint {address}:{port} занят, но принадлежность слушателя не подтверждена. Старый сервис не остановлен; проверьте Docker, ss и повторите диагностику. Порт автоматически не меняется")
    raise RuntimeError(f"Не удалось проверить привязку {address}:{port}: {state}. Старый сервис не остановлен")


def save_draft(root, plan):
    plan = {k: v for k, v in plan.items() if k in ("config", "api", "completed", "version")}
    path = layout.state_path(root, ".stolas-draft.json")
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise ValueError("Черновик не записывается через symlink")
    fd, name = tempfile.mkstemp(prefix=".stolas-draft-", dir=path.parent)
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
    address = ipaddress.IPv4Address(api["STOLAS_LISTEN"])
    if address.is_unspecified or address.is_global or address.is_multicast or address.is_reserved:
        raise ValueError("Выберите конкретный адрес хоста; публичное прослушивание не предлагается")
    if api.get("STOLAS_LOG_LEVEL", "INFO") not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        raise ValueError("Неизвестный уровень логирования")
    if api.get("STOLAS_LOG_FORMAT", "json") not in ("json", "text"):
        raise ValueError("Выберите формат логов json или text")


def print_facts(facts):
    terminal = ui()
    terminal.stage("Проверка системы", 1)
    terminal.line("Каталог: " + facts["installation"]["directory"])
    terminal.line("Система: " + facts.get("system", sys.platform) + "; узел: " + facts["hostname"])
    if facts["docker"]["available"]:
        terminal.result("Docker доступен")
    elif not shutil.which("docker"):
        terminal.line("Docker будет установлен после подтверждения.")
    else:
        terminal.result(facts["docker"]["reason"], "warning")
    if facts["docker"].get("access_warning"):
        terminal.result(facts["docker"]["access_warning"], "warning")
    if facts["installation"].get("config"):
        terminal.result("найдены сохранённые настройки")


def print_plan(plan, facts, reuse=False):
    terminal = ui()
    cfg, api = plan["config"], plan["api"]
    terminal.stage("Проверьте настройки", 3)
    terminal.line("Узел: " + cfg["node"])
    terminal.line(f"Измерение: {cfg['parallel']} потока, {cfg['seconds']} секунд на направление")
    names = dict(primary="Основные", additional="Дополнительные", emergency="Аварийные")
    for name in GROUPS:
        group = cfg["server_groups"][name]
        terminal.line(names[name] + ": " + ", ".join(s["host"] for s in group["servers"]) + " (" + LABELS[group["selection"]] + ")")
    terminal.line()
    terminal.line("Проверка канала: " + ("включена" if cfg["route"]["mode"] == "required" else "выключена"))
    if cfg["route"]["mode"] == "required":
        terminal.line("Разрешённые IP: " + (", ".join(cfg["route"]["expected_public_cidrs"]) or "не заданы"))
    terminal.line("API: http://" + api["STOLAS_LISTEN"] + ":" + api["STOLAS_PORT"])
    terminal.line("Токен API: сохранён / создан автоматически; скрыт")
    terminal.line()
    terminal.line("Будет запущен Stolas Core." + (" Настройки и история сохраняются." if reuse else " Затем — один тест скорости."))
    if not facts["docker"]["available"] and not shutil.which("docker"):
        terminal.line("Также будет установлен Docker Engine с Compose.")


def collect_plan(root, existing, facts, reuse=False):
    api = read_api(root) if (root / ".env").exists() else {"STOLAS_API_TOKEN": secrets.token_urlsafe(32), "STOLAS_LISTEN": "127.0.0.1", "STOLAS_PORT": "8080"}
    plan = {"config": copy.deepcopy(existing), "api": api, "completed": [], "version": 2}
    draft = layout.state_path(root, ".stolas-draft.json")
    configured = (root / "config/local.json").exists() and (root / ".env").exists()
    if not configured:
        plan["config"]["node"] = re.sub(r"[^\w.-]", "-", facts["hostname"])[:64] or "stolas-node"
    ui().line()
    ui().line("Enter — принять. :back — назад; :cancel — отмена.")
    ui().line(":rescan — обновить. Ctrl+C — сохранить и выйти.")
    if draft.exists() and not reuse:
        if draft.is_symlink():
            raise ValueError("Черновик является symlink; чтение остановлено")
        saved = json.loads(draft.read_text(encoding="utf-8"))
        # Legacy integration secrets never enter the new Core plan or backups.
        saved = {k: saved[k] for k in ("config", "api", "completed") if k in saved}
        saved["completed"] = [x for x in saved.get("completed", []) if x in ("wan", "api", "measurements")]
        saved["version"] = 2
        validate_plan(saved)
        save_draft(root, saved)
        ui().result("найдена незавершённая настройка")
        if choice("Продолжить настройку?", ("resume", "saved"), "resume") == "resume":
            plan = saved
        else:
            draft.unlink()
    stages = [] if reuse or configured else [s for s in ("wan",) if s not in plan["completed"]]
    position = 0
    if not reuse:
        try:
            propose_port(plan["api"], facts, root)
        except ValueError as error:
            ui().result(str(error), "warning")
    while True:
        section = stages[position] if position < len(stages) else None
        try:
            if section is None:
                print_plan(plan, facts, reuse)
                values = ("apply", "cancel") if reuse else ("apply", "wan", "measurements", "api", "rescan", "cancel")
                section = choice("Что сделать?", values, "apply")
                if section == "cancel":
                    raise Cancel()
                if section == "apply":
                    if plan["config"]["route"]["mode"] == "required" and not plan["config"]["route"]["expected_public_cidrs"]:
                        raise ValueError("Укажите разрешённый IP основного канала в разделе «Проверка интернет-канала»")
                    validate_plan(plan)
                    before = copy.deepcopy(plan["api"])
                    if not reuse:
                        propose_port(plan["api"], facts, root)
                    if plan["api"] != before:
                        ui().result("Порт изменился; проверьте сводку ещё раз.", "warning")
                        continue
                    save_draft(root, plan)
                    return plan
            if section == "rescan":
                facts.clear()
                facts.update(environment.discover(root))
                print_facts(facts)
                continue
            candidate = copy.deepcopy(plan)
            if section == "wan":
                ui().stage("Проверка интернет-канала", 2)
                route = candidate["config"]["route"]
                ui().line("Основной канал: пока не подтверждён")
                ui().line("Разрешённые IP: " + (", ".join(route["expected_public_cidrs"]) or "не заданы"))
                ui().line()
                ui().line("Перед тестом Stolas сверит внешний IP с разрешёнными адресами.")
                route["mode"] = choice("Проверять основной канал перед тестами?", ("required", "off"), route["mode"])
                if route["mode"] == "required":
                    ui().line()
                    ui().line("Укажите внешний IPv4 основного подключения или диапазон провайдера.")
                    ui().line("Например: 203.0.113.10/32. При смене адреса настройку нужно обновить.")
                    route["expected_public_cidrs"] = ask("Разрешённые IP (через запятую)", ",".join(route["expected_public_cidrs"]), cidrs)
            elif section == "measurements":
                ui().stage("Параметры измерений")
                candidate["config"] = collect_config(candidate["config"])
            elif section == "api":
                ui().stage("Доступ к API")
                access = choice("Откуда будут обращаться к агенту?", ("local", "network"), "local")
                candidate["api"]["STOLAS_LISTEN"] = "127.0.0.1"
                if access == "network":
                    ui().line("Выберите адрес доверенной сети. Firewall остаётся под вашим управлением.")
                    addresses = [a for a in facts["addresses"] if not ipaddress.IPv4Address(a["address"]).is_global and not ipaddress.IPv4Address(a["address"]).is_loopback]
                    if not addresses:
                        raise ValueError("Адреса локальной сети не найдены. Настройте интерфейс и обновите обнаружение")
                    selected = select_named("Адрес хоста", addresses, [a["interface"] + " — " + a["address"] for a in addresses], automatic=False)
                    candidate["api"]["STOLAS_LISTEN"] = selected["address"]
                candidate["api"]["STOLAS_PORT"] = str(ask("Порт API", candidate["api"]["STOLAS_PORT"], integer(1024, 65535)))
                propose_port(candidate["api"], facts, root)
            validate_plan(candidate)
            plan = candidate
            if section not in plan["completed"]:
                plan["completed"].append(section)
            save_draft(root, plan)
            if position < len(stages):
                position += 1
        except Back:
            if section is None:
                position = max(0, len(stages) - 1)
            elif position < len(stages):
                position = max(0, position - 1)
        except Rescan:
            facts.clear()
            facts.update(environment.discover(root))
            print_facts(facts)
        except Cancel:
            if draft.exists() and not draft.is_symlink():
                draft.unlink()
            raise
        except (ValueError, RuntimeError) as error:
            ui().result(str(error), "error")


def run(args, root, capture=True, check=True):
    result = subprocess.run(args, cwd=root, env=clean_env(), text=True, encoding="utf-8", capture_output=capture)
    if check and result.returncode:
        raise RuntimeError("Команда завершилась ошибкой: " + " ".join(map(str, args[:4])))
    return result


def docker_command(root):
    command = ["docker"]
    if not shutil.which("docker"):
        print("Docker отсутствует. Будут установлены Docker Engine и Compose из официального apt-репозитория.")
        prefix = [] if os.geteuid() == 0 else ["sudo"]
        run(prefix + ["sh", str(layout.code_root(root) / "tools/install-docker.sh")], root)
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
    if layout.runtime(root):
        from tools import resources
        resources.configure(root, command)
        return resources.compose(root, command)
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
    ui().line("\nТест скорости (может занять несколько минут)…")
    result = run(compose + ["exec", "-T", "stolas", "python3", "-m", "agent", "run"], root, capture=True, check=False)
    if result.returncode not in (0, 2):
        raise RuntimeError("Тест не запущен. Проверьте ./stolas diagnose; повторите ./stolas run после завершения текущего теста")
    data = json.loads(result.stdout)
    if data.get("time"):
        from tools import timezones
        zone = timezones.discover()
        if zone["name"]:
            ui().line("Время: " + timezones.display(data["time"], zone["name"]))
        else:
            ui().result(zone["error"], "warning")
            ui().line("Время UTC (пояс ОС не определён): " + data["time"])
    ui().line("Результат теста: " + {"ok": "успешно", "low_confirmed": "подтверждено снижение скорости", "low_unconfirmed": "низкая скорость без независимого подтверждения", "server_disagreement": "серверы показали разные результаты", "route_blocked": "измерение остановлено проверкой канала", "unavailable": "серверы недоступны"}.get(data["status"], data["status"]))
    if "mixed_routing" in data.get("warnings", []):
        ui().line()
        ui().result("сервисы определения IP показали разные адреса.", "warning")
        ui().line()
        ui().line("Возможна раздельная маршрутизация.")
        ui().line("Проверьте маршрут к серверам измерения.")
        ui().line()
        ui().line("Измерение продолжено; внешний маршрут к серверу не подтверждён.")
    reasons = {error.get("reason") for error in data.get("errors", [])} | {error for attempt in data.get("attempts", []) for error in attempt.get("errors", [])}
    explanations = {"route_unconfigured": "Укажите разрешённый внешний IP основного подключения.",
                    "route_public_ip_mismatch": "Внешний IP не совпал с разрешёнными адресами. Проверьте подключение и настройки канала.",
                    "route_verification_unavailable": "Не удалось проверить внешний IP. Проверьте доступ к HTTPS-источникам.",
                    "route_verification_conflict": "Источники внешнего IP вернули разные адреса.",
                    "dns_error": "Не удалось найти измерительный сервер. Проверьте DNS.",
                    "server_busy": "Измерительный сервер занят; повторите тест позже."}
    for reason in sorted(reasons - {None}):
        ui().result(explanations.get(reason, "Причина: " + reason + ". Подробности сохранены в истории."), "warning")
    for name in ("primary", "confirmation"):
        sample = data.get(name)
        if sample:
            group = GROUP_LABELS.get(sample.get("group"), "Сервер")
            ui().line(f"  {sample['server']} ({group.lower()}): DL {sample['download']['mbps']} / UL {sample['upload']['mbps']} Mbps")
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


def install(root=ROOT, configure_only=False, reuse=False, recover=False):
    layout.preflight(root)
    current = root / "config/local.json"
    existing = validated(current) if current.exists() else copy.deepcopy(DEFAULT)
    facts = environment.discover(root)
    print_facts(facts)
    if not configure_only and not facts["docker"]["available"] and (facts.get("tools", {}).get("docker") or shutil.which("docker")):
        raise RuntimeError(facts["docker"]["reason"] + ". Мастер остановлен до настройки: восстановите доступ к Docker и повторите команду; доступ через sudo -n также проверен")
    old_api = read_api(root) if (root / ".env").is_file() else None
    plan = {"config": existing, "api": read_api(root), "completed": []} if recover else collect_plan(root, existing, facts, reuse)
    validate_plan(plan)
    cfg, api = plan["config"], plan["api"]
    progress_path = layout.state_path(root, ".stolas-progress.json")
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
        commit_configuration(root, cfg, api)
    checkpoint("configured")
    ui().result("настройки сохранены")
    if configure_only:
        print("Настройки сохранены. Агент и тест скорости не запускались.")
        layout.state_path(root, ".stolas-draft.json").unlink()
        return 0
    ui().stage("Установка и проверка", 4)
    compose = docker_command(root)
    run(compose + ["config", "--quiet"], root)
    ui().line("Сборка образа — первый запуск может занять несколько минут…")
    run(compose + ["build", "stolas"], root)
    ui().result("образ собран")
    run(compose + ["run", "--rm", "--no-deps", "stolas", "validate"], root)
    same_endpoint = old_api and all(old_api[k] == api[k] for k in ("STOLAS_LISTEN", "STOLAS_PORT"))
    check_api_port(api, environment.discover(root), allow_existing=bool(same_endpoint))
    if layout.runtime(root):
        from tools import resources
        resources.activate(root, compose[:compose.index("compose")])
    run(compose + ["up", "-d", "--force-recreate", "--wait", "--wait-timeout", "90", "stolas"], root)
    checkpoint("deployed")
    host = api["STOLAS_LISTEN"]
    request_json(f"http://{host}:{api['STOLAS_PORT']}", "/healthz", api["STOLAS_API_TOKEN"])
    ui().result("API отвечает, авторизация проверена")
    if layout.runtime(root):
        resources.complete(root, compose[:compose.index("compose")])
    if reuse:
        result = None
        print("Сервис обновлён; конфигурация и история сохранены. Дополнительный нагрузочный тест не запускался.")
    elif measured:
        result = {"status": first_status}
        print("Первичный CLI-цикл уже завершён до прерывания; повторный нагрузочный тест не запускается.")
    else:
        result = first_test(compose, root)
    if result:
        first_status, measured = result["status"], True
    checkpoint("measured", first_status=result["status"] if result else None)
    ui().result("Stolas Core запущен")
    ui().line("API: http://" + api["STOLAS_LISTEN"] + ":" + api["STOLAS_PORT"])
    ui().line("Токен API сохранён в .env; история — в Docker volume.")
    ui().line("Результаты: ./stolas history")
    ui().line("Логи: ./stolas logs")
    checkpoint("complete", first_status=result["status"] if result else None)
    layout.state_path(root, ".stolas-draft.json").unlink()
    if layout.runtime(root):
        from tools import entrypoints
        entrypoints.offer(root)
    if result and result["status"] in ("route_blocked", "unavailable"):
        ui().result("Агент установлен, но измерение не получено. Исправьте причину выше и повторите тест.", "warning")
        return 2
    return 0


def commit_configuration(root, cfg, api):
    paths = (root / "config/local.json", root / ".env")
    if any(item.is_symlink() for path in paths for item in (path, *path.parents)):
        raise ValueError("Отказ записи через symlink")
    originals = {path: (path.read_bytes(), path.stat().st_mode & 0o777) if path.exists() else None for path in paths}
    previous_env = originals[paths[1]][0].decode() if originals[paths[1]] else ""
    resource_keys = {"STOLAS_PROJECT_NAME", "STOLAS_CONTAINER_NAME", "STOLAS_INSTANCE_ID", "STOLAS_DATA_VOLUME"}
    project_lines = [line for line in previous_env.splitlines() if line.partition("=")[0] in resource_keys]
    try:
        write_private(paths[0], json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")
        os.chmod(paths[0], 0o644)
        write_private(paths[1], "".join(k + "=" + v + "\n" for k, v in api.items()) + "".join(line + "\n" for line in project_lines))
    except BaseException:
        for path, original in originals.items():
            if original is None:
                if path.exists() and not path.is_symlink():
                    path.unlink()
            else:
                fd, name = tempfile.mkstemp(prefix=".stolas-restore-", dir=path.parent)
                try:
                    with os.fdopen(fd, "wb") as file:
                        file.write(original[0])
                    os.chmod(name, original[1])
                    layout.keep_owner(Path(name), layout.owner_for(path))
                    os.replace(name, path)
                finally:
                    if os.path.exists(name):
                        os.unlink(name)
        raise


def main():
    parser = argparse.ArgumentParser(description="Интерактивная установка Stolas на Linux")
    parser.add_argument("--configure-only", action="store_true", help="только записать настройки без установки и тестирования")
    parser.add_argument("--reuse-config", action="store_true", help="сохранить настройки при обновлении")
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
        ui().result("Установка отменена")
        return 3
    except (KeyboardInterrupt, EOFError):
        ui().result("Установка прервана. Повторите команду для продолжения.", "warning")
        return 130
    except (ValueError, RuntimeError, OSError, KeyError) as error:
        if isinstance(error, (OSError, KeyError)):
            from tools.diagnostics import report
            report(args.root, error)
        else:
            ui().result(str(error), "error")
        ui().line("Исправьте причину и повторите команду.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
