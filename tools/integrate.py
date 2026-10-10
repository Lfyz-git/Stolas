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
import subprocess
import urllib.parse
from tools.install import (ask, choice, select_named, propose_port, url, matching,
    integer, timezone, https_url, read_api, clean_env, write_private, request_json,
    docker_command, run, Back, Cancel, Rescan)
from tools import environment, layout
from tools.terminal import ui
from tools.deploy import install_lock

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



def collect_topology(api, facts=None, root=ROOT, previous=None):
    facts = facts if facts is not None else environment.discover(root)
    previous = previous or {}
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
        ui().line("Выберите интерфейс заранее настроенного доверенного VPN.")
        selected = select_named("Интерфейс VPN", candidates, [f"{item['interface']} ({item['address']})" for item in candidates], automatic=False)
        api["STOLAS_LISTEN"] = selected["address"]
    else:
        api["STOLAS_LISTEN"] = "127.0.0.1"
    propose_port(api, facts, root)
    if topology in ("lan", "proxy"):
        print("Нужен ваш уже настроенный HTTPS reverse proxy. Stolas будет доступен proxy на 127.0.0.1:" + api["STOLAS_PORT"] + ".")

        options["endpoint"] = ask("Ваш HTTPS адрес Stolas на reverse proxy", previous.get("endpoint", ""), https_url)
    else:
        options["endpoint"] = f"http://{api['STOLAS_LISTEN']}:{api['STOLAS_PORT']}"
        connection_url(options["endpoint"], "native" if options.get("network_mode") == "host" else topology)
        print("Адрес Stolas для n8n сформирован автоматически:", options["endpoint"])
    return options


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
                identifier = shared.get("projectId") or project.get("id") or shared.get("id")
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



def workflow(root, options):
    data = json.loads((layout.code_root(root) / "n8n/stolas.json").read_text(encoding="utf-8"))
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
        raise ValueError("Предыдущий запрос мог создать ресурс. Проверьте n8n/install-state.json и импортируйте файл вручную")
    if state.get("workflow_id"):
        ui().result("workflow уже подключён; дубликат не создаётся")
        return True
    try:
        items = list_credentials(options)
    except (RuntimeError, ValueError):
        ui().result("n8n не разрешил чтение списка credentials. Файл подготовлен для ручного импорта.", "warning")
        return False
    telegram = credential_choice(items, "telegramApi")
    if telegram is None:
        ui().result("Telegram credential не найден. Создайте его в n8n и повторите подключение либо импортируйте файл вручную.", "warning")
        return False
    owned = next((c for c in state.get("credentials", []) if c["type"] == "httpHeaderAuth"), None)
    header = next((c for c in items if owned and c["id"] == owned["id"]), None) or credential_choice(items, "httpHeaderAuth")
    if header is None:
        if choice("Создать в n8n credential для доступа к Stolas?", ("create", "export"), "export") != "create":
            return False
    projects = {p["id"]: p for p in telegram["projects"]}
    if header:
        header_projects = {p["id"] for p in header["projects"]}
        projects = {k: v for k, v in projects.items() if k in header_projects}
        if telegram["projects"] and not projects:
            ui().result("Credentials находятся в разных проектах. Выберите общий доступ в n8n и импортируйте файл вручную.", "warning")
            return False
    project = select_named("Проект workflow", list(projects.values()), [p["name"] for p in projects.values()]) if projects else None
    if not project:
        ui().result("n8n не сообщил проекты credentials. Безопасная автоматическая привязка невозможна; используйте ручной импорт.", "warning")
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
    state.pop("pending")
    write_private(state_path, json.dumps(state) + "\n")
    ui().result("неактивный workflow создан")
    ui().line(options["url"] + "/workflow/" + urllib.parse.quote(state["workflow_id"], safe=""))
    return True


def integrate(root, mode=None):
    original = read_api(root)
    settings = layout.integration_path(root, "settings.json")
    previous = json.loads(settings.read_text()) if settings.exists() else {}
    previous = {k: v for k, v in previous.items() if k not in ("key", "bot_token")}
    facts = environment.discover(root)
    with install_lock(root):
        while True:
            try:
                ui().stage("Подключение n8n")
                selected_mode = mode or choice("Как подключить n8n?", ("export", "api"), "export")
                api = dict(original)
                options = {"mode": selected_mode, **collect_topology(api, facts, root, previous)}
                options["chat_id"] = ask("Telegram chat ID", previous.get("chat_id", ""), matching(r"-?\d+|@[a-zA-Z0-9_]{5,}"))
                options["notification_mode"] = choice("Какие сообщения отправлять?", ("alerts_only", "every_measurement", "daily_summary"), previous.get("notification_mode", "alerts_only"))
                options["hours"] = ask("Интервал измерений, часов", previous.get("hours", 3), integer(1, 23))
                options["timezone"] = ask("Часовой пояс", previous.get("timezone", facts.get("timezone") or "Etc/UTC"), timezone)
                options["summary_hour"] = ask("Час сводки", previous.get("summary_hour", 9), integer(0, 23)) if options["notification_mode"] == "daily_summary" else 9
                if selected_mode == "api":
                    options["url"] = ask("Адрес n8n", previous.get("url", ""), url)
                    options["key"] = ask("API key, созданный вами в n8n", "", matching(r"[^\s]+"), secret=True)
                ui().stage("Проверьте подключение")
                ui().line("Адрес Stolas: " + options["endpoint"])
                if api != original:
                    ui().line("Адрес API изменится. Stolas будет перезапущен; сети n8n останутся прежними.")
                if choice("Применить настройки подключения?", ("apply", "cancel"), "apply") == "cancel":
                    raise Cancel()
                validate_discovered_endpoint({"api": api, "n8n": options}, environment.discover(root))
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
                ui().line("Импорт: n8n/local.json. Выберите Header Auth и Telegram credential в n8n.")
                ui().line("Выполните Manual test; затем включите расписание.")
                return 0 if connected else 2
            except Back:
                continue
            except Rescan:
                facts = environment.discover(root)
            except (ValueError, RuntimeError):
                ui().result("Подключение не завершено. Core продолжает работать. Импортируйте n8n/local.json вручную или повторите команду.", "warning")
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
