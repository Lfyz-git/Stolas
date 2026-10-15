"""Optional n8n integration. No Telegram secrets are read or created by Stolas."""
import sys
if __name__ == "__main__":
    sys.dont_write_bytecode = True
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import argparse
import copy
import ipaddress
import json
import hashlib
import subprocess
import urllib.parse
from tools.install import (ask, choice, select_named, propose_port, matching,
    integer, timezone, read_api, clean_env, write_private, request_json,
    docker_command, run, Cancel, ServiceRequestError)
from tools import environment, layout
from tools.terminal import ui
from tools.deploy import install_lock

class BindingError(ValueError):
    pass


class IntegrationConnectionError(RuntimeError):
    pass


def connection_url(value, topology=None):
    """Validate endpoint syntax independently of placement."""
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError("Неверный адрес или порт URL") from None
    if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ValueError("Нужен URL http/https без логина, query и fragment")
    if parsed.netloc.endswith(":") or port is not None and not 1 <= port <= 65535:
        raise ValueError("Порт endpoint должен быть от 1 до 65535")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        address = None
    if address is not None and (address.is_unspecified or address.is_multicast):
        raise ValueError("Endpoint должен содержать адрес, доступный клиенту")
    return value.rstrip("/")


def listener(value):
    try:
        address, port = value.rsplit(":", 1)
        address = ipaddress.IPv4Address(address)
        port = integer(1, 65535)(port)
        if address.is_multicast or address.is_reserved:
            raise ValueError()
        return str(address), str(port)
    except (ValueError, AttributeError):
        raise BindingError("Нужен локальный IPv4:порт, например 192.0.2.10:8080; порт 1–65535") from None


def local_listener_candidates(facts):
    bridges = {network.get("bridge") for network in facts.get("networks", {}).values() if network.get("driver") == "bridge"}
    gateways = {item.get("Gateway") for network in facts.get("networks", {}).values() if network.get("driver") == "bridge" for item in network.get("ipam") or []}
    candidates = {}
    for item in facts.get("addresses", []):
        try:
            address, _ = listener(item["address"] + ":8080")
        except (ValueError, KeyError, TypeError):
            continue
        if address == "0.0.0.0":
            continue
        interface = item["interface"]
        bridge = interface in bridges or address in gateways or interface == "docker0" or interface.startswith("br-")
        candidate = dict(address=address, interface=interface, bridge=bridge)
        # Deterministic even if discovery returns duplicate addresses/interfaces.
        if address not in candidates or interface < candidates[address]["interface"]:
            candidates[address] = candidate
    return sorted(candidates.values(), key=lambda item: (item["bridge"], ipaddress.IPv4Address(item["address"]).is_loopback,
                                                        int(ipaddress.IPv4Address(item["address"])), item["interface"]))


def validate_listener(api, facts, root):
    address, port = listener(api["STOLAS_LISTEN"] + ":" + api["STOLAS_PORT"])
    if address == "0.0.0.0" and api.get("STOLAS_BIND_CONFIRMED") != "1":
        raise BindingError("Прослушивание всех интерфейсов требует отдельного подтверждения")
    state = environment.port_state(address, int(port))
    if state == "free":
        return
    if state == "busy" and (root / ".env").is_file():
        saved = read_api(root)
        if all(saved[key] == api[key] for key in ("STOLAS_LISTEN", "STOLAS_PORT")) and facts.get("stolas"):
            return  # The authenticated local health check still runs before apply.
    if state == "not_local":
        raise BindingError("Адрес прослушивания отсутствует на этом хосте; выберите локальный IPv4 или повторно запустите мастер")
    if state == "busy":
        raise BindingError("Порт прослушивания занят; выберите другой IP:порт. Endpoint n8n пока не проверялся")
    raise BindingError("ОС запрещает привязку API; проверьте права и выберите доступный локальный IP:порт")



