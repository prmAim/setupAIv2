# Встроенный CodexChatModel: подготовка и эксплуатация

Подготовлено 27.11.2026. **Живой запуск не выполнен.** Рабочая установка
`.scratch/deerflow` не менялась. Отдельный кандидат находится в
`.scratch/deerflow-codex`; его конфигурация и образ готовы к следующей приёмке.
Не запускать обе установки одновременно на портах 2026/2027.

## Состав

- Pinned backend v2.2.0 из `images.json` плюс единственный upstream backport
  `CodexChatModel` из PR #8053, commit `88cddf6c56402fd3b938a2f105a03ac9c86b2d0d`.
  Исходники, лицензия и контрольные суммы — `vendor/`.
- Официальный Codex 0.172.0 Linux x86_64 musl. Он обслуживает только refresh
  credentials через `app-server account/read`, не выполняет coding tasks.
- Тонкий `setupaiv2_deerflow.codex:ManagedCodexChatModel` наследует встроенный
  класс. Формат сообщений, tools, HTTP/SSE и parsing остаются upstream.
  Надстройка управляет credentials, сроком запроса и безопасной остановкой.
- Единственная модель в candidate profile, без API key и fallback;
  provider и LLM middleware ограничены одной попыткой. Модель задаётся явно
  при подготовке, доступность для аккаунта офлайн не проверяется.

## Воспроизводимая сборка без авторизации

