"""Regenerate the importable, credential-free n8n workflow."""
import json
from pathlib import Path


def node(name, type_, version, position, parameters, **extra):
    return dict(id=name.lower().replace(" ", "-"), name=name, type="n8n-nodes-base." + type_, typeVersion=version, position=position, parameters=parameters, **extra)


nodes = [
    node("Every 3 hours", "scheduleTrigger", 1.2, [0, 0], {"rule": {"interval": [{"field": "hours", "hoursInterval": 3}]}}),
    node("Manual test", "manualTrigger", 1, [0, 180], {}),
    node("Settings", "code", 2, [240, 0], {"jsCode": "return [{json: {endpoint: 'https://stolas.example.invalid', chatId: 'REPLACE_WITH_CHAT_ID'}}];"}),
    node("Run Stolas", "httpRequest", 4.2, [480, 0], {
        "method": "POST", "url": "={{ $json.endpoint + '/v1/tests' }}",
        "authentication": "genericCredentialType", "genericAuthType": "httpHeaderAuth",
        "options": {"timeout": 610000, "response": {"response": {"responseFormat": "json"}}}
    }, onError="continueRegularOutput"),
    node("Classify result", "code", 2, [720, 0], {"jsCode": """const r = $json;
// HTTP/proxy errors are monitoring faults, never a WAN diagnosis.
if (r.error) return [{json: {text: 'Stolas: API unavailable or request rejected. Check execution details; WAN state is unknown.'}}];
if (r.wan_alert === true) {
  const p = r.primary, c = r.confirmation;
  return [{json: {text: `Stolas ${r.node}: low speed confirmed on two servers (${r.time}).\\n${p.server}: DL ${p.download.mbps}, UL ${p.upload.mbps} Mbps\\n${c.server}: DL ${c.download.mbps}, UL ${c.upload.mbps} Mbps\\nCycle: ${r.id}`}}];
}
if (['route_blocked', 'unavailable'].includes(r.status)) {
  return [{json: {text: `Stolas ${r.node}: measurement unavailable (${r.status}); WAN state is unknown. Cycle: ${r.id}`}}];
}
return []; // Normal, disagreement and unconfirmed low samples remain in Stolas history.
"""}),
    node("Telegram alert", "telegram", 1.2, [960, 0], {
        "chatId": "={{ $('Settings').first().json.chatId }}", "text": "={{ $json.text }}",
        "additionalFields": {"appendAttribution": False, "parse_mode": "HTML"}
    }),
]
# Use plain-text Telegram messages: never interpret node ids as HTML.
nodes[-1]["parameters"]["additionalFields"].pop("parse_mode")
connections = {}
for source, target in [("Every 3 hours", "Settings"), ("Manual test", "Settings"), ("Settings", "Run Stolas"), ("Run Stolas", "Classify result"), ("Classify result", "Telegram alert")]:
    connections[source] = {"main": [[{"node": target, "type": "main", "index": 0}]]}
workflow = {"name": "Stolas - 3h monitoring", "nodes": nodes, "connections": connections,
            "active": False, "settings": {"executionOrder": "v1", "timezone": "Etc/UTC"}, "pinData": {}}
Path("n8n").mkdir(exist_ok=True)
Path("n8n/stolas.json").write_text(json.dumps(workflow, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
