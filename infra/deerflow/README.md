# DeerFlow для Fortune и Orbita

Общая установка DeerFlow **v2.2.0**, commit
`58fc5bd5b8e6ea46538e9481ba06ca2a46e14e2a`, на Docker Desktop **WSL 2**.
Образы backend, frontend и nginx закреплены по digest в `images.json`.
Два отдельных runtime используют одни образы, но разные сети, базы, память,
учётные записи и рабочие каталоги. TENDIS не подключается.

## Адреса и вход

- Fortune: <http://fortune.localhost:2026>
- Orbita: <http://orbita.localhost:2027>

Используйте разные имена хоста: cookies браузера не разделяются по номеру
порта. Chromium-браузеры разрешают `*.localhost` в loopback. Если ваш браузер
не разрешает эти имена, используйте `http://localhost:2026` и
`http://localhost:2027` в **разных профилях браузера**, иначе входы будут
перезаписывать cookie друг друга. Порты опубликованы только на 127.0.0.1.

Учётные данные находятся локально (не в Git):

- `.scratch/deerflow/fortune/credentials.json`
- `.scratch/deerflow/orbita/credentials.json`

Для входа нужны поля `email` и `password`. Остальные поля — служебные секреты.
Не отправляйте содержимое этих файлов в чат или систему задач.

## Запуск после включения ПК

1. Запустить Docker Desktop и дождаться готовности WSL 2 engine.
2. Выполнить в PowerShell:

```powershell
cd C:\Git\setupAIv2
docker compose --env-file infra/langfuse/.env -f infra/langfuse/compose.yaml up -d --pull never --wait --wait-timeout 240
uv run --offline --no-sync python tools/deerflow_platform.py start
uv run --offline --no-sync python tools/deerflow_platform.py status
```

Запуск использует уже загруженные образы (`--pull never`), без сборки,
обновления пакетов и обращения к реестрам. Вызовы OpenAI требуют интернет.

Перед выключением ПК:

```powershell
cd C:\Git\setupAIv2
uv run --offline --no-sync python tools/deerflow_platform.py stop
```

Затем при необходимости остановить Langfuse по его отдельной инструкции,
выйти из Docker Desktop и выключить Windows штатно. `stop` сохраняет данные.
Не удалять `.scratch/deerflow`, несмотря на название каталога: здесь теперь
находится постоянное локальное состояние установки. Не использовать `down -v`.

## Что доступно агентам

Созданы `fortune-developer` и `orbita-developer`. Каждому доступна отдельная
рабочая копия в `/mnt/project`, подготовленная из **HEAD** соответствующего
репозитория. Неотслеживаемые и незакоммиченные изменения не копируются.
Файлы `.env` (кроме примера), закрытые ключи типовых расширений и symlink
не переносятся. Это фильтр, а не полноценный сканер секретов в истории Git.

Изменения агента остаются в этой копии; исходные `C:\Git\Fortune` и
`C:\Git\orbita` не смонтированы. Нет автоматического применения, commit или
push. Повторный `prepare` не обновляет снимок и не затирает его изменения.
Исходный SHA записан рядом в `source-revision.txt`.

Начальный профиль разрешает только файловые инструменты. Shell, Docker socket,
MCP-серверы и внешняя публикация не предоставлены. `LocalSandboxProvider`
работает **внутри отдельного контейнера проекта**, с `allow_host_bash: false`.
Не включать bash в этом профиле: для выполнения недоверенного кода требуется
отдельная исполнительная песочница, не содержащая секреты Gateway.
Это ограниченный рабочий профиль, не приёмка полного автономного разработчика.

Общий каталог `.scratch/deerflow/shared-skills` смонтирован только для чтения.
Изначально он пуст: навыки разработчика с Windows автоматически не импортируются.
Доступ к каждому добавляемому runtime-навыку следует проверять отдельно.

## Память, трассы и резервные копии

Данные каждого runtime лежат в `.scratch/deerflow/<project>/data`, SQLite —
в `data/database`. Хранение контрольных точек и событий включено. Память
включена, но качество извлечения и отсутствие смыслового смешения знаний
на длинных диалогах требуют отдельной приёмки.

