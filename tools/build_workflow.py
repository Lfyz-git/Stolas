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
let text = `Stolas ${r.node || 'unknown'}\n${r.time || 'unknown time'}\nStatus: ${r.status || 'unknown'}\nWAN: ${wan}`;
if (measured) text += `\n${p.server}: DL ${p.download.mbps}, UL ${p.upload.mbps} Mbps\nGroup: ${p.group || 'legacy'}; fallback: ${p.reason === 'fallback' || ['additional', 'emergency'].includes(p.group) ? 'yes' : 'no'}`;
else text += '\nMeasurement unavailable; WAN state is unknown.';
if (r.wan_alert === true && r.confirmation) {
  const c = r.confirmation;
  text += `\nlow speed confirmed on two servers\n${c.server}: DL ${c.download.mbps}, UL ${c.upload.mbps} Mbps`;
}
text += `\nCycle: ${r.id || 'unknown'}`;
return reply(text);
"""

SUMMARY = r"""const r = $json;
const settings = $('Summary settings').first().json;
let text;
if (r.error) text = 'Stolas: daily summary unavailable. Check API connectivity.';
else {
  text = `Stolas daily summary: ${(r.nodes || []).join(', ') || 'no measurements'}\n${r.window_start} — ${r.window_end}\nCycles: ${r.count}; valid measurements: ${r.measured_count}\nStatuses: ${JSON.stringify(r.statuses || {})}`;
  if (r.measured_count > 0 && r.download_avg_mbps != null && r.upload_avg_mbps != null) text += `\nAverage DL ${r.download_avg_mbps}, UL ${r.upload_avg_mbps} Mbps`;
  else text += '\nNo valid speeds in retained history.';
  text += '\nPreceding 24 hours of retained history; no speed test was started.';
}
return [{json: {text, chatId: settings.chatId}}];
"""


def build(options=None):
    cfg = {"endpoint": "https://stolas.example.invalid", "chatId": "REPLACE_WITH_CHAT_ID", "notificationMode": "alerts_only"}
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
