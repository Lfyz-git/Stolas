# Stolas

Переносимый монитор скорости IPv4 TCP на iperf3. Linux + Docker, CLI с JSON и HTTP API для локального или удалённого n8n. Лицензия MIT. Никакой зависимости от конкретного маршрутизатора, провайдера или n8n в процессе агента.

## Быстрый старт на Linux

```sh
cp .env.example .env
cp config/example.json config/local.json
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Запишите сгенерированный токен в `.env` как `STOLAS_API_TOKEN`. В `config/local.json` задайте `node`, пороги скорости и `route.expected_public_cidrs`: разрешённый внешний IPv4 основного WAN в формате `/32` либо доверенный диапазон провайдера. При возможности задайте ожидаемые `route.interface` и `route.gateway`. Не копируйте реальные адреса и секреты в example-файлы.

```sh
docker compose build
docker compose up -d
docker compose logs --tail 30
```

Пример конфигурации намеренно не допускает тест до настройки WAN (`route_unconfigured`). Для лаборатории можно явно поставить `route.mode: "off"`; такие циклы никогда не дают `wan_alert: true`. Контейнер слушает только `127.0.0.1:8080`. Compose использует host networking **на Linux**, запускает uid 10001 без capabilities, с read-only rootfs. Не задавайте публичный адрес прослушивания без TLS, firewall и ограничений reverse proxy.

Для API нужен TLS reverse proxy на хосте либо доверенный VPN/SSH-туннель. Пример Caddy с собственным DNS-именем:

```caddyfile
stolas.example.invalid {
    reverse_proxy 127.0.0.1:8080 {
        transport http {
            response_header_timeout 610s
        }
    }
}
```

Замените имя на своё. Ограничьте доступ адресами n8n или доверенной сетью, лимитируйте запросы/соединения на proxy. Токен обязателен и при TLS. Если n8n тоже в Docker, его localhost не является localhost хоста: используйте доступное ему TLS-имя/туннель. Не подключайте Stolas к чужим Compose-проектам и не меняйте существующие сервисы.

## API

Все маршруты требуют `Authorization: Bearer <token>`. GET `/healthz` проверяет работоспособность HTTP-сервиса, **не** доступность WAN/iperf. GET `/v1/results/latest` возвращает последний цикл, GET `/v1/results` — последние 100 циклов. POST `/v1/tests` запускает синхронный цикл, **без тела и параметров**. Адреса серверов нельзя передать через API. Timeout клиента должен быть больше `cycle_timeout` + 10 секунд.

```sh
curl --fail-with-body -X POST --max-time 250 \
  -H "Authorization: Bearer $STOLAS_API_TOKEN" \
  https://stolas.example.invalid/v1/tests
