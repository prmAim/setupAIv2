# Langfuse для локальной разработки

Compose адаптирован из `langfuse/langfuse` commit
`da4b60578892a38dafd86a0f59fa37a31e137b7c`. Все шесть образов закреплены
по digest. Web/worker Langfuse 4 используют PostgreSQL 17, ClickHouse,
Redis 7 и SeaweedFS. Python SDK закреплён отдельно в корневом `uv.lock`.

SeaweedFS выбран после отказа в загрузке upstream-образа MinIO.
`weed mini` создаёт bucket `langfuse` с `S3_SECRET_ACCESS_KEY` из локального `.env`.

## Команды из корня репозитория

```powershell
uv run --locked python tools/init_local_observability.py
docker compose --env-file infra/langfuse/.env -f infra/langfuse/compose.yaml config --quiet
docker compose --env-file infra/langfuse/.env -f infra/langfuse/compose.yaml up -d --wait --wait-timeout 240
docker compose --env-file infra/langfuse/.env -f infra/langfuse/compose.yaml ps
uv run --locked python tools/check_services.py
```

Остановка с сохранением данных:

```powershell
docker compose --env-file infra/langfuse/.env -f infra/langfuse/compose.yaml stop
```

Повторный запуск: команда `up` выше. Политика `unless-stopped` действует,
когда Docker Engine работает; она не запускает Docker Desktop после входа
в Windows сама по себе.

Данные хранятся в именованных Docker volumes с префиксом
`aim-setupaiv2-observability_`. Не используйте `down -v` для сохранения данных.
Пароли `.env` тоже нужно сохранять: повторная генерация не меняет автоматически
пароли уже инициализированных баз и пользователя Langfuse.

## Доступ и диагностика

- Web: `127.0.0.1:3000`, S3 для вложений: `127.0.0.1:9090`.
- Базы, Redis, worker и внутренние интерфейсы SeaweedFS не опубликованы на хосте.
- Телеметрия Langfuse и встроенный AI agent отключены.
- Пользователь `admin@aim-setupaiv2.local`; пароль `ADMIN_PASSWORD` в `.env`
  этого каталога. Ключи приложения находятся в корневом `.env`.
- Полный `docker compose config` и `docker inspect` содержат секреты.
  Для проверки конфигурации используйте `config --quiet`.
- Проверки выводят тип ошибки и HTTP-статус без тела ответа и ключей.
  При ошибках проверяйте `ps`, затем локальные логи нужного сервиса;
  не публикуйте логи без проверки на чувствительные данные.

Это локальный стенд, без проверки отказоустойчивости и восстановления backup.
Для эксплуатации с реальными данными потребуются резервное копирование,
контроль доступа, политика хранения и проверка ресурсов под нагрузкой.
Состояние health не заменяет проверку сохранения трассы.

Источники:

- [Langfuse Compose](https://langfuse.com/self-hosting/deployment/docker-compose)
- [S3 / Blob Storage](https://langfuse.com/self-hosting/deployment/infrastructure/blobstorage)
- [Headless initialization](https://langfuse.com/self-hosting/administration/headless-initialization)
- [SeaweedFS mini](https://github.com/seaweedfs/seaweedfs/wiki/Quick-Start-with-weed-mini)

## Отдельные проекты Fortune и Orbita

После штатного запуска базового стека:

```powershell
cd C:\Git\setupAIv2
uv run --offline --no-sync python tools/provision_langfuse_projects.py
```

Скрипт использует headless initialization, создаёт отсутствующие проекты
`fortune` и `orbita` с существующими ключами из `.env` этих репозиториев.
Он проверяет область доступа ключей и возвращает web к базовой конфигурации.
Данные не удаляет, ключи OpenAI не меняет; несколько раз перезапускает web,
поэтому выполнять вне активных задач. Не нужен при обычном запуске ПК.
Если существующий проект имеет другие ключи, проверка завершается ошибкой;
скрипт не сбрасывает ключи и не обходит проверку.

26.11.2026 выполнена согласованная переинициализация: прежние трассы удалены,
созданы три проекта и подтверждено сохранение новых трасс DeerFlow для обоих
агентов. Отчёт: `.scratch/deerflow/langfuse-acceptance.json`.
