# Перенос Orbita и переинициализация Langfuse — 26.11.2026

## Результат

- Docker Desktop работает через WSL 2.
- Основная база Orbita: `orbita_database`, контейнер `orbita-db-1`, healthy.
- Все 26 пользовательских таблиц: количество строк и отпечатки содержимого
  совпали с VMM — PASS.
- `orbita_backups`: один файл, SHA-256 совпал с исходным архивом — PASS.
- PostgreSQL запущен из сохранённого исходного образа, без обновления версии.
- На момент переноса базы торговое приложение Orbita не запускалось — NOT_RUN.
  По сообщению владельца от 26.11.2026, приложение пересобрали и оно работает;
  перенос старого `orbita-ui149-app:latest` не требуется. См. обновление ниже.
- Старые volumes Langfuse удалены в WSL 2 и VMM по разрешению владельца.
- Новый Langfuse: `aim-setupaiv2`, `fortune`, `orbita`; ключи проектов проверены.
- DeerFlow v2.2.0: оба агента ответили через OpenAI, observations сохранены
  и прочитаны из Langfuse. Ключи другого проекта не видят эти observations.
- Два модульных теста DeerFlow — PASS (TEMP направлен в `.scratch/test-tmp`).
- Визуальная приёмка интерфейсов была NOT_RUN при первоначальной проверке;
  26.11.2026 владелец сообщил, что её выполнили.

## Локальные доказательства и резервные копии

### Проверка после перезагрузки Windows

Повторная проверка 26.11.2026 после сообщения владельца о перезагрузке:

- Docker Engine доступен, `UseLibkrun=false`; нужные контейнеры уже запущены.
- Orbita PostgreSQL: healthy, совпадение всех 26 таблиц с исходными
  отпечатками — PASS.
- Langfuse и два DeerFlow: HTTP 200, прежние учётные записи, чаты и
  observations доступны — PASS.
- Новые синтетические вызовы OpenAI для Fortune и Orbita — PASS;
  новые observations прочитаны из соответствующих проектов Langfuse.
- Изоляция чатов, cookie и области доступа Langfuse — PASS.
- На момент этой повторной проверки приложение Orbita не запускалось: прежнего
  образа `orbita-ui149-app:latest` в WSL 2 не было, контейнер приложения
  отсутствовал. Образ `orbita-runner:prototype` не подставлялся.
- Визуальная проверка браузером на момент этой повторной проверки — NOT_RUN.

### Обновление после проверки владельцем

После повторной проверки владелец сообщил, что визуальную проверку выполнили,
образ приложения Orbita пересобрали и приложение сейчас работает. Перенос
старого `orbita-ui149-app:latest` не требуется. Эти сведения отмечены как
подтверждение владельца; приложение повторно не запускалось в рамках этого
отчёта.

Локальные результаты: `.scratch/deerflow/post-reboot-persistence.json`,
`acceptance.json`, `langfuse-acceptance.json` в том же каталоге. Предыдущий
отчёт вызовов сохранён в `pre-reboot-acceptance.json`.

В `.scratch/docker-migration-20261126/` сохранены:

- `DockerDesktop.vhdx` и SHA-256: полная копия остановленного VMM, около 25,8 GiB;
- `volumes/orbita_database.tgz`, `volumes/orbita_backups.tgz`, manifest с SHA-256;
- `orbita.dump`: логическая копия `pg_dump -Fc`, проверенная `pg_restore --list`;
- `orbita-db-image.tar` и SHA-256;
- `source-table-fingerprints.json`, `target-table-fingerprints.json`, `verification.json`;
- `orbita-db-compose.json`: локальная конфигурация базы, содержит секреты.

Доказательства DeerFlow: `.scratch/deerflow/acceptance.json` и
`.scratch/deerflow/langfuse-acceptance.json`. Эти каталоги игнорируются Git.
Не удалять их при очистке временных файлов. Копия на том же диске не защищает
от отказа носителя; резервные материалы следует хранить и на другом носителе.

## Запуск после включения ПК

Запустить Docker Desktop с WSL 2, дождаться готовности engine, затем:

```powershell
cd C:\Git\setupAIv2
docker compose -f .scratch/docker-migration-20261126/orbita-db-compose.json up -d --pull never --wait --wait-timeout 120
docker compose --env-file infra/langfuse/.env -f infra/langfuse/compose.yaml up -d --pull never --wait --wait-timeout 240
uv run --offline --no-sync python tools/deerflow_platform.py start
```

Первая команда запускает только PostgreSQL Orbita. Не выполняйте автоматически
запуск полного Compose торгового приложения: его подключение и рабочий режим
требуют отдельной проверки. База не публикует порт на Windows.

## Перед выключением ПК

Дождаться завершения задач DeerFlow, затем:

```powershell
cd C:\Git\setupAIv2
uv run --offline --no-sync python tools/deerflow_platform.py stop
docker compose --env-file infra/langfuse/.env -f infra/langfuse/compose.yaml stop --timeout 60
docker compose -f .scratch/docker-migration-20261126/orbita-db-compose.json stop --timeout 60
```

