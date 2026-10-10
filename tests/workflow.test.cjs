// Execute the exact Code nodes exported to n8n, including its Settings lookup.
const fs = require('node:fs');
const assert = require('node:assert/strict');
const workflow = JSON.parse(fs.readFileSync('n8n/stolas.json', 'utf8'));
const code = name => new Function('$json', '$', workflow.nodes.find(n => n.name === name).parameters.jsCode);
const classify = code('Classify result');
const settings = mode => name => ({first: () => ({json: {notificationMode: mode, chatId: '123'}})});
const sample = {server: 'test-server', group: 'additional', valid: true, download: {mbps: 1}, upload: {mbps: 2}};
const cycle = {node: 'test-node', time: 'test-time', id: 'cycle', primary: sample, wan_alert: false};
for (const mode of ['alerts_only', 'daily_summary']) {
  for (const status of ['ok', 'server_disagreement', 'low_unconfirmed']) {
    assert.deepEqual(classify({...cycle, status}, settings(mode)), []);
  }
}
for (const status of ['unavailable', 'route_blocked']) {
  const text = classify({...cycle, status}, settings('alerts_only'))[0].json.text;
  assert.match(text, /WAN state is unknown/);
  assert.doesNotMatch(text, /DL 1/);
}
for (const status of ['ok', 'server_disagreement', 'low_unconfirmed', 'low_confirmed', 'route_blocked', 'unavailable']) {
  const item = classify({...cycle, status}, settings('every_measurement'))[0].json;
  assert.equal(item.chatId, '123');
  assert.match(item.text, /test-node/);
  assert.match(item.text, /test-time/);
  assert.match(item.text, new RegExp(status));
}
assert.match(classify({error: 'secret-raw-error'}, settings('alerts_only'))[0].json.text, /API unavailable/);
assert.doesNotMatch(classify({error: 'secret-raw-error'}, settings('alerts_only'))[0].json.text, /secret-raw/);
assert.match(classify({...cycle, status: 'low_confirmed', wan_alert: true, confirmation: sample}, settings('alerts_only'))[0].json.text, /low speed confirmed/);
assert.match(classify({...cycle, status: 'ok'}, settings('every_measurement'))[0].json.text, /fallback: yes/);
const summary = code('Format summary');
const summarySettings = timezone => name => ({
  first: () => ({json: {chatId: '123', timezone}})
});
const daily = (data, timezone = 'Europe/Moscow') => summary(data, summarySettings(timezone))[0].json;
const normal = {
  nodes: ['azazel'],
  window_start: '2026-10-09T20:19:27.096934+00:00',
  window_end: '2026-10-10T20:19:27.096934+00:00',
  count: 6,
  measured_count: 6,
  statuses: {ok: 6},
  download_avg_mbps: 915.619,
  upload_avg_mbps: 884.864
};
const success = daily(normal);
assert.equal(success.chatId, '123');
assert.match(success.text, /Суточный отчёт/);
assert.match(success.text, /🟢 Скорость в пределах нормы/);
assert.match(success.text, /09\.10.*23:19.*10\.10.*23:19 МСК/);
assert.match(success.text, /915,6 Мбит\/с/);
assert.match(success.text, /884,9 Мбит\/с/);
assert.match(success.text, /Проблем по результатам тестов не выявлено/);
assert.doesNotMatch(success.text, /Statuses:|Average DL|Stolas daily summary/);
assert.match(daily(normal, 'Etc/UTC').text, /20:19 UTC/);

const confirmed = daily({...normal, statuses: {ok: 5, low_confirmed: 1}});
assert.match(confirmed.text, /🔴 Обнаружены проблемы/);
assert.match(confirmed.text, /Подтверждённое снижение: 1/);
assert.doesNotMatch(confirmed.text, /Проблем по результатам тестов не выявлено/);

const unconfirmed = daily({...normal, statuses: {ok: 5, low_unconfirmed: 1}});
assert.match(unconfirmed.text, /🟠 Есть отклонения/);
assert.match(unconfirmed.text, /Снижение без подтверждения: 1/);

const blocked = daily({...normal, measured_count: 0,
  download_avg_mbps: null, upload_avg_mbps: null,
  statuses: {route_blocked: 4, unavailable: 2}});
assert.match(blocked.text, /Остановлено проверкой маршрута: 4/);
assert.match(blocked.text, /Измерение не удалось: 2/);
assert.match(blocked.text, /Нет данных для расчёта/);
assert.doesNotMatch(blocked.text, /Мбит\/с/);

const empty = daily({count: 0, measured_count: 0, statuses: {}});
assert.match(empty.text, /⚪ За сутки измерений нет/);
assert.match(empty.text, /В истории за указанный период нет измерений/);
assert.doesNotMatch(empty.text, /Скорость в пределах нормы/);

const incomplete = daily({...normal, statuses: {ok: 5}});
assert.match(incomplete.text, /🟠 Есть отклонения или неполные данные/);
assert.doesNotMatch(incomplete.text, /Проблем по результатам тестов не выявлено/);

const unknown = daily({...normal, statuses: {ok: 5, experimental_status: 1}});
assert.match(unknown.text, /Неизвестный статус \(experimental_status\): 1/);
assert.doesNotMatch(unknown.text, /Проблем по результатам тестов не выявлено/);

const fail = daily({error: 'secret-token-could-leak'});
assert.match(fail.text, /Не удалось получить статистику/);
assert.doesNotMatch(fail.text, /secret-token-could-leak/);
assert.equal(workflow.nodes.find(n => n.name === 'Daily summary').disabled, true);
assert.equal(workflow.nodes.find(n => n.name === 'Read summary').parameters.method, 'GET');
assert.equal(workflow.nodes.find(n => n.name === 'Read summary').parameters.genericAuthType, 'httpHeaderAuth');
console.log('n8n notification and summary policies passed');
