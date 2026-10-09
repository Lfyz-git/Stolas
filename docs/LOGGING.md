# Логи

Stolas пишет JSON Lines в stderr. Docker собирает stdout/stderr; внешний сборщик
выбирает администратор. Отдельный сервер логов для работы агента не требуется.
На INFO успешный цикл создаёт одну итоговую запись; начало цикла видно на DEBUG.

```json
{"timestamp":"2026-10-09T14:30:00.000Z","level":"INFO","service":"stolas","node":"example-node","event":"measurement_completed","test_id":"f4e8c291","status":"ok","duration_ms":23420,"server":"primary-1","download_mbps":921.4,"upload_mbps":914.7}
```

Обязательные поля: `timestamp` (UTC), `level`, `service`, `node`, `event`.
`test_id` совпадает с `id` результата; `server` — идентификатор сервера из конфигурации.
Поля результата присутствуют там, где применимы. Схема истории SQLite не изменена.

| События | Уровень |
| --- | --- |
| `service_started`, `service_stopped`, `measurement_completed` | INFO |
| `configuration_loaded`, `measurement_started` | DEBUG |
| `server_error`, `server_unavailable`, `server_busy`, `measurement_retry` | WARNING |
| `speed_degradation_confirmed`, `api_rejected` | WARNING |
| `wan_check_failed`, `history_error`, `api_error` | ERROR |
| `application_failed` | CRITICAL |

Записи содержат только разрешённые поля и коды причин. Исключения, traceback,
сырые HTTP-запросы, Authorization, URL с секретами и содержимое конфигурации не пишутся.
Недоступность потока вывода не прерывает измерение.

## Настроить

В `.env`:

```dotenv
STOLAS_LOG_LEVEL=INFO
STOLAS_LOG_FORMAT=json
```

Уровни: DEBUG, INFO, WARNING, ERROR, CRITICAL. Форматы: json, text.
Применить: `docker compose up -d stolas`. Ротация Docker уже настроена: 3 файла по 10 MB.
Логи CLI-команды `exec` доступны вызывающему клиенту в stderr; результаты всех циклов
независимо от способа запуска сохраняются в общей SQLite.

Для автономного процесса под systemd стандартный stderr попадает в journal:

```sh
journalctl -u stolas -f -o cat
```

Юнит systemd пользователь создаёт для своего способа запуска. Отдельную службу
установщик не добавляет. Fluent Bit, Vector, Docker logging drivers и syslog через
journald могут собирать тот же поток; изменений в Core не требуется.

## Существующий Alloy → Loki

[examples/stolas.alloy](../examples/stolas.alloy) — небольшой самостоятельный фрагмент
для уже установленного Alloy на Docker-хосте. Укажите `LOKI_URL`, например
`https://logs.example.net/loki/api/v1/push`, в окружении службы Alloy. Если Loki
требует аутентификацию, задайте её штатным блоком `endpoint` Alloy, а не в Stolas.

Добавьте фрагмент в конфигурацию своего Alloy (имена компонентов не должны
совпадать с существующими), проверьте её `alloy validate <файл>` и примените
штатным для вашей установки способом. Alloy должен иметь доступ к локальному
Docker API. Фильтр читает только контейнеры с label `org.stolas.service=core`.
Для Compose v0.4.0 этот label задан автоматически.

В Grafana Explore:

```logql
{service_name="stolas"} | json | event="measurement_completed"
```

Идентификатор теста остаётся полем JSON и не превращается в Loki label с высокой
кардинальностью. Alloy и Loki установщик не устанавливает и не перенастраивает.
Существующие коллекторы сохраняют свою конфигурацию.

Пример использует стандартные компоненты из [документации Grafana Alloy](https://grafana.com/docs/alloy/latest/monitor/monitor-docker-containers/).