def collect_topology(api, facts=None, root=ROOT, previous=None):
    facts = facts if facts is not None else environment.discover(root)
    previous = previous or {}
    saved_direct = f"http://{api['STOLAS_LISTEN']}:{api['STOLAS_PORT']}"
    legacy = previous.get("topology") in ("lan", "vpn", "proxy")
    default = "remote" if legacy else previous.get("topology", "docker" if facts["n8n"] else "")
    topology = choice("Где работает n8n", ("docker", "native", "remote"), default)
    options = {"topology": topology}
    preserve = False
    if legacy:
        ui().line("Старая схема подключения заменяется размещением n8n. Текущий listener: " + api["STOLAS_LISTEN"] + ":" + api["STOLAS_PORT"])
        preserve = choice("Сохранить текущий listener и проверить endpoint?", ("keep", "change"), "keep") == "keep"
    if topology == "docker":
        if not facts["docker"]["available"]:
            raise ValueError(facts["docker"]["reason"] + ". Исправьте доступ и повторно запустите мастер, либо выберите другую схему")
        if not facts["n8n"]:
            raise ValueError("Запущенный n8n не обнаружен по образу/Compose метаданным. Запустите его и повторно запустите мастер, либо выберите удалённое подключение")
        saved = next((c for c in facts["n8n"] if c["name"] == previous.get("docker_container")), None)
        container = saved or select_named("Экземпляр n8n", facts["n8n"],
            [f"{c['name']} — {c['image']}, проект {c.get('project') or 'без Compose'}" for c in facts["n8n"]],
            automatic=len(facts["n8n"]) == 1 and facts["n8n"][0]["confidence"] == "image")
        candidates, reasons = environment.host_candidates(container, facts)
        if not candidates:
            raise ValueError("Не найден подтверждённый адрес хоста для n8n: " + "; ".join(reasons or ["контейнер не подключён к подходящей сети"]) + ". Выберите другое размещение или исправьте связность; сети n8n не изменены")
        saved_network = next((c for c in candidates if c["network"] == previous.get("network") and c["address"] == api["STOLAS_LISTEN"]), None)
        selected = saved_network or select_named("Сеть для связи n8n со Stolas", candidates,
                    [f"{c['network']} — интерфейс {c['interface']} ({c['address']})" for c in candidates])
        options.update(docker_container=container["name"], docker_id=container["id"], network=selected["network"], network_mode=container["mode"])
        if not preserve:
            api["STOLAS_LISTEN"] = selected["address"]
            propose_port(api, facts, root)
    elif topology == "remote" and not preserve:
        candidates = local_listener_candidates(facts)
        items = [*candidates, None]
        names = [item["interface"] + " — " + item["address"] + ":8080" + (" (Docker bridge)" if item["bridge"] else "") for item in candidates]
        ui().line("Локальный IP:порт, на котором Stolas будет слушать.")
        default_index = next((index for index, item in enumerate(candidates) if item["address"] == api["STOLAS_LISTEN"]), 0)
        selected = select_named("Listener Stolas", items, [*names, "Ввести IPv4:порт вручную"], default=default_index, automatic=False)
        binding = selected["address"] + ":8080" if selected else ask("Локальный IPv4:порт Stolas", api["STOLAS_LISTEN"] + ":" + api["STOLAS_PORT"], lambda value: ":".join(listener(value)))
        api["STOLAS_LISTEN"], api["STOLAS_PORT"] = listener(binding)
    elif topology == "native" and not preserve:
        api["STOLAS_LISTEN"] = "127.0.0.1"
        api["STOLAS_PORT"] = "8080" if previous.get("topology") != "native" else api["STOLAS_PORT"]
        propose_port(api, facts, root)
    address = ipaddress.IPv4Address(api["STOLAS_LISTEN"])
    if address.is_unspecified or address.is_global:
        ui().result("Listener может открыть API внешним клиентам. Проверьте firewall и доверие к сети", "warning")
        if choice("Явно разрешить выбранное прослушивание?", ("allow_bind", "cancel"), "cancel") != "allow_bind":
            raise Cancel()
        api["STOLAS_BIND_CONFIRMED"] = "1"
    else:
        api.pop("STOLAS_BIND_CONFIRMED", None)
    validate_listener(api, facts, root)
    direct = "" if address.is_unspecified else f"http://{api['STOLAS_LISTEN']}:{api['STOLAS_PORT']}"
    # Preserve explicit and older unflagged overrides relative to the saved bind.
    saved_endpoint = previous.get("endpoint")
    override = saved_endpoint and (previous.get("endpoint_override") or saved_endpoint.rstrip("/") != saved_direct)
    endpoint = saved_endpoint if override else direct
    if not endpoint:
        raise BindingError("Для выбранного listener нужен отдельно настроенный endpoint Stolas")
    options["endpoint"] = connection_url(endpoint)
    options["endpoint_override"] = options["endpoint"] != direct
    return options