OpenAI использует уже существующий ключ из корневого `.env`. Значение копируется
в локальное окружение каждого runtime; после смены исходного ключа нужны
`prepare` и `start`. Пароли и ключи шифрования повторно не генерируются.

Langfuse использует ключи соответствующего проекта из его `.env`.
26.11.2026 Langfuse заново инициализирован с разрешения владельца: старая
история удалена. Созданы отдельные проекты `aim-setupaiv2`, `fortune`, `orbita`;
существующие ключи Fortune/Orbita сохранены. Проверены область доступа ключей,
реальные ответы модели и чтение сохранённых observations через API Langfuse.
Трассировка включена для обоих runtime. Отчёт (локально):
`.scratch/deerflow/langfuse-acceptance.json`.

После переинициализации Langfuse используйте `tools/provision_langfuse_projects.py`
для создания проектов с существующими ключами, затем `prepare/start`.
Скрипт перезапускает только web Langfuse; не запускать во время активных задач.
Маркер `tracing-verified` создаётся только после проверки доступа к проекту.

Для резервной копии остановить DeerFlow и сохранить весь `.scratch/deerflow`
(особенно `data`, `workspace`, `credentials.json`, `.env`) на другой носитель.
Секреты и данные нужны вместе. Репозиторий Git не является этой копией.

## Проверки

```powershell
uv run --offline --no-sync python -m unittest discover -s tests -p test_deerflow_platform.py -v
uv run --offline --no-sync python tools/verify_deerflow.py
# Дополнительно: платные задачи OpenAI, по одной на проект
uv run --offline --no-sync python tools/verify_deerflow.py --live
```

Проверка создаёт синтетические чаты, проверяет вход, отказ чтения чужого чата,
отказ чужой cookie и штатные ограничения файловых путей. Отчёт:
`.scratch/deerflow/acceptance.json`. Без `--live` модель не вызывается.
При `--live` возможны дополнительные служебные вызовы модели для заголовка
или памяти; это не ограничение ровно одним API-вызовом.
HTTP health и эти проверки не являются аудитом безопасности или проверкой GUI.

Приёмка 26.11.2026: два модульных теста PASS; для обоих проектов вход,
изоляция чатов и сессий, запрет служебных файлов, синтетический ответ OpenAI,
сохранение ответа после stop/start и HTTP-интерфейс — PASS.
Перезагрузка Windows и визуальная проверка браузером — NOT_RUN.

## Восстановление установки на этом ПК

Сохранить upstream в `.scratch/deerflow/upstream` на указанном commit. Скрипт
`prepare` проверяет SHA. Он генерирует конфигурацию и Compose, не запускает сервисы:

```powershell
uv run --offline --no-sync python tools/deerflow_platform.py prepare
docker compose -f .scratch/deerflow/compose.json config --quiet
uv run --offline --no-sync python tools/deerflow_platform.py start
uv run --offline --no-sync python tools/deerflow_platform.py initialize
```

Образы предварительно загружаются по ссылкам из `images.json`. `initialize`
создаёт администратора только для пустой установки, а существующего проверяет
входом. Не печатать полный `docker compose config`: он содержит секреты.

## Старое хранилище Docker VMM

26.11.2026 основные volumes `orbita_database` и `orbita_backups` перенесены
в WSL 2. Совпали отпечатки содержимого всех 26 таблиц и хеш файла резервной
копии. Торговое приложение не запускалось. Старые volumes Langfuse удалены
из обоих backend с разрешения владельца.

Остальные VMM-данные и общий build-кэш сохранены. Полный исходный диск и его
проверенная резервная копия не удалены: там остаются данные других проектов.
Подробности и команды базы: [отчёт миграции](../../docs/validation/docker-wsl2-migration-20261126.md).

Источники: [DeerFlow v2.2.0](https://github.com/bytedance/deer-flow/releases/tag/v2.2.0),
[Compose upstream](https://github.com/bytedance/deer-flow/blob/v2.2.0/docker/docker-compose.yaml),
[безопасность sandbox](https://github.com/bytedance/deer-flow/blob/v2.2.0/backend/docs/CONFIGURATION.md).
