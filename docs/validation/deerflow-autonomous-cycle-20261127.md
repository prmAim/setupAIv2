# Реализация автономного цикла на DeerFlow: прогресс 27.11.2026

## Актуальное уточнение после выбора CodexChatModel

Владелец выбрал встроенный CodexChatModel. Подготовленная интеграция принята
офлайн и опубликована через [PR #36](https://github.com/prmAim/setupAIv2/pull/36),
слитый в `main` 27.11.2026 (`f9cca1d`). Подробности —
[отчёт интеграции](deerflow-codex-integration-20261127.md) и
[актуальный план](../plans/deerflow-autonomous-cycle.md).

Все следующие разделы фиксируют хронологию до этого решения. Прежние выводы
«использовать встроенный адаптер не следует», SIWC как gate аккаунта и
«следующий шаг — Support» больше не определяют порядок работ. Support-текст
сохранён как неотправленный исторический материал. Новые live-попытки,
авторизация и ослабление MIME/TLS этим документом не разрешаются.

Доказательства synthetic checkpoint/resume остаются действительными в своих
границах: embedded Harness, не Gateway и не восстановление внешних эффектов.

## Исторический статус и доказательства

**Статус:** этап 1 завершён; этап 2 частично реализован. В повторном продолжении
перенесены #28/#29 и проверен process restart embedded DeerFlow с синтетической
моделью (см. раздел ниже). Проведена офлайн-проверка пути
модели в закреплённом образе и локального runner. Владелец выполнил согласованную
свежую попытку: `responses_post_attempted`, `plan_use=possible`,
`reason=media_type; media_class=missing`. Попытка использована; не повторять.
Следующий шаг — Support; прежние инструкции о ещё доступной попытке ниже
сохранены как история и больше не действуют. Полная цель остаётся прежней:
Issue → план → изменение кода → проверки → исправление или остановка →
доказательства → PR → проверка PR → слияние в `main` → следующая Issue.

## Исходное состояние

- Рабочая ветка `codex/deerflow-platform`, HEAD `0648943637d493866ff0c6a9be1dc8a445317c5c`.
- DeerFlow `v2.2.0`, upstream commit `58fc5bd5b8e6ea46538e9481ba06ca2a46e14e2a`;
  backend закреплён по digest `sha256:0eff3cb395700c5bfee09a6bc57f878dcdc6232591f2210896e5fe5fdf9c145b`.
- GitHub подтверждает закрытые #28 и #29: PR #33 и #34 находятся в
  `origin/main` (`1f7679a`). Текущая ветка от этой версии не основана и не
  содержит `tools/select_issue.py` или `tools/claim_issue.py`. Эти решения
  следует интегрировать из `origin/main` целиком, сохранив связанные проверки
  и отчёты.
- #30 остаётся открытой и назначенной владельцу. PR #35 открыт как draft,
  текущий head `e6eea313a592dabc621662e0d06712487dd8feb4`, отзывов нет.
  GitHub вернул успешный check `checks`; перед любой готовностью его связь с
  проверяемым SHA нужно подтвердить повторно.
- #31 открыта и зависит от #30; #32 открыта и зависит от #31. #10 не является
  блокером живого пилота по #27.
- Существующие изменения `AGENTS.md`, `README.md`, отчёта миграции, `.kilo/`,
  `CONTEXT.md`, ADR и плана сохранены.

## Схема интеграции по шагам

| Шаг полного цикла | Готовые компоненты | Состояние в профиле DeerFlow | Следующая интеграция и проверяемый результат |
| --- | --- | --- | --- |
| **Issue: выбор** | #28: профиль с закреплённой политикой; чтение пагинированной очереди, проверка автора, происхождения метки, зависимостей, веток и PR. Отчёт `docs/validation/issue-selection.md` в `origin/main`. | Выборщик в текущей ветке отсутствует. DeerFlow не имеет GitHub-инструментов и принимает задачу в чате, а не выбирает её сам. | Перенести #28 с его тестами и профилем; запускать по доверенному имени профиля, без ID задачи. Для пустой очереди получить `no_work` и доказать отсутствие записей и вызова модели. Текст Issue не задаёт права или команды. |
| **Issue: claim и восстановление** | #29: локальная блокировка одного Linux-хоста, write-ahead intent, подтверждение удалённого маркера и режим resume только для проверки уже сделанного. Отчёт `docs/validation/issue-claim.md` в `origin/main`. | Claim не подключён к чату или SQLite DeerFlow. GitHub не является транзакционной частью локальной БД. | Интегрировать перед первым вызовом модели; записать устойчивые `run_id` и фазу до внешней записи; при resume сверять GitHub и не повторять подтверждённый POST. Проверить два конкурирующих запуска и остановку между записью и чекпойнтом. Ограничение одного хоста сохраняется. |
| **План и состояние выполнения** | DeerFlow threads, цели, LangGraph checkpoints и DB-события; локальный прототип `tools/agent_skeleton.py` использует `StateGraph`, SQLite saver и сохраняет шаги и evidence. Runtime профили уже имеют отдельные SQLite-каталоги. | Настроено `database.backend=sqlite` и `run_events.backend=db`, но Issue, hash профиля, план и следующая операция не связаны с записью claim. Восстановление задачи через остановку процесса не принималось. Долговременная память не заменяет checkpoint. | В checkpoint связать Issue, профиль и его revision, цель, план, выполненные шаги, попытки, результаты гейтов, внешние intents и следующий шаг. Прервать задачу после checkpoint, перезапустить runtime и подтвердить продолжение без дублей. |
| **Изменение кода** | `snapshot()` в `tools/deerflow_platform.py` строит рабочее пространство из `HEAD`, исключает `.scratch`, локальные `.env` и ключевые файлы. DeerFlow имеет только `ls`, `read_file`, `glob`, `grep`, `write_file`, `str_replace`; `allow_host_bash=false`. | Файловый профиль изолирует рабочую копию, но не выполняет цикл разработки: нет GitHub, shell и вызова runner. Текущий generator задаёт `langchain_openai:ChatOpenAI` с `OPENAI_API_KEY`. | После проверки model path передавать модели только задачу и разрешённый контекст. Оставить правки в снимке проекта; не выдавать модели или runner токен публикации. Отсутствие подписочного доступа должно давать явную остановку без API fallback. |
| **Модель Codex** | DeerFlow v2.2.0 позволяет указывать LangChain `BaseChatModel` через `models[].use`. В образе backend офлайн проверено разрешение `deerflow.models.openai_codex_provider:CodexChatModel` фабрикой `deerflow.models.factory.resolve_class`. В отдельной ветке #30 уже есть локальный OSS Sign in with ChatGPT canary. | Встроенный класс читает Codex CLI auth-файл и отправляет запрос на внутренний endpoint; использовать его не следует. SIWC canary PR #35 работает отдельно от DeerFlow, хранит токены только в памяти и не реализует refresh. Каталог проверялся без inference; один live synthetic вызов завершился `media_type`. | Не монтировать `auth.json` и не повторять live canary. Сначала отправить минимальный отчёт OpenAI Support по наблюдавшемуся ответу. После ответа владелец отдельно решит, разрешить ли не более одного свежего synthetic запроса на точном reviewed SHA. Долговременное OAuth-хранение/refresh и интеграция в DeerFlow остаются последующей работой. |
| **Проверки** | В кодовой базе есть `tools/diff_scope.py`, `tools/test_integrity.py`, `tools/container_test_runner.py`, `tools/eval_harness.py`; runner на DinD не монтирует host `docker.sock` и не предназначен для секретов. Прототипы #21/#23 фиксируют версию образа и результаты выполнения. | Эти инструменты не доступны профилю DeerFlow. Shell намеренно выключен; выполнить тесты на хосте как запасной путь нельзя. | Соединить доверенный профиль с обязательным runner и передавать только reviewed tree и команды. Проверять test-integrity, diff-scope, lint/format, typecheck, быстрые и полные тесты. Нет runner или он недоступен — `not_run` и остановка. |
| **Исправление или остановка** | `tools/agent_skeleton.py` содержит ограниченную графовую fix-петлю; `tools/codex_engine.py` различает ошибки задачи и инфраструктуры. Политика #6 ограничивает fix тремя попытками, затем требует эскалацию. | Fix-петля, квоты и остановка не связаны с реальными действиями DeerFlow. | Перед каждой попыткой сохранять номер и бюджет. После трёх исправлений или лимита создавать эскалацию с причиной; не выполнять четвёртую попытку. Квота, auth, сеть, timeout и `not_run` не считать дефектом кода. |
| **Доказательства** | `TraceRecorder`, Langfuse и JSON-артефакты прототипов покрывают трассы, гейты, логи и длительности. Для DeerFlow включены отдельные project DB и трассировка после проверки ключей. | Трасса не связывает claim, SHA, результаты CI и внешние записи в единый отчёт. | Сохранять артефакт запуска с profile revision, Issue/claim, base/head SHA, командами, runner/image, результатами гейтов, PR, CI, reviewer и следующим шагом. Секреты не включать. |
| **PR** | `tools/publish.py` повторно использует ветку и PR по маркеру run ID. #30 добавляет candidate journal и подготовку draft при выполненных gate. | В DeerFlow нет GitHub tool и нет разрешения публикации. #30 частично реализована в PR #35; документ `docs/draft-run.md` на его ветке фиксирует `not_run` до проверки model credential и отсутствие API fallback. | Подключать publisher только после зелёных гейтов и устойчивого evidence. Восстановление после сбоя должно сверять ветку/PR и не дублировать запись. Живой claim-to-draft остаётся непринятым. |
| **Проверка PR и CI** | #31 определяет независимое read-only review, максимум три правки и обязательную перепроверку всех checks на текущем head SHA. | Нет интеграции ревьюера или CI в профиль; по #35 отзывов нет. | Рецензент без прав записи сверяет diff с Issue и профилем. Любой новый SHA аннулирует прежние тесты, review и CI; при финале повторно считать текущий head и все обязательные проверки. |
| **Merge и следующая Issue** | Политика #6/#7 требует семь обязательных гейтов, 0 merge при красных checks, эскалацию после трёх исправлений и условия перехода к auto-merge после четырёхнедельного shadow и ≥20 merged PR при заданных порогах качества. Выбор следующей задачи повторно использует #28. | В MVP #27/#32 агент передаёт проверенный PR человеку; #32 требует ноль agent merge. Полный целевой цикл из `CONTEXT.md` этим не исчерпывается. | На пилотном этапе оставить merge человеку. Автоматическое слияние и переход к следующей Issue включать только по действующей политике и после её гейтов. Затем проверить подряд две Issue, пустую очередь, перезапуск и отсутствие повторных внешних эффектов. |

## Проверка CodexChatModel

Context7 для DeerFlow v2.2.0 подтвердил пользовательский путь модели через
`models[].use` и передачу параметров конструктору. В закреплённом образе
проведён офлайн-запуск с `--network none`, `--read-only`, без credential и
mount: фабрика успешно разрешила `CodexChatModel`. Это подтверждает наличие
класса и совместимость конфигурационного пути с образом.

Это не проверка ChatGPT-аккаунта, срока токена, сетевого endpoint, вызова
модели или использования квоты. В установленном compose нет Codex auth mount.
Актуальная публичная документация OpenAI описывает Sign in with ChatGPT для
локальных open-source приложений: приложение открывает OAuth в браузере,
запрашивает отдельное согласие на использование плана, проверяет выданные
scopes, защищённо хранит токены и обновляет их. Запросы идут на публичный
`https://api.openai.com/v1/responses` с `store: false` и streaming.
Документация также описывает совместимый режим Codex app-server с этим OAuth
токеном, `base_url=https://api.openai.com/v1`, `wire_api=responses` и
`ACCESS_TOKEN`; после refresh app-server перезапускается и thread возобновляется.

Найденный DeerFlow `CodexChatModel` использует другой путь: Codex CLI auth-файл
и внутренний endpoint `chatgpt.com/backend-api/codex/responses`. Публичные
документы не подтверждают, что такой прямой вызов поддерживается для стороннего
приложения. Это вывод из сравнения реализации с публичным контрактом, а не
утверждение о запрете. Поэтому успешный импорт класса не делает этот transport
пригодным для подписочного runtime.

В PR #35 уже есть отдельный OSS SIWC canary с browser OAuth, проверкой identity
и scopes, одноразовым `POST /v1/responses` и синтетическим runner. Он не
встроен в DeerFlow и не хранит долговременные токены. `catalog-only` ранее
завершился успешно без inference. Единственный предыдущий live synthetic вызов
вернул HTTP 200 без подходящего `Content-Type: text/event-stream`; клиент не
читал тело и не повторял запрос. Неизвестно, был ли inference или расход плана.

27.11.2026 владелец согласовал одну свежую synthetic попытку до обращения в
Support и запустил `--run-synthetic` из проверенного worktree на SHA
`e6eea313a592dabc621662e0d06712487dd8feb4`. Запуск завершился
`stage=preflight; plan_use=not_attempted`, поэтому новая авторизация и запрос
к OpenAI не начинались. Read-only проверка Docker подтвердила `seccomp`, но
закреплённого образа не было. Скачан официальный
`docker.io/library/python:3.12-alpine` для `linux/amd64`; его image ID и
RepoDigest совпали с закреплённым `sha256:c5ba3deef00c7758561a0f03c26f44f10589263d40e732b8748e0d1f38f6dd89`.
После этого документированный `--preflight` прошёл с результатом
`preflight passed; no sign-in or plan use`. Согласованная одна live-попытка ещё
не использована. Сообщение в OpenAI Support не отправлялось.

В этом продолжении не выполнялись новые авторизации и модельные запросы, не
использовался API-key fallback, не было записи в Orbita, публикации PR или merge.
Результат предыдущего живого canary остаётся неопределённым по расходу плана.

## Политика допуска и следующий шаг

Согласование плана не меняет границы допуска. Не запускать `codex login`, не
передавать `auth.json`, не монтировать `.codex` и не повторять `--catalog-only`.
Предыдущий неудачный live-вызов всё ещё имеет неизвестный расход плана, но
владелец отдельно разрешил ровно один свежий synthetic запрос до обращения в
Support. Он пока не состоялся: `plan_use=not_attempted`, а локальный preflight
теперь проходит.

Следующее действие владельца — повторить из `C:\Git\_wf\siwc30` команду
`uv run --offline --locked python -m tools.siwc_canary --run-synthetic` на том
же SHA. Владелец вручную подтверждает терминальный opt-in и проходит OAuth в
системном браузере; команда может выполнить не более одного Responses-запроса
и он может расходовать план. Не повторять запуск автоматически. Передать
только итоговую безопасную строку canary. При успехе требуются код выхода `0`
и `synthetic passed`; при неуспехе остановиться и подготовить Support-отчёт по
фактической безопасной категории, не включая токены, callback URL, заголовки,
тела или логи.

Успешный synthetic canary ещё не является интеграцией DeerFlow или пилотом
Orbita. Для постоянного runtime отдельно потребуется защищённое
хранение/refresh SIWC и resume после обновления access token. Затем подключать
Issue/claim, runner и публикацию по действующей политике.
#10 не ставить условием для живого пилота по #27.

## Повторное продолжение: интеграция и process restart

### Актуальное состояние и перенесённые компоненты

GitHub повторно прочитан 27.11.2026 через `gh` с явным репозиторием:
#28/#29 CLOSED; #30/#31/#32 OPEN. PR #35 OPEN/DRAFT, head
`e6eea313a592dabc621662e0d06712487dd8feb4`, reviews пусты, check `checks`
SUCCESS. Удалённый main: `1f7679a1516a628e5eb040296f5b3c64d0eaa95b`.
Первый сетевой read в sandbox не прошёл; разрешённый read вне sandbox прошёл.

Все 14 добавленных файлов #28/#29 из этого main перенесены в рабочую копию:
`claim.py`, `tools/select_issue.py`, `tools/claim_issue.py`, GraphQL-запрос,
`tools/__init__.py`, два набора unit-тестов, четыре fixture-файла, CI и два
отчёта приёмки. Код перенесён без изменений; к Linux-only claim-тестам добавлен
skip на Windows, где `flock` недоступен (не подмена проверки PASS). Это интеграция файлов,
не merge ветки и не установка разрешений/credentials. Сохранены прежний runner,
проверки diff/test-integrity, изменения миграционного отчёта и `.kilo/`.

Схема выше описывает исходную точку: выборщик и claim теперь доступны локально,
но ещё не подключены к Gateway. Следующий связующий шаг — доверенный контроллер,
который удерживает исключительное владение исполнением от проверки claim до
checkpoint. `verified_only` из #29 не разрешает запуск работ; нельзя выдавать
его за уже реализованный execution handoff.

### Реализованная проверка состояния

- `tools/deerflow_cycle_state.py`: схема progress для `state_schema` embedded
  DeerFlow. Сохраняет Issue, run/profile revision, цель, решения, план, завершённые
  шаги, проверки, сведения об эффектах, причину остановки и следующий шаг.
  Поля progress не являются политикой доступа; runtime-валидация доверенного
  профиля и подтверждение внешних эффектов остаются у контроллера.
- `tools/verify_deerflow_resume.py`: настоящий `create_deerflow_agent` из
  закреплённого образа с `SqliteSaver`, middleware-паузой и синтетической моделью.
  Процесс завершается через `os._exit(73)` после сохранённой паузы без cleanup;
  другой процесс восстанавливает тот же thread и завершается через
  `os._exit(74)` после terminal checkpoint; третий только читает результат.
- Проверены все поля исходного progress, сохранение ответа и завершённого
  состояния. По одному model-call marker создаётся эксклюзивно: повторный вызов
  модели приводит к ошибке. На обоих проектах намеренно одинаковый thread ID
  при разных SQLite-каталогах; их цели и repository не смешались.
- `interrupt()` повторно исполняет начало узла после resume. Поэтому до него
  здесь нет внешних действий. Проверка **не доказывает exactly-once для GitHub**;
  write-ahead/reconciliation из #29/#30 по-прежнему обязательны.

### Доказательства

В контейнере `ghcr.io/bytedance/deer-flow-backend@sha256:0eff3cb395700c5bfee09a6bc57f878dcdc6232591f2210896e5fe5fdf9c145b`:

1. `python -B -m unittest test_selection test_claim -v`: **24/24 PASS**, exit 0.
   Источники `tools`, `tests`, `claim.py` смонтированы read-only; GitHub заменён
   fixture. Включены конкуренция процессов, потерянный ответ и восстановление
   claim, неопределённые чтения и пустая очередь.
2. `python -B -m tools.verify_deerflow_resume`: **PASS**, exit 0:

```json
{"status":"passed","runtime":"DeerFlow embedded factory","projects":["orbita","fortune"],"processes":6,"synthetic_model_calls_per_project":1,"live_model_calls":0,"external_writes":0,"gateway_restart":"not_run","live_effect_recovery":"not_run"}
```

В обоих случаях `--network none --read-only --cap-drop ALL
--security-opt no-new-privileges --pids-limit 128 --memory 1g`, временный `/tmp`,
без credentials, mounts рабочих данных и host socket. Команда resume приведена
в [инструкции](../../infra/deerflow/README.md#офлайн-проверка-checkpoint-полного-цикла).
Полная старая suite и живая приёмка в этом продолжении не запускались.

### Что остаётся

Этап 2 частично подтверждён только для embedded Harness с синтетической моделью.
Рабочие контейнеры Gateway не менялись и не перезапускались. Схема пока не
подключена к ним; refresh OAuth, реальный inference и resume после него не приняты.
Новый транспорт не написан: встроенный CodexChatModel исследован, существующий
SIWC canary остаётся точкой проверки аккаунта. Историческое указание в таблице
сначала обратиться в Support уточняется более поздним согласованием одной
попытки в разделе «Политика допуска»: повтор автоматически не выполняется.

Следующее зависимое действие — владелец завершает одну ранее согласованную
попытку `--run-synthetic` на reviewed SHA `e6eea31` по инструкции выше и передаёт
только безопасную итоговую строку. После успешного результата: защищённые
хранение/refresh, подключение модели к DeerFlow, затем trusted execution handoff,
runner и #30; далее #31/#32 и условия допуска merge/очереди полного плана.
До этого нет оснований запускать live Orbita, принимать #30 или сливать #35.
Цель остаётся полным циклом до merge и следующей Issue.

Все изменения этого продолжения пока локальны, HEAD `0648943`. Публикации
в GitHub, вызовов подписки/API и merge не было.

## Результат свежей попытки владельца

27.11.2026 владелец передал вывод запуска из `C:\Git\_wf\siwc30`:

```text
preflight passed; no sign-in or plan use
Use your ChatGPT plan for one synthetic request? Type Continue with ChatGPT: Continue with ChatGPT
synthetic not passed; stage=responses_post_attempted; plan_use=possible; reason=media_type; media_class=missing
```

Источник — сообщение владельца, не новый запуск агентом. Точное время и код
выхода в сообщении отсутствуют. Согласованная одна свежая попытка использована.
По коду canary категория `media_class=missing` на этом этапе означает HTTP 200
с отсутствующим, пустым или состоящим из пробелов `Content-Type`; это вывод
из обработки ответа, а не отдельно сохранённые HTTP-заголовки. Проверка MIME
останавливает обработку до чтения тела. Валидный ответ модели и контейнерная
проверка сгенерированного результата не подтверждены; предшествующий offline
preflight прошёл. Расход плана остаётся неизвестным, возможным.

Нельзя по этой строке установить причину или место нарушения: сервер,
промежуточный сетевой узел или иной компонент. Новый запрос, ослабление MIME/TLS,
API fallback и сброс регистрации не выполнялись. Живой #30 и этап 2 остаются
непринятыми. Следующий шаг — отправка отчёта в Support; автоматическая отправка
не разрешалась и не выполнялась.

### Подготовленный текст обращения (не отправлен)

Subject: Sign in with ChatGPT OSS — Responses stream rejected due to missing Content-Type

On 27 November 2026, I ran one explicitly consented synthetic request from a
local Windows client using Sign in with ChatGPT for open-source apps. The
offline Docker preflight passed. The request reached POST
`https://api.openai.com/v1/responses`, with `Accept: text/event-stream`,
`stream: true` and `store: false`.

The client reported:

```text
synthetic not passed; stage=responses_post_attempted; plan_use=possible; reason=media_type; media_class=missing
```

In this client, that result means HTTP 200 with an absent, empty or
whitespace-only Content-Type header. The expected media type was
`text/event-stream`. The client rejected the response before reading its body
and did not retry. No successful model response or generated-code validation
was confirmed. Whether the request consumed my ChatGPT plan allowance is unknown.
A previous attempt on 25 November produced the same diagnostic category.

Client source revision: `e6eea313a592dabc621662e0d06712487dd8feb4`.
The exact timestamp, raw headers, response body and request ID are not available
in the shared diagnostic output. No credentials, tokens or OAuth callback URLs
are included in this report.

Could you help determine why this route returned no usable Content-Type,
whether the request consumed plan allowance, and what safe diagnostic metadata
we should collect before considering any further request?

## Источники

- [DeerFlow v2.2.0: custom models](https://github.com/bytedance/deer-flow/blob/v2.2.0/frontend/src/content/en/harness/customization.mdx)
- [DeerFlow v2.2.0: configuration](https://github.com/bytedance/deer-flow/blob/v2.2.0/backend/docs/CONFIGURATION.md)
- [OpenAI: Using Codex with your ChatGPT plan](https://help.openai.com/en/articles/11369540-using-codex-with-your-chatgpt-plan)
- [OpenAI: Using your ChatGPT plan in other apps and sites](https://help.openai.com/en/articles/20001542-using-your-chatgpt-plan-in-other-apps-and-sites)
- [OpenAI: Sign in with ChatGPT for open-source apps](https://developers.openai.com/siwc/token-sharing-open-source)
- [OpenAI: Codex app-server with ChatGPT plan tokens](https://developers.openai.com/siwc/token-sharing-open-source/codex-app-server)
- [Issue #28](https://github.com/prmAim/setupAIv2/issues/28), [#29](https://github.com/prmAim/setupAIv2/issues/29), [#30](https://github.com/prmAim/setupAIv2/issues/30), [#31](https://github.com/prmAim/setupAIv2/issues/31), [#32](https://github.com/prmAim/setupAIv2/issues/32), [draft PR #35](https://github.com/prmAim/setupAIv2/pull/35)
- [Policy #6](https://github.com/prmAim/setupAIv2/issues/6), [shadow-mode decision #7](https://github.com/prmAim/setupAIv2/issues/7)