def list_projects(options):
    """Read accessible Public API projects, including projects without credentials."""
    projects, seen, cursor = {}, set(), None
    for _ in range(100):
        path = "/projects?limit=100" + ("&cursor=" + urllib.parse.quote(cursor, safe="") if cursor else "")
        page = request_json(options["url"] + "/api/v1", path, options["key"], n8n=True)
        if not isinstance(page.get("data"), list):
            raise ValueError("Список проектов недоступен в этой версии n8n")
        for item in page["data"]:
            if isinstance(item, dict) and item.get("id"):
                identifier = str(item["id"])
                projects[identifier] = {"id": identifier, "name": str(item.get("name") or identifier)}
        cursor = page.get("nextCursor")
        if not cursor:
            return list(projects.values())
        if not isinstance(cursor, str) or cursor in seen:
            break
        seen.add(cursor)
    raise ValueError("Не удалось прочитать весь список проектов; используйте ручной импорт")


def list_credentials(options):
    """Read bounded, paginated metadata only; discard all unknown fields."""
    items, seen, cursor = [], set(), None
    for _ in range(100):
        path = "/credentials?limit=100" + ("&cursor=" + urllib.parse.quote(cursor, safe="") if cursor else "")
        page = request_json(options["url"] + "/api/v1", path, options["key"], n8n=True)
        if not isinstance(page.get("data"), list):
            raise ValueError("Список credentials недоступен в этой версии n8n")
        for item in page["data"]:
            if item.get("type") not in ("telegramApi", "httpHeaderAuth") or not item.get("id"):
                continue
            projects = []
            for shared in item.get("shared", []):
                project = shared.get("project", {})
                # Public API versions return either a relation or a flat project.
                identifier = shared.get("projectId") or project.get("id")
                if not identifier and shared.get("role") in ("credential:owner", "credential:user"):
                    identifier = shared.get("id")
                if identifier:
                    projects.append({"id": str(identifier), "name": project.get("name") or shared.get("name") or str(identifier)})
            items.append({"id": str(item["id"]), "name": str(item.get("name", item["id"])),
                          "type": item["type"], "projects": projects})
        cursor = page.get("nextCursor")
        if not cursor:
            return items
        if not isinstance(cursor, str) or cursor in seen:
            break
        seen.add(cursor)
    raise ValueError("Не удалось прочитать весь список credentials; используйте ручной импорт")


def credential_choice(items, kind):
    matching_items = [c for c in items if c["type"] == kind]
    if not matching_items:
        return None
    names = [c["name"] + " · ID " + c["id"] + (" · " + ", ".join(p["name"] for p in c["projects"]) if c["projects"] else "") for c in matching_items]
    return select_named("Telegram credential" if kind == "telegramApi" else "Credential доступа к Stolas", matching_items, names, automatic=kind == "telegramApi")



def validate_discovered_endpoint(plan, facts, root=ROOT):
    options = plan["n8n"]
    if options["mode"] == "later":
        return
    validate_listener(plan["api"], facts, root)
    if options.get("topology") != "docker":
        return
    if not facts["docker"]["available"]:
        raise ValueError(facts["docker"]["reason"] + "; повторно запустите мастер")
    selected = next((c for c in facts["n8n"] if c["name"] == options.get("docker_container")), None)
    if selected is None:
        raise ValueError("Сохранённый контейнер n8n больше не обнаружен; повторно запустите мастер подключения n8n")
    choices, reasons = environment.host_candidates(selected, facts)
    host = plan["api"]["STOLAS_LISTEN"]
    if not any(c["address"] == host for c in choices):
        raise BindingError("Локальный listener не соответствует обнаруженным сетям n8n; повторите выбор размещения. Endpoint может быть задан отдельно. " + "; ".join(reasons))
    options["docker_id"] = selected["id"]
    options["network_mode"] = selected["mode"]



def workflow(root, options):
    data = json.loads((layout.code_root(root) / "n8n/stolas.json").read_text(encoding="utf-8"))
    data["name"] = f"Stolas - {options['hours']}h monitoring"
    data["settings"]["timezone"] = options["timezone"]
    nodes = {n["name"]: n for n in data["nodes"]}
    nodes["Every 3 hours"]["parameters"]["rule"]["interval"][0]["hoursInterval"] = options["hours"]
    # JSON encoding avoids injecting user input into the JavaScript Code node.
    settings_code = "return [{json: " + json.dumps({"endpoint": options["endpoint"], "chatId": options["chat_id"], "notificationMode": options.get("notification_mode", "alerts_only"), "timezone": options["timezone"]}, ensure_ascii=False) + "}];"
    for name in ("Settings", "Summary settings"):
        nodes[name]["parameters"]["jsCode"] = settings_code
    nodes["Daily summary"]["disabled"] = options.get("notification_mode") != "daily_summary"
    nodes["Daily summary"]["parameters"]["rule"]["interval"][0]["triggerAtHour"] = options.get("summary_hour", 9)
    return data