```

В этой команде переменная должна быть уже задана в shell; файл `.env` читается Compose, а не curl. Ответы: 401 — неверный токен, 409 — уже идёт тест, 429 — cooldown, 400 — непустое тело, 500 — внутренняя ошибка. Успешно завершённый цикл возвращает HTTP 200 даже при недоступности измерений: проверяйте `status`. Не повторяйте POST автоматически после timeout: цикл мог продолжиться; сначала прочтите latest. Одна файловая блокировка на общий каталог данных защищает от наложения CLI/API/планировщика. Не запускайте несколько независимых каталогов данных для одного канала.

## CLI и расписание

Python 3.12+, iperf3 и iproute2; Python-зависимостей из PyPI нет.

```sh
python3 -m agent validate --config config/local.json
python3 -m agent run --config config/local.json --data data
python3 -m agent history --data data
python3 -m agent schedule --config config/local.json --data data
```

`run`: exit 0 — ok, 2 — цикл сохранён с иным статусом, 1 — ошибка конфигурации/хранилища, busy или cooldown. stdout — JSON, диагностическая ошибка — JSON stderr. `schedule` запускает цикл сразу и через три часа после завершения предыдущего. Для фиксированных часов используйте n8n (каждые 3 часа) или cron. Выберите **один** планировщик; API сам по себе тесты не запускает.

Разовый запуск через работающий контейнер:

```sh
docker compose exec stolas python3 -m agent run
docker compose exec stolas python3 -m agent history
```

## Что и как измеряется

1. Обычный порядок: HOSTKEY Москва → МТС Москва → RUWEB Москва. После первого успешного полного DL/UL цикл заканчивается. HOSTKEY использует 5201–5209; МТС 3333; RUWEB 5201. По умолчанию на сервер — **два** захода: у HOSTKEY 5201 затем 5202, у однопортовых — повтор того же порта. Остальные порты доступны при увеличении `attempts_per_server` (до 9).
2. Каждый заход: download (`-R`), затем upload, IPv4 TCP, 4 потока по 10 секунд. Результат пары привязан к одному разрешённому IPv4 и порту. Пары с разных серверов не объединяются; частичные/невалидные измерения сохраняются.
3. `server_busy`, timeout, DNS и ошибки iperf — сбои измерения. Повторы ограничены числом заходов и общим deadline. Недоступность всех серверов означает `unavailable`, а не диагноз WAN.
4. Скорость ниже порога данного сервера сохраняется и проверяется следующим доступным независимым сервером. `low_confirmed` требует просадки **того же направления** на двух разных именах и адресах. Это сигнал деградации, не доказательство неисправности провайдера. Если второй сервер здоров — `server_disagreement`; если подтверждения нет — `low_unconfirmed`.
5. Каждый восьмой сохранённый цикл проверяет также резервы (около суток при интервале 3 часа). Ошибка резерва остаётся в `attempts`/`errors`, не портит успешный primary. Номер цикла переживает рестарт и очистку старой истории.

Пороговые значения — настраиваемые абсолютные Mbps для **каждого сервера и направления**, не объединённая статистическая baseline. Примерные 500/500 и 500/300 не являются универсальной нормой: настройте их под тариф и собственные наблюдения. Адаптивное обучение baseline не реализовано. Значения, полученные ранее на чужом узле, не используются как калибровка.

`mbps` берётся из `end.sum_received.bits_per_second`, TCP retransmits — из `end.sum_sent.retransmits`, если поле присутствует. Нет поля — `null`, не ноль. `latency_ms` и `latency_method` всегда `null`: iperf throughput и TCP RTT под нагрузкой не выдаются за корректную базовую задержку.

## Проверка WAN: возможности и ограничения

До и после **каждого направления** агент сверяет `ip -j -4 route get <resolved endpoint>` с заданными interface/gateway и HTTPS-проверку внешнего IPv4 с разрешёнными CIDR. DNS и guard выполняются в ограниченных по времени дочерних процессах. Proxy-переменные игнорируются для HTTPS-проверки. IPv4 endpoint фиксируется на пару DL/UL. `bind_address` передаётся в iperf3 через `-B` и учитывается при route lookup.

Это проверка локального маршрута и внешнего выхода HTTPS-пробы. Она **не доказывает**, что NAT/PBR маршрутизатора направляет iperf и HTTPS одним WAN. При PBR, нескольких source IP или VPN требуется правило на маршрутизаторе, закрепляющее весь исходящий трафик узла (или выбранного source IP) за основным WAN, без fallback на LTE. `bind_address` не привязывает HTTPS-пробу: в сложной многоканальной схеме это особенно важно. Если нужен строгий запрет расходования LTE даже во время переключения в середине теста, обеспечьте его firewall/PBR: проверки до/после не могут остановить мгновенный failover.

`route_blocked` прекращает цикл и исключает WAN-alert; измеренные до изменения маршрута значения остаются в журнале как invalid. Не задавайте слишком широкий разрешённый CIDR, включающий альтернативный WAN. При динамическом адресе используйте поддерживаемый доверенный диапазон или обновляйте локальную конфигурацию после проверки.

## Конфигурация и данные

`config/example.json` перечисляет все настройки. Файл задаётся `--config` или `STOLAS_CONFIG`. Любой верхнеуровневый ключ можно переопределить `STOLAS_<KEY>`: `STOLAS_NODE` — строка, остальные — JSON, например `STOLAS_PARALLEL=4`, `STOLAS_ROUTE='{"mode":"off"}'`, `STOLAS_BIND_ADDRESS='"192.0.2.10"'`. Env приоритетнее файла. Неизвестные ключи/невалидные диапазоны отклоняются. При смене настроек перезапустите API.

Дополнительно: `STOLAS_DATA_DIR`, `STOLAS_LISTEN`, `STOLAS_PORT`, `STOLAS_API_TOKEN`. `min_interval` по умолчанию 300 секунд, включая неудачные циклы. `process_timeout` — максимум одного iperf, `cycle_timeout` — бюджет всего цикла. `reserve_every`, `attempts_per_server`, `retry_delay`, thresholds задаются локально. История: SQLite в volume `stolas-data`, последние `history_limit` (10000) циклов. `history` экспортирует последние 100; для полного экспорта используйте SQLite backup/read-only tooling. Ошибка записи не маскируется под успешный цикл.

JSON schema version 1: `id`, `node`, UTC `time`, `status`, `wan_alert`, `parameters`, `primary`, `confirmation`, все `attempts`, `errors`, `duration_seconds`. В попытке: server, hostname/IP/port, причина (`primary`, `fallback`, `confirmation`, `reserve`), отдельные DL/UL, retransmits, длительности, проверки маршрута и ошибки. В runtime-журнале будут реальные адреса endpoints и ваш node id: относитесь к нему как к локальным эксплуатационным данным. `.env`, `config/local.json`, `data/` исключены из Git и Docker context.

## n8n → Telegram

Импортируйте `n8n/stolas.json` (неактивен). В Settings задайте TLS endpoint и chat id. В Run Stolas выберите **Header Auth credential**, имя заголовка `Authorization`, значение `Bearer <token>`. В Telegram alert выберите Telegram credential с токеном бота. Токены храните в credential store n8n, не в workflow. Выполните Manual test и только после проверки активируйте расписание.

Workflow шлёт WAN-alert только при `wan_alert: true`. Недоступность API/измерений и блокировка маршрута дают отдельное техническое сообщение с неизвестным состоянием WAN. Занятые серверы никогда не трактуются как падение канала. Неподтверждённые низкие измерения остаются в журнале. Ошибки Telegram видны как failed execution n8n; настройте наблюдение за такими executions. Workflow не выполняет дедупликацию уведомлений: повторение возможно каждые 3 часа. Не публикуйте экспорт после добавления собственных адресов/credentials.

## Проверка и воспроизводимость

```sh
python3 -m unittest discover -s tests -v
docker build -t stolas:test .
docker run --rm --read-only --tmpfs /tmp --cap-drop ALL \
  -v "$PWD/tests:/tests:ro" --entrypoint python3 stolas:test \
  -m unittest discover -s /tests -v
