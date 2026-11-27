# Перенос Orbita и переинициализация Langfuse — 26.11.2026

## Результат

- Docker Desktop работает через WSL 2.
- Основная база Orbita: `orbita_database`, контейнер `orbita-db-1`, healthy.
- Все 26 пользовательских таблиц: количество строк и отпечатки содержимого
  совпали с VMM — PASS.
- `orbita_backups`: один файл, SHA-256 совпал с исходным архивом — PASS.
- PostgreSQL запущен из сохранённого исходного образа, без обновления версии.
- Торговое приложение Orbita не запускалось; его работоспособность после
  переноса — NOT_RUN. Это приёмка переноса базы, а не торговой системы.
- Старые volumes Langfuse удалены в WSL 2 и VMM по разрешению владельца.
- Новый Langfuse: `aim-setupaiv2`, `fortune`, `orbita`; ключи проектов проверены.
- DeerFlow v2.2.0: оба агента ответили через OpenAI, observations сохранены
  и прочитаны из Langfuse. Ключи другого проекта не видят эти observations.
- Два модульных теста DeerFlow — PASS (TEMP направлен в `.scratch/test-tmp`).
- Перезагрузка Windows и визуальная приёмка интерфейсов — NOT_RUN.

## Локальные доказательства и резервные копии

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