def check_connection(root, options, api, compose):
    if "STOLAS_LISTEN" in api:
        host = "127.0.0.1" if api["STOLAS_LISTEN"] == "0.0.0.0" else api["STOLAS_LISTEN"]
        try:
            if request_json(f"http://{host}:{api['STOLAS_PORT']}", "/healthz", api["STOLAS_API_TOKEN"]).get("status") != "ready":
                raise RuntimeError("not_ready")
        except ServiceRequestError as error:
            raise IntegrationConnectionError("Локальный listener Stolas: " + str(error)) from None
        except (RuntimeError, ValueError, OSError):
            raise IntegrationConnectionError("Локальный listener Stolas не отвечает с авторизацией. Проверьте привязку IP:порта и состояние Core; endpoint n8n пока не проверен") from None
    container = options.get("docker_id") or options.get("docker_container")
    if options.get("topology") == "docker" and container:
        code = "let s='';process.stdin.on('data',x=>s+=x);process.stdin.on('end',async()=>{try{const p=JSON.parse(s);const r=await fetch(p.url+'/healthz',{headers:{Authorization:'Bearer '+p.token},redirect:'error',signal:AbortSignal.timeout(8000)});const j=await r.json();process.exit(r.status===200&&j.status==='ready'?0:1)}catch{process.exit(1)}})"
        docker = compose[:compose.index("compose")]
        try:
            result = subprocess.run(docker + ["exec", "-i", container, "node", "-e", code], cwd=root, env=clean_env(),
                                    input=json.dumps({"url": options["endpoint"], "token": api["STOLAS_API_TOKEN"]}),
                                    text=True, capture_output=True, timeout=15)
        except subprocess.TimeoutExpired:
            raise IntegrationConnectionError("Проверка endpoint из n8n превысила 15 секунд; проверьте связность и URL. Локальный listener проверен") from None
        if result.returncode:
            if result.returncode in (126, 127) or "executable file not found" in result.stderr.lower():
                raise RuntimeError("Node.js недоступен в контейнере n8n. Проверьте HTTP Request через Manual test. Файл: " + str(layout.integration_path(root, "local.json")))
            raise IntegrationConnectionError("n8n Docker не достигает endpoint /healthz. Локальный listener проверен; проверьте URL и связность из n8n; контейнер n8n не изменён")
        ui().result("API доступен из контейнера n8n")
    else:
        try:
            if request_json(options["endpoint"], "/healthz", api["STOLAS_API_TOKEN"]).get("status") != "ready":
                raise RuntimeError("not_ready")
        except ServiceRequestError as error:
            raise IntegrationConnectionError("Endpoint Stolas для n8n: " + str(error)) from None
        except (RuntimeError, ValueError, OSError):
            raise IntegrationConnectionError("Endpoint n8n недоступен с этого хоста. Локальный listener проверен; проверьте URL, DNS и связность. Доступ с другого хоста подтверждается Manual test в n8n") from None
        ui().result("API доступен с этой машины")