```

Тесты проверяют busy/fallback, низкую скорость и подтверждение, изменение маршрута, историю, блокировку, cooldown, конфигурацию, настоящий HTTP API и настоящий iperf3 на loopback. Публичные speed-серверы тестами не нагружаются. Без установленного iperf3 loopback test явно skipped; в Docker он обязателен. CI собирает production-образ и повторяет тесты внутри него.

Docker: Alpine 3.23.0 закреплён manifest digest; полный набор APK для amd64/arm64 закреплён URL, версией и SHA-256 в `config/apk.lock.json` и `.lock`. Установка идёт без обращения к текущему APKINDEX. Изменение/удаление upstream APK завершает сборку ошибкой; для долгосрочного архива храните APK в собственном неизменяемом mirror. Это воспроизводимость входных зависимостей, не обещание побитово одинакового image ID. Обновление lock: `python3 tools/lock_apk.py`, затем review diff и тестовая сборка. CI Actions также закреплены SHA.

## Источники

- [Официальные параметры iperf3](https://software.es.net/iperf/invoking.html)
- [HOSTKEY: Москва, порты 5201–5209](https://hostkey.ru/documentation/technical/exist_server_using/speedtest/)
- [n8n HTTP Request credentials](https://docs.n8n.io/integrations/builtin/credentials/httprequest/)

Публичные endpoints могут изменяться и быть заняты; их доступность из конкретной сети проверяется при эксплуатации. Совместимость с iperf3 3.16 не означает, что 3.16 используется в образе: точные версии production-зависимостей указаны в lock.