Нужны ранее загруженный backend по digest из `images.json` и официальный
[Codex archive](https://github.com/openai/codex/releases/download/rust-v0.172.0/codex-x86_64-unknown-linux-musl.tar.gz).
Сохранить архив в `.scratch/codex-integration/codex.tar.gz`; SHA256:
`a2342a8f7f68f088be30620ccfe9cb84162c93a6b56177855961cda3ed6be392`.
Сборщик проверяет архив, а installer дополнительно исходный и новый provider.
Сборщик отклоняет несовпадение FROM с `images.json` и создаёт новый временный
контекст для каждой сборки: удалённые модули и Python bytecode не переносятся.

```powershell
uv run --offline --locked python tools/build_deerflow_codex.py --codex-archive .scratch/codex-integration/codex.tar.gz
uv run --offline --locked python tools/deerflow_platform.py prepare --profile codex --model gpt-6-luna
docker compose -f .scratch/deerflow-codex/compose.json config --quiet
```

Сборка не разрешает сеть своим build steps. Загрузка публичных build inputs
отделена от неё. `prepare --profile codex` не читает API/Codex credentials,
не делает login/catalog/inference и не запускает сервисы. Он генерирует
локальные учётные данные самого DeerFlow и снимки HEAD проектов.
`gpt-6-luna` сохранён как идентификатор модели текущего проекта, **не как
утверждение о его доступности по подписке**. При выборе другого разрешённого
имени повторить prepare с явным `--model`.

`image.json` в каталоге кандидата содержит immutable image ID. После каждой
пересборки повторить prepare: существующий Compose сам на новый ID не переходит.
Обычные команды без `--profile codex` продолжают обслуживать прежнюю установку;
это сохранение совместимости до cutover, не автоматический API fallback.

## Обновление авторизации

У каждого проекта отдельный Linux named volume: `fortune-codex-auth` и
`orbita-codex-auth` в Compose project `deerflow-codex`, смонтированный только
в его Gateway как `/run/codex`. Это новый выделенный `CODEX_HOME`, а не mount
пользовательского `.codex`. Каталог 0700, `auth.json` должен быть regular file
0600 с managed ChatGPT login, access/refresh token, account ID и JWT expiry.
Нет mount в workspace, frontend, nginx или runner. Не копировать один
refresh-token chain между проектами: для каждого выделенного home нужен
отдельный вход владельца. Первичный вход в этом задании не выполнялся.

Перед каждым запросом под межпроцессным flock читается актуальный auth-файл.
Если до `exp` больше пяти минут — сетевого refresh нет. Иначе:

1. Сохранить и fsync `refresh.pending` с account ID, без токенов, затем fsync
   родительского каталога. Ошибка записи/синхронизации запрещает refresh.
2. Запустить закреплённый Codex с file credential store, forced ChatGPT auth,
   очищенным env и cwd в выделенном auth home.
3. Только `initialize`, `initialized`, `account/read {refreshToken:true}`.
   Нет `login/start`, `model/list`, `thread/start` или `turn/start`.
4. Codex владеет протоколом OAuth и записью обновлённого auth-файла.
   Надстройка перечитывает файл, проверяет неизменность account ID и запас expiry.
   Только после этого снимает marker, синхронизирует каталог и допускает inference.

На RPC отведено 30 секунд, на auth transaction вместе с ожиданием lock — 40.
При отмене задачи начатая транзакция refresh дожидается завершения в этих
границах: нельзя освободить lock посреди ротации refresh token. После неё
отмена распространяется дальше, inference не стартует. Ошибка/timeout/crash
с оставшимся marker блокирует следующий refresh и inference до восстановления
владельцем. При HTTP 401 после отправки inference не происходит скрытого
refresh+повтора: удалённый результат уже может быть неопределённым.

JWT expiry используется только для планирования refresh, не для проверки
подписи/доказательства доступа. Реальную авторизацию проверяет провайдер.
Смена аккаунта в уже созданной модели требует пересоздания runtime после
проверки владельцем; конкурентные запросы не меняют auth-поля общей модели.

## Восстановление после ошибки refresh

Команды ниже предназначены для следующей согласованной стадии, когда создан
выделенный auth volume. В текущем задании они не запускались на реальных данных.

В credential-bearing контейнере (или одноразовом контейнере с **только своим**
auth volume и `CODEX_HOME=/run/codex`):

```text
/app/backend/.venv/bin/python -m setupaiv2_deerflow.auth status
/app/backend/.venv/bin/python -m setupaiv2_deerflow.auth recover
```

Обе команды локальные: не делают refresh/login/inference и не печатают токены,
account ID, файлы или ответы сервера. `status` проверяет готовность. `recover`
берёт тот же lock и удаляет marker только при свежем токене **того же аккаунта**.
Если сервер успел обновить файл до падения процесса, повторная авторизация
может не понадобиться. Если токен не обновлён — владелец сначала вручную
восстанавливает вход официальным Codex в этот выделенный home. Нельзя лечить
ошибку бесконечными refresh, удалением marker вслепую или копированием всей
домашней `.codex`. Некорректный marker/смена аккаунта требуют отдельного разбора.

Локальный `ready` не подтверждает серверную валидность, квоту или inference.

## Отмена и ошибки модели

Upstream async HTTP теперь принадлежит текущей задаче: отмена закрывает stream
и client, включая repeated cancellation во время cleanup. Дополнительный
deadline запроса — 90 секунд, включая ожидание auth; при отмене во время
refresh возврат может задержаться до его ограниченного завершения. Sync invoke
делегирует тому же async implementation через отдельный event loop.

Безопасный `CodexStopped` наследует `GraphBubbleUp`: штатный LLM middleware
пропускает его без retry и без подмены ошибки обычным AI-ответом. Проверка
embedded Harness подтверждает, что успешное AI-сообщение при auth failure
не попадает в состояние. Интеграция внешнего контроллера с `cycle.stop_reason`
и полным процессом claim/PR остаётся последующим этапом общего плана.

Нет output-token cap у этого upstream endpoint. Deadline и закрытие сокета
не доказывают отмену удалённой генерации/расхода плана. Нельзя считать эту
подготовку подтверждением бюджета подписки или ночной приёмкой.

## Проверки

```powershell
$candidateImage = (Get-Content .scratch/deerflow-codex/image.json -Raw | ConvertFrom-Json).image
docker run --rm --pull never --network none --read-only --cap-drop ALL `
  --security-opt no-new-privileges --pids-limit 128 --memory 1g `
  --tmpfs /tmp:rw,nosuid,nodev,size=256m `
  --mount type=bind,source=C:/Git/setupAIv2/tests/deerflow,target=/tests,readonly `
  --entrypoint /app/backend/.venv/bin/python $candidateImage `
  -B -m pytest /tests -q -p no:cacheprovider
```

Suite использует искусственные credentials, MockTransport и loopback SSE.
Реальный Codex binary проверяется только через initialize/account-read **без
credentials**, с `refreshToken=false` и отключённой сетью контейнера.
Автоматизация: `.github/workflows/deerflow-codex.yml`; загрузка публичных
образа/архива предшествует offline build/test. CI не опубликован и не запускался.

## Следующая стадия

Нужны отдельно разрешённые первичная авторизация, ограниченный live model/tool
probe, refresh на реальном аккаунте и Gateway restart. Candidate использует
отдельные пустые DB/workspaces для приёмки и не переносит историю автоматически.
Перед production cutover сохранить текущие SQLite/память/workspaces и согласовать
их перенос, остановив запись; не запускать новый Gateway на старой живой БД.
Вернуть проверенную Langfuse-конфигурацию отдельно: в кандидате tracing выключен
и проектные `.env` не читаются. Старые сервисы/данные/доступы не удалять.

Источники: [upstream backport](https://github.com/bytedance/deer-flow/pull/8053),
[Codex app-server auth](https://learn.chatgpt.com/docs/app-server#auth-endpoints),
[pinned account/read implementation](https://github.com/openai/codex/blob/rust-v0.172.0/codex-rs/app-server/src/request_processors/account_processor/workspace_routing.rs),
[исследование вариантов](../../docs/validation/codex-deerflow-integration-research-20261127.md).
