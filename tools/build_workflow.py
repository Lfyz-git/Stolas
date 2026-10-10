"""Build the credential-free n8n 2.x workflow; no external modules in Code nodes."""
import json
from pathlib import Path


def node(name, type_, version, position, parameters, **extra):
    return dict(id=name.lower().replace(" ", "-"), name=name, type="n8n-nodes-base." + type_, typeVersion=version, position=position, parameters=parameters, **extra)


CLASSIFY = r"""const r = $json;
const settings = $('Settings').first().json;
const policy = settings.notificationMode || 'alerts_only';
const reply = text => [{json: {text, chatId: settings.chatId}}];
if (r.error) return reply('Stolas: API unavailable or request rejected. WAN state is unknown.');
const bad = ['route_blocked', 'unavailable'].includes(r.status);
if (policy !== 'every_measurement' && r.wan_alert !== true && !bad) return [];
const p = r.primary;
const checks = (r.attempts || []).flatMap(a => a.route_checks || []);
const failed = checks.find(c => !c.verified && c.reason !== 'explicitly_disabled');
const wan = failed ? (failed.verification_status || failed.reason || 'unknown') : checks.length ? (checks.every(c => c.verified) ? 'verified' : 'off') : 'unknown';
const measured = p && p.valid !== false && !bad && p.download && p.upload;
let localTime = 'unknown time';
try {
  if (!settings.timezone) throw new Error();
  const date = new Date(r.time);
  localTime = new Intl.DateTimeFormat('ru-RU', {timeZone: settings.timezone, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false, timeZoneName: 'shortOffset'}).format(date) + ' (' + settings.timezone + ')';
} catch { localTime = 'Часовой пояс ОС или время не определены; обновите workflow через stolas integrate n8n'; }
let text = `Stolas ${r.node || 'unknown'}\n${localTime}\nStatus: ${r.status || 'unknown'}\nWAN: ${wan}`;
if (measured) text += `\n${p.server}: DL ${p.download.mbps}, UL ${p.upload.mbps} Mbps\nGroup: ${p.group || 'legacy'}; fallback: ${p.reason === 'fallback' || ['additional', 'emergency'].includes(p.group) ? 'yes' : 'no'}`;
else text += '\nMeasurement unavailable; WAN state is unknown.';
if (r.wan_alert === true && r.confirmation) {
  const c = r.confirmation;
  text += `\nlow speed confirmed on two servers\n${c.server}: DL ${c.download.mbps}, UL ${c.upload.mbps} Mbps`;
}
text += `\nCycle: ${r.id || 'unknown'}`;
return reply(text);
"""

SUMMARY = r"""const r = $json || {};
const settings = $('Summary settings').first().json;
const reply = text => [{ json: { text, chatId: settings.chatId } }];

if (r.error) {
  return reply('🚫 Stolas · Суточный отчёт\n\nНе удалось получить статистику от Stolas. Состояние соединения неизвестно.\nПроверьте доступность API.');
}

const counts = r.statuses && typeof r.statuses === 'object' ? r.statuses : {};
const count = key => Math.max(0, Number(counts[key]) || 0);
const total = Math.max(0, Number(r.count) || 0);
const measured = Math.max(0, Number(r.measured_count) || 0);
const known = ['ok', 'low_confirmed', 'low_unconfirmed', 'server_disagreement', 'route_blocked', 'unavailable'];
const other = Object.entries(counts).filter(([key, value]) => !known.includes(key) && Number(value) > 0);
const accounted = Object.values(counts).reduce((sum, value) => sum + (Number(value) || 0), 0);
const critical = count('low_confirmed') + count('route_blocked') + count('unavailable');
const caution = count('low_unconfirmed') + count('server_disagreement');
const allOk = total > 0 && count('ok') === total && measured === total && accounted === total;

let headline;
if (total === 0) headline = '⚪ За сутки измерений нет';
else if (critical > 0) headline = '🔴 Обнаружены проблемы';
else if (caution > 0 || other.length || !allOk) headline = '🟠 Есть отклонения или неполные данные';
else headline = '🟢 Скорость в пределах нормы';

const timezone = settings.timezone;
try { new Intl.DateTimeFormat('ru-RU', {timeZone: timezone}).format(); if (!timezone) throw new Error(); }
catch { return reply('Stolas: часовой пояс ОС не настроен в workflow. Выполните stolas integrate n8n и обновите существующий workflow.'); }
const zoneLabel = timezone === 'Europe/Moscow' ? 'МСК' : timezone === 'Etc/UTC' ? 'UTC' : timezone;
const formatTime = value => {
  try {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return 'неизвестно';
    return new Intl.DateTimeFormat('ru-RU', {
      timeZone: timezone,
      day: '2-digit', month: '2-digit',
      hour: '2-digit', minute: '2-digit',
      hour12: false,
      ...(timezone === 'Europe/Moscow' || timezone === 'Etc/UTC' ? {} : {timeZoneName: 'shortOffset'})
    }).format(date);
  } catch {
    return 'неизвестно';
  }
};
const speed = value => Number(value).toLocaleString('ru-RU', {
  minimumFractionDigits: 1,
  maximumFractionDigits: 1
});
const nodes = Array.isArray(r.nodes) && r.nodes.length ? r.nodes.join(', ') : 'не указан';
const lines = [
  '📊 Stolas · Суточный отчёт',
  headline,
  '',
  'Узел: ' + nodes,
  'Период: ' + formatTime(r.window_start) + ' — ' + formatTime(r.window_end) + ' ' + zoneLabel,
  '',
  '⚡ Средняя скорость'
];

if (measured > 0 && r.download_avg_mbps != null && r.upload_avg_mbps != null &&
    Number.isFinite(Number(r.download_avg_mbps)) && Number.isFinite(Number(r.upload_avg_mbps))) {
  lines.push('⬇️ Загрузка: ' + speed(r.download_avg_mbps) + ' Мбит/с');
  lines.push('⬆️ Отдача: ' + speed(r.upload_avg_mbps) + ' Мбит/с');
} else {
  lines.push('Нет данных для расчёта.');
}

lines.push('', '📋 Результаты проверок', 'Всего: ' + total + ' · Скорость измерена: ' + measured);
for (const [key, label] of [
  ['ok', '✅ В пределах порогов'],
  ['low_confirmed', '🔴 Подтверждённое снижение'],
  ['low_unconfirmed', '🟠 Снижение без подтверждения'],
  ['server_disagreement', '⚠️ Противоречивые результаты'],
  ['route_blocked', '🚫 Остановлено проверкой маршрута'],
  ['unavailable', '🚫 Измерение не удалось']
]) {
  if (count(key)) lines.push(label + ': ' + count(key));
}
for (const [key, value] of other) {
  lines.push('⚠️ Неизвестный статус (' + key + '): ' + value);
}
if (allOk) lines.push('', 'Проблем по результатам тестов не выявлено.');
if (total === 0) lines.push('', 'В истории за указанный период нет измерений.');

lines.push('', 'ℹ️ Сводка по сохранённым измерениям за последние 24 часа. Дополнительный тест не запускался.');
return reply(lines.join('\n'));"""