def connect_n8n(root, options, api, data):
    template = json.loads(json.dumps(data))
    for node in template["nodes"]:
        node.pop("credentials", None)
    template_sha = hashlib.sha256(json.dumps(template, sort_keys=True).encode()).hexdigest()
    local = layout.integration_path(root, "local.json")
    write_private(local, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    if options["mode"] == "export":
        ui().result("файл для импорта сохранён: " + str(local))
        return True
    state_path = layout.integration_path(root, "install-state.json")
    state = json.loads(state_path.read_text()) if state_path.exists() else {"url": options["url"], "credentials": []}
    if state.get("url") != options["url"]:
        raise ValueError("Сохранена другая интеграция. Используйте ручной импорт")
    if state.get("pending"):
        raise ValueError("Предыдущий запрос мог создать ресурс. Проверьте " + str(state_path) + " и импортируйте файл вручную")
    if state.get("workflow_id"):
        if state.get("workflow_timezone") == options["timezone"] and state.get("template_sha256") == template_sha:
            ui().result("workflow уже подключён; дубликат не создаётся")
            return True
        ui().result("Подготовлены новые настройки, но существующий workflow требует ручного обновления.", "warning")
        ui().line("Workflow: " + options["url"] + "/workflow/" + urllib.parse.quote(str(state["workflow_id"]), safe=""))
        ui().line("Файл: " + str(local))
        ui().line("В n8n сохраните резервную копию workflow, обновите его из файла, проверьте credentials и timezone " + options["timezone"] + ", выполните Manual test и опубликуйте изменения расписания. Новый workflow не создаётся.")
        return False
    resource = "проектов"
    try:
        projects = list_projects(options)
        if not projects:
            ui().result("Нет доступных проектов n8n. Используйте ручной импорт.", "warning")
            return False
        if len(projects) == 1:
            project = select_named("Проект n8n", projects, [p["name"] for p in projects])
        else:
            for index, project in enumerate(projects, 1):
                ui().line(f"  {index}. {project['name']}")
            index = ask("Проект n8n — номер варианта", "", integer(1, len(projects)))
            project = projects[index - 1]
        resource = "credentials выбранного проекта"
        items = list_credentials(options)
    except ServiceRequestError as error:
        ui().result("Не удалось прочитать список " + resource + ": " + str(error), "warning")
        ui().line("Файл подготовлен для ручного импорта.")
        return False
    except (RuntimeError, ValueError):
        ui().result("Список " + resource + " недоступен: проверьте права API-ключа и версию n8n. Используйте ручной импорт.", "warning")
        return False
    # Explicit ownership/sharing establishes membership; n8n enforces key permissions.
    items = [c for c in items if any(p["id"] == project["id"] for p in c["projects"])]
    telegram = credential_choice(items, "telegramApi")
    if telegram is None:
        ui().result("Telegram credential не найден. Создайте его в n8n и повторите подключение либо импортируйте файл вручную.", "warning")
        return False
    owned = next((c for c in state.get("credentials", []) if c["type"] == "httpHeaderAuth"), None)
    header = next((c for c in items if owned and c["id"] == owned["id"]), None) or credential_choice(items, "httpHeaderAuth")
    if header is None:
        if choice("Создать в n8n credential для доступа к Stolas?", ("create", "export"), "export") != "create":
            return False
    ui().line("Будет создан неактивный workflow. Telegram credential: " + telegram["name"])
    if choice("Подключить workflow?", ("connect", "export"), "connect") != "connect":
        return False
    base = options["url"] + "/api/v1"
    if header is None:
        state["pending"] = "httpHeaderAuth"
        write_private(state_path, json.dumps(state) + "\n")
        created = request_json(base, "/credentials", options["key"], {
            "name": "Stolas API", "type": "httpHeaderAuth", "projectId": project["id"],
            "data": {"name": "Authorization", "value": "Bearer " + api["STOLAS_API_TOKEN"]}}, n8n=True)
        header = {"type": "httpHeaderAuth", "id": str(created["id"]), "name": "Stolas API"}
        state["credentials"].append(header)
        state.pop("pending")
        write_private(state_path, json.dumps(state) + "\n")
    for name, kind, ref in (("Run Stolas", "httpHeaderAuth", header), ("Read summary", "httpHeaderAuth", header), ("Telegram alert", "telegramApi", telegram)):
        next(n for n in data["nodes"] if n["name"] == name)["credentials"] = {kind: {k: ref[k] for k in ("id", "name")}}
    payload = {k: data[k] for k in ("name", "nodes", "connections", "settings")}
    if project:
        payload["projectId"] = project["id"]
    state["pending"] = "workflow"
    write_private(state_path, json.dumps(state) + "\n")
    created = request_json(base, "/workflows", options["key"], payload, n8n=True)
    state["workflow_id"] = str(created["id"])
    state["workflow_timezone"] = options["timezone"]
    state["template_sha256"] = template_sha
    state.pop("pending")
    write_private(state_path, json.dumps(state) + "\n")
    ui().line("Готово: workflow создан в n8n.")
    ui().line("Статус: неактивен.")
    ui().line("Адрес: " + options["url"] + "/workflow/" + urllib.parse.quote(state["workflow_id"], safe=""))
    return True


def integrate(root, mode=None):
    original = read_api(root)
    settings = layout.integration_path(root, "settings.json")
    previous = json.loads(settings.read_text()) if settings.exists() else {}
    previous = {k: v for k, v in previous.items() if k not in ("key", "bot_token")}
    facts = environment.discover(root)
    from tools import timezones
    timezones.require(facts)
    with install_lock(root):
        try:
            ui().stage("Подключение n8n")
            selected_mode = mode or choice("Как подключить n8n?", ("export", "api"), "export")
            api = dict(original)
            options = {"mode": selected_mode, **collect_topology(api, facts, root, previous)}
            options["chat_id"] = ask("Telegram chat ID", previous.get("chat_id", ""), matching(r"-?\d+|@[a-zA-Z0-9_]{5,}"))
            options["notification_mode"] = choice("Какие сообщения отправлять?", ("alerts_only", "every_measurement", "daily_summary"), previous.get("notification_mode", "alerts_only"))
            options["hours"] = ask("Интервал измерений, часов", previous.get("hours", 3), integer(1, 23))
            from tools import timezones
            options["timezone"] = timezones.require(facts)
            ui().line("Часовой пояс ОС: " + options["timezone"] + " (" + (facts.get("timezone_source") or "ОС") + ")")
            options["summary_hour"] = ask("Час сводки", previous.get("summary_hour", 9), integer(0, 23)) if options["notification_mode"] == "daily_summary" else 9
            if selected_mode == "api":
                options["url"] = ask("Адрес n8n", previous.get("url", ""), connection_url)
                ui().line("Ключ будет виден при вставке. Он используется только сейчас и не сохраняется.")
                options["key"] = ask("API key, созданный вами в n8n", "", matching(r"[^\s]+"))
            ui().stage("Проверьте подключение")
            ui().line("Адрес Stolas: " + options["endpoint"])
            if api != original:
                ui().line("Адрес API изменится. Stolas будет перезапущен; сети n8n останутся прежними.")
            if choice("Применить настройки подключения?", ("apply", "cancel"), "apply") == "cancel":
                raise Cancel()
            validate_discovered_endpoint({"api": api, "n8n": options}, environment.discover(root), root)
            data = workflow(root, options)
            write_private(layout.integration_path(root, "local.json"), json.dumps(data, ensure_ascii=False, indent=2) + "\n")
            if layout.runtime(root):
                from tools import resources
                compose = resources.compose(root, resources.command(root))
            else:
                compose = docker_command(root)
            env_path = root / ".env"
            with env_path.open(encoding="utf-8", newline="") as file:
                old_env = file.read()
            if api != original:
                lines = [line for line in old_env.splitlines() if line.partition("=")[0] not in api]
                write_private(env_path, "\n".join(lines) + "\n" + "".join(k + "=" + v + "\n" for k, v in api.items()))
                try:
                    run(compose + ["up", "-d", "--force-recreate", "--wait", "--wait-timeout", "90", "stolas"], root)
                    check_connection(root, options, api, compose)
                except BaseException:
                    write_private(env_path, old_env)
                    run(compose + ["up", "-d", "--force-recreate", "--wait", "stolas"], root)
                    raise
            else:
                check_connection(root, options, api, compose)
            safe = {k: v for k, v in options.items() if k not in ("key", "bot_token")}
            write_private(settings, json.dumps(safe, ensure_ascii=False, indent=2) + "\n")
            connected = connect_n8n(root, options, api, data)
            if not connected:
                ui().line("Импорт: " + str(layout.integration_path(root, "local.json")))
            return 0 if connected else 2
        except (ValueError, RuntimeError) as error:
            ui().result("Подключение не завершено. Core продолжает работать.", "warning")
            if isinstance(error, (BindingError, IntegrationConnectionError, ServiceRequestError)):
                ui().line(str(error))
            ui().line("Импортируйте файл вручную: " + str(layout.integration_path(root, "local.json")))
            return 2


def main():
    parser = argparse.ArgumentParser(description="Подключить установленный Stolas Core к n8n")
    parser.add_argument("integration", choices=("n8n",))
    parser.add_argument("--mode", choices=("export", "api"))
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    try:
        return integrate(args.root, args.mode)
    except Cancel:
        ui().line("Подключение отменено.")
        return 3
    except (KeyboardInterrupt, EOFError):
        ui().line("Подключение прервано; Stolas Core продолжает работать.")
        return 130
    except (OSError, ValueError, RuntimeError, KeyError):
        ui().result("Не удалось подключить n8n. Проверьте настройки Core и права доступа; повторите команду.", "error")
        return 1


if __name__ == "__main__":
    sys.exit(main())
