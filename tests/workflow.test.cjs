// Test the actual embedded n8n classification code without a live n8n account.
const fs = require('node:fs');
const assert = require('node:assert/strict');
const workflow = JSON.parse(fs.readFileSync('n8n/stolas.json', 'utf8'));
const classify = new Function('$json', workflow.nodes.find(n => n.name === 'Classify result').parameters.jsCode);
for (const status of ['ok', 'server_disagreement', 'low_unconfirmed']) {
  assert.deepEqual(classify({status, wan_alert: false}), []);
}
for (const status of ['unavailable', 'route_blocked']) {
  const result = classify({status, node: 'test-node', id: 'test', wan_alert: false});
  assert.match(result[0].json.text, /WAN state is unknown/);
}
assert.match(classify({error: 'timeout'})[0].json.text, /API unavailable/);
const sample = {server: 'test-server', download: {mbps: 1}, upload: {mbps: 2}};
assert.match(classify({wan_alert: true, node: 'test-node', time: 'test-time', id: 'test', primary: sample, confirmation: sample})[0].json.text, /low speed confirmed/);
console.log('n8n classification: 7 cases passed');