def build(options=None):
    cfg = {"endpoint": "https://stolas.example.invalid", "chatId": "REPLACE_WITH_CHAT_ID", "notificationMode": "alerts_only", "timezone": "REPLACE_WITH_OS_IANA_TIMEZONE"}
    cfg.update(options or {})
    settings = {"jsCode": "return [{json: " + json.dumps(cfg, ensure_ascii=False) + "}];"}
    auth = {"authentication": "genericCredentialType", "genericAuthType": "httpHeaderAuth"}
    response = {"response": {"responseFormat": "json"}}
    nodes = [
        node("Every 3 hours", "scheduleTrigger", 1.2, [0, 0], {"rule": {"interval": [{"field": "hours", "hoursInterval": 3}]}}),
        node("Manual test", "manualTrigger", 1, [0, 180], {}),
        node("Settings", "code", 2, [240, 0], settings),
        node("Run Stolas", "httpRequest", 4.2, [480, 0], {"method": "POST", "url": "={{ $json.endpoint + '/v1/tests' }}", **auth, "options": {"timeout": 610000, "response": response}}, onError="continueRegularOutput"),
        node("Classify result", "code", 2, [720, 0], {"jsCode": CLASSIFY}),
        node("Daily summary", "scheduleTrigger", 1.2, [0, 400], {"rule": {"interval": [{"field": "days", "daysInterval": 1, "triggerAtHour": 9, "triggerAtMinute": 0}]}}, disabled=True),
        node("Summary settings", "code", 2, [240, 400], settings),
        node("Read summary", "httpRequest", 4.2, [480, 400], {"method": "GET", "url": "={{ $json.endpoint + '/v1/summary/daily' }}", **auth, "options": {"timeout": 30000, "response": response}}, onError="continueRegularOutput"),
        node("Format summary", "code", 2, [720, 400], {"jsCode": SUMMARY}),
        node("Telegram alert", "telegram", 1.2, [960, 0], {"chatId": "={{ $json.chatId }}", "text": "={{ $json.text }}", "additionalFields": {"appendAttribution": False}}),
    ]
    connections = {}
    for source, target in [("Every 3 hours", "Settings"), ("Manual test", "Settings"), ("Settings", "Run Stolas"), ("Run Stolas", "Classify result"), ("Classify result", "Telegram alert"), ("Daily summary", "Summary settings"), ("Summary settings", "Read summary"), ("Read summary", "Format summary"), ("Format summary", "Telegram alert")]:
        connections[source] = {"main": [[{"node": target, "type": "main", "index": 0}]]}
    return {"name": "Stolas - 3h monitoring", "nodes": nodes, "connections": connections,
            "active": False, "settings": {"executionOrder": "v1", "timezone": "Etc/UTC"}, "pinData": {}}


if __name__ == "__main__":
    Path("n8n").mkdir(exist_ok=True)
    Path("n8n/stolas.json").write_text(json.dumps(build(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
