"""Host OS timezone discovery and human presentation; storage stays UTC."""
import datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def validate(name):
    if not name or name.startswith(("+", "-")) or name == "MSK":
        raise ValueError("Нужен IANA timezone, например Europe/Moscow или Europe/Berlin; сокращение/смещение не определяет правила DST")
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        try:
            ZoneInfo("Europe/Moscow")
        except ZoneInfoNotFoundError:
            raise ValueError("Недоступна база IANA: установите tzdata (Ubuntu: sudo apt-get install tzdata)") from None
        raise ValueError("Неизвестный IANA timezone: " + name + "; пример: Europe/Moscow или Europe/Berlin") from None
    return name


def discover(system=Path("/")):
    """Ignore process TZ. A copied localtime must agree with OS name metadata."""
    local = system / "etc/localtime"
    try:
        resolved = local.resolve()
        if local.is_symlink() and "/zoneinfo/" in resolved.as_posix():
            name = resolved.as_posix().split("/zoneinfo/", 1)[1]
            validate(name)
            return {"name": name, "source": "/etc/localtime", "error": None}
        metadata = system / "etc/timezone"
        if metadata.is_file():
            name = metadata.read_text(encoding="utf-8").strip()
            validate(name)
            zonefile = system / "usr/share/zoneinfo" / name
            if local.exists() and (not zonefile.is_file() or local.read_bytes() != zonefile.read_bytes()):
                raise ValueError("/etc/timezone не соответствует /etc/localtime; проверьте настройки часового пояса ОС")
            return {"name": name, "source": "/etc/timezone", "error": None}
        raise ValueError("Не удалось определить IANA timezone ОС по /etc/localtime и /etc/timezone; проверьте timedatectl и пакет tzdata")
    except (OSError, ValueError) as error:
        message = str(error) if isinstance(error, ValueError) else "Не читаются настройки часового пояса ОС; проверьте /etc/localtime, /etc/timezone и права доступа"
        return {"name": None, "source": None, "error": message}


def require(facts=None):
    info = discover() if facts is None else {"name": facts.get("timezone"), "error": facts.get("timezone_error")}
    if not info["name"]:
        raise ValueError(info.get("error") or "Не удалось определить IANA timezone ОС; проверьте timedatectl и tzdata")
    return validate(info["name"])


def display(value, name):
    moment = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError("Временная метка не содержит смещение UTC")
    text = moment.astimezone(ZoneInfo(validate(name))).strftime("%Y-%m-%d %H:%M:%S %Z %z")
    if name == "Europe/Moscow":
        text = text.replace(" MSK ", " МСК ")
    return text + " (" + name + ")"


def history(rows, name):
    lines = []
    for row in rows:
        lines.append(display(row["time"], name) + " | " + row.get("node", "?") + " | " + row["status"])
        sample = row.get("primary")
        if sample and sample.get("valid") and sample.get("download") and sample.get("upload"):
            lines.append(f"  {sample['server']}: DL {sample['download']['mbps']} / UL {sample['upload']['mbps']} Mbps")
        lines.append("  ID: " + row["id"])
    return "\n".join(lines) if lines else "История измерений пуста."