Затем выйти из Docker Desktop и штатно выключить Windows. Не использовать
`down -v` или очистку volumes. Для реальных вызовов OpenAI нужен интернет.

## Сохранённые данные VMM

Исходный VMM-диск, его полная копия, preview/wayfinder volumes Orbita,
данные других проектов и общий build-кэш сохранены. Общий экспорт всех volumes
и очистка всего build-кэша были отклонены автоматической проверкой разрешений
как выходящие за согласованную область. Выполнен только узкий перенос основной
базы и резервных копий Orbita. Удалять весь VMM-диск по этому отчёту нельзя.

## Приёмка и восстановление истории Langfuse — 26.11.2026

### Проверки Orbita после переноса

| № | Проверка | Результат |
|---:|---|---|
| 1 | `GET /api/public/projects` с ключами Orbita | PASS: ключ видит проект `orbita` на `http://localhost:3000`; конфигурация клиента поддерживает `localhost` и `127.0.0.1`. |
| 2 | Две известные трассы одного чата | PASS: разные trace ID, общий session ID, по четыре наблюдения и сохранённые тексты. [Первый ход](http://localhost:3000/project/orbita/traces/7e99bd455c91d659224ac7c24f2c2508), [второй ход](http://localhost:3000/project/orbita/traces/afbcb846c30001954ba7c3e88f8d121b). |
| 3 | `uv run --offline --no-sync orbita-check --mode local` | PASS: `local; graph and validated result; trace persisted`. Trace ID: `6b3ad00b80aedb447d8538344fcd7c48`. [Трасса](http://localhost:3000/project/orbita/traces/6b3ad00b80aedb447d8538344fcd7c48). |
| 4 | Автоматические hooks Codex и Context7 в двух ходах | PASS: две трассы одной сессии; каждая содержит по одному `codex.user`, `mcp__context7__resolve_library_id`, `mcp__context7__query_docs` и `codex.assistant`. [Первый ход](http://localhost:3000/project/orbita/traces/a4317f4ab4c1bbf6ba84439aec9bbb0b), [второй ход](http://localhost:3000/project/orbita/traces/7a8a7a1472ddf0f3bc25787fb58c6a14). Тексты сообщений и результаты инструментов прочитаны обратно. |

### Проекты общего Langfuse

Каждая собственная пара ключей возвращает ровно свой проект через `GET /api/public/projects`: `aim_setupaiv2`, `fortune`, `orbita`. Полные названия ключей/секреты в отчёт не записывались.

Импорт выполнен через `POST /api/public/otel/v1/traces` с project-scoped Basic Auth и заголовком `x-langfuse-ingestion-version: 4`, как указано в [документации Langfuse OTEL](https://langfuse.com/integrations/native/opentelemetry#opentelemetry-endpoint).

Из доступной старой копии ClickHouse восстановлены 85 исторических trace ID и 214 уникальных spans. Исходная выборка содержала 215 событий; один span имел две версии с разными `updated_at`, для импорта выбрана последняя версия. Восстановлены только отсутствующие записи; восемь spans двух известных трасс Orbita уже были в новом экземпляре и совпали при сверке.

| Проект | Восстановлено: трассы / spans | Всего после импорта: трассы / наблюдения | До 26.11.2026: трассы / наблюдения |
|---|---:|---:|---:|
| `aim_setupaiv2` | 6 / 18 | 6 / 18 | 6 / 18 |
| `fortune` | 11 / 33 | 15 / 89 | 11 / 33 |
| `orbita` | 66 / 155 | 83 / 248 | 68 / 163 |

Все 214 исходных spans прочитаны обратно через API. Расхождений по trace/span ID, типу, имени, родительской связи, input/output, времени, session ID и trace name не обнаружено. Примеры восстановленных трасс: [aim_setupaiv2](http://localhost:3000/project/aim-setupaiv2/traces/11bbcc890b1c1557029bf8dbb29eadba), [Fortune](http://localhost:3000/project/fortune/traces/1c4990c6bfdede5053afc72d089a0789), [Orbita](http://localhost:3000/project/orbita/traces/0174ed48384758f57ecc25b30c2a13e2).

В 44 spans родительский span ID ссылался на запись, не представленную внутри той же трассы в доступной старой копии. Ссылку сохранили как атрибут `migration.source_parent_span_id`, сами spans импортировали как корневые. В трёх трассах поэтому несколько корневых наблюдений.

### Ограничение полноты исходной истории

Доступная старая копия VMM содержит невоспроизведённый ext4-журнал, а в клонированной старой PostgreSQL оставалась ожидающая фоновая миграция Langfuse v4 `20260701_v4_step_5_drop_pid_tid_sorting_tables`. Поэтому таблица выше подтверждает восстановление всех найденных в этой копии данных, но не доказывает полноту первоначального Langfuse. Для полной гарантии нужен отдельный чистый снимок исходного хранилища и сверка исторических счётчиков по проектам и периоду.

Recovery-контейнеры остановлены; рабочий Langfuse остался запущен. Полный локальный экспорт с текстами трасс и манифест backfill находятся в игнорируемом Git каталоге `.scratch/langfuse-recovery-20261126/`; экспорт содержит исторические payloads и не должен попадать в Git.
