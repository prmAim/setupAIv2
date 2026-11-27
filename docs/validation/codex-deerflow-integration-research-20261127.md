# Codex + DeerFlow: готовые пути и упрощение интеграции

Дата исследования: 27.11.2026. Статус: исследование и офлайн-проверка;
предложение для следующего этапа, не изменение политики допуска.

## Рекомендация

**Да, модельную интеграцию усложнили преждевременно:** собственный OSS Sign in
with ChatGPT (SIWC) фактически стал обязательным предшественником DeerFlow,
хотя в установленном v2.2.0 уже есть документированный `CodexChatModel`.
Рекомендую основной путь: **DeerFlow Harness + встроенный CodexChatModel**,
сначала отдельная ограниченная проверка, затем подключение существующих
компонентов полного цикла. SIWC оставить альтернативой, а не обязательным
гейтом. Это инженерный выбор минимального объёма интеграции, а не утверждение
о готовности аккаунта или гарантированной поддержке транспорта OpenAI.

До ночной эксплуатации необходимо закрыть два конкретных пробела встроенного
провайдера: обновление credentials и корректную отмену запросов. Для последнего
уже существует upstream-исправление после нашего SHA. Если прямой доступ
провайдера не будет принят владельцем или подтвердится непригодность,
резервный путь — официальный Codex через готовый ACP/app-server, с отдельно
решённым источником модели ведущего агента. Не писать новый OAuth-клиент
автоматически при первом затруднении.

Полная цель сохраняется: Issue → план → код → проверки → исправление/остановка
→ доказательства → PR → независимое ревью/CI → merge по политике → следующая
Issue, включая ночь. Простое подключение модели не реализует этот цикл.

## Что проверено и с какой версией

- Рабочая ветка `codex/deerflow-platform`, HEAD
  `0648943637d493866ff0c6a9be1dc8a445317c5c` совпали с передачей.
  Существующие staged/unstaged/untracked изменения сохранены, включая `.kilo/`
  и миграционный отчёт. GitHub-статусы #28–35 заново не проверялись; сведения
  о них здесь происходят из [отчёта продолжения](deerflow-autonomous-cycle-20261127.md).
- Локальный upstream чистый, HEAD
  `58fc5bd5b8e6ea46538e9481ba06ca2a46e14e2a`, commit date
  `2026-11-04T14:46:47+08:00`. Версия и digest сверены с
  [images.json](../../infra/deerflow/images.json). Исследован именно этот код,
  а не только меняющаяся ветка `main`.
- Context7: разрешён `/bytedance/deer-flow`, запросы с `version=2.2.0`
  вернули ссылки на v2.2.0 для модели и ACP. Для Codex исследован `/openai/codex`;
  текущие официальные веб-страницы уточнили контракт. Где нет даты публикации,
  дата выше означает дату чтения, а не дату появления функции.
- Проведена синтетическая проверка настоящего провайдера в pinned backend
  без сети, реальных credentials и рабочих volumes: результат ниже.
- Не выполнялись login, чтение auth-файлов, live canary, inference, публикации,
  обновление образов, изменение сервисов, допусков или ADR.

## Реальные варианты

| Вариант | Готовые компоненты и версия | Аккаунт и авторизация | Сложность для нас / границы |
|---|---|---|---|
| **CodexChatModel — основной** | Встроенный LangChain BaseChatModel в DeerFlow v2.2.0; фабрика и tool calling уже есть | Codex CLI ChatGPT login в читаемом JSON; `CODEX_AUTH_PATH` либо `~/.codex/auth.json`; ключ API не требуется этому классу | Минимум новой интеграции модели. Требует отдельно принятой передачи одного credential-файла доверенному runtime; сейчас она не разрешена. Нет собственного refresh; прямой HTTP-контракт поддерживается DeerFlow, не доказан как публичный API OpenAI |
| **Codex через ACP** | Реальный `invoke_acp_agent` в v2.2.0. Пример указывает старый `@zed-industries/codex-acp`; актуальный преемник `@agentclientprotocol/codex-acp` запускает app-server | Поддерживается ChatGPT login; API-key auth — отдельный вариант, не fallback. Нужны доступный runtime executable и проверенная пара версий | Готовый coding worker, но **не** модель lead agent. Нужны установка в среду Gateway, разрешения, workspace, фиксация версий и проверка совместимости нового адаптера с v2.2.0 |
| **Codex CLI `exec`** | Официальный noninteractive CLI, события JSONL, resume | Saved CLI auth; eligibility и лимиты плана проверяются отдельно | Простой внешний worker, но в изученном DeerFlow нет специального встроенного `exec` model adapter. Shell/свой tool и обработка результатов — дополнительная интеграция; текущий профиль shell не разрешает |
| **Прямой Codex app-server** | Официальный JSON-RPC сервер, sessions/events/approvals, managed auth/refresh | Документация допускает продолжение локальных/OSS приложений с его auth; SIWC рекомендует для контроля. Hosted/commercial сценарии имеют другие условия | Надёжнее опираться на официальный agent runtime, чем писать свой transport. Однако raw app-server не говорит ACP и не является LangChain BaseChatModel: прямой мост DeerFlow ещё надо реализовать; готовый ACP уменьшает эту работу |
| **OSS SIWC → публичный Responses** | Официальный OAuth и API-контракт; существующий наш canary. Готового SIWC provider DeerFlow в исследованном коде не найдено | Eligible plan (в изученных материалах Plus/Pro), отдельное согласие и регистрация приложения; хранение/refresh/scopes на стороне интегратора | Наибольшая собственная работа для базового подключения. Полезен для приложения со своим входом и управлением доступом, не доказан как обязательный для этой локальной установки |
| **OpenAI API key** | Текущий generator использует ChatOpenAI + Responses | Отдельные API credentials/billing | Технически простой путь, но не соответствует предпочтению использовать подписку. Не выбран и не включается автоматически |

Источники: [DeerFlow configuration v2.2.0](https://github.com/bytedance/deer-flow/blob/v2.2.0/backend/docs/CONFIGURATION.md),
[ACP catalog v2.2.0](https://github.com/bytedance/deer-flow/blob/v2.2.0/frontend/src/content/en/harness/subagents/catalog.mdx),
[Codex noninteractive](https://learn.chatgpt.com/docs/non-interactive-mode),
[app-server auth](https://learn.chatgpt.com/docs/app-server#auth-endpoints),
[SIWC quickstart](https://developers.openai.com/siwc/quickstart).

## Фактический контракт встроенной модели

Проверено непосредственно в pinned
[openai_codex_provider.py](https://github.com/bytedance/deer-flow/blob/58fc5bd5b8e6ea46538e9481ba06ca2a46e14e2a/backend/packages/harness/deerflow/models/openai_codex_provider.py),
[credential_loader.py](https://github.com/bytedance/deer-flow/blob/58fc5bd5b8e6ea46538e9481ba06ca2a46e14e2a/backend/packages/harness/deerflow/models/credential_loader.py)
и фабрике из той же локальной checkout:

- CLI не запускается. Класс один раз при создании читает access token/account ID
  и сам отправляет HTTP на `https://chatgpt.com/backend-api/codex/responses`.
  Поддержаны top-level `access_token`, `token` и вложенный `tokens.access_token`.
  Keyring-only login этим loader не читается. Наличие CLI на Windows само по
  себе не делает credentials доступными внутри Linux Gateway.
- Формирует Responses input, `store=false`, `stream=true`, reasoning effort,
  function calls/results с `call_id`; поддерживает `bind_tools`, `tool_choice`
  и strict. SystemMessage преобразуется в instructions. Content blocks
  уплощаются в текст: это не прозрачная мультимодальная замена ChatOpenAI.
- SSE собирается до завершённого ответа; failed/incomplete — ошибка.
  Это streaming транспорта, а не обещание полноценного token streaming UI.
  Usage может отсутствовать. При его отсутствии счётчик токенов нельзя считать
  доказанным жёстким бюджетом расхода.
- По умолчанию до трёх попыток на 429/500/529, HTTP timeout 300 секунд;
  `retry_max_attempts=1` убирает повторы **провайдера**, но не обязательно
  middleware/контроллера. Для однократной проверки нужен прямой вызов модели
  либо отдельно отключённые и проверенные все внешние retry layers.
- `max_tokens`/`max_output_tokens` фабрика удаляет: запрос не имеет такого
  ограничения выхода. Prompt «ответь коротко» не является жёстким лимитом.
- Нет refresh OAuth, проверки expiry или перечитывания токена перед каждым
  запросом. Замена файла не обновляет уже созданный объект. Ночной runtime
  требует проверенного lifecycle refresh/recreate либо сохранённой остановки
  при auth failure; read-only mount сам этого не решает.
- В нашем образе отсутствует собственный `_agenerate`. Upstream
  [issue #8051](https://github.com/bytedance/deer-flow/issues/8051) от 09.11.2026
  описывает сохранение HTTP-потока после cancel; исправление
  [PR #8053](https://github.com/bytedance/deer-flow/pull/8053) merged 12.11.2026,
  позднее закреплённого commit. Его наличие в `main` не обновляет наши images.
  До ночного режима нужен reviewed upgrade/backport и offline cancellation
  regression. Закрытие клиентского сокета не доказывает остановку удалённой
  генерации или отсутствие расхода.

Наличие endpoint `backend-api` **не доказывает запрет**: OpenAI сам описывает
его в статье [Unrolling the Codex agent loop, 23.01.2026](https://openai.com/index/unrolling-the-codex-agent-loop/).
Но статья о CLI не гарантирует совместимость стороннего прямого провайдера.
Официальный [app-server](https://learn.chatgpt.com/docs/app-server#auth-endpoints)
имеет собственный документированный контракт; нельзя автоматически переносить
его обещания на CodexChatModel. Получается промежуточный, точный вывод:
**готовая интеграция поддерживается DeerFlow; общий запрет не установлен;
поддержка конкретного прямого сценария OpenAI и аккаунта остаётся открытой.**

## ACP действительно есть, но он не устраняет все вопросы

В pinned `invoke_acp_agent_tool.py` происходит spawn отдельного процесса,
initialize → `new_session` → prompt. Каждый вызов создаёт новую session;
встроенного восстановления прежней ACP session этим tool не найдено.
Директория per-thread сохраняется, но это не равнозначно сохранению сессии
или exactly-once исполнения. Процесс стартует на стороне Gateway, а не
автоматически внутри тестового runner. Поэтому выключенный host bash для
файловых tools не изолирует произвольный ACP subprocess.

`auto_approve_permissions=false` отклоняет запросы, а не показывает владельцу
диалог подтверждения. `true` автоматически разрешает их — это не наша
политика допуска. Timeout по умолчанию 1800 секунд охватывает protocol exchange;
workspace preparation/cleanup находятся вне него. Внешние агенты обходят
обычный task catalog/capacity/ledger, поэтому лимиты subagents нельзя считать
лимитами ACP. Enabled MCP servers передаются отдельно: следует проверять их
набор и доступы. Источники: pinned
[tool](https://github.com/bytedance/deer-flow/blob/v2.2.0/backend/packages/harness/deerflow/tools/builtins/invoke_acp_agent_tool.py),
[config](https://github.com/bytedance/deer-flow/blob/v2.2.0/backend/packages/harness/deerflow/config/acp_config.py).

Исторический [RFC #1302, 24.03.2026](https://github.com/bytedance/deer-flow/issues/1302)
объясняет tool-first подход, но реализация подтверждается кодом, не самим RFC.
[Старый Zed adapter](https://github.com/zed-industries/codex-acp) архивирован
22.07.2026 и рекомендует [новый adapter](https://github.com/agentclientprotocol/codex-acp).
Новый использует app-server и поставляет совместимый Codex. Следовательно,
копировать `npx -y @zed-industries/codex-acp` как актуальную production-команду
не следует. Версию преемника для нашей установки ещё не выбрали/не проверили;
перед внедрением нужен pinned package/lock и offline handshake.

## Самый простой документированный quickstart и наши отличия

Минимальный путь upstream: доступный в процессе DeerFlow Codex CLI login,
запись модели в config, выбор её в UI и запрос. Пример модели из v2.2.0:

```yaml
models:
  - name: gpt-5.4
    use: deerflow.models.openai_codex_provider:CodexChatModel
    model: gpt-5.4
    supports_thinking: true
    supports_reasoning_effort: true
    retry_max_attempts: 1
```

`gpt-5.4` здесь идентификатор документированного примера, не результат проверки
доступности для владельца. Выбирать модель следует по разрешённому аккаунту.
Это конфигурационная схема для будущего согласованного пилота, не инструкция
сейчас авторизовываться или монтировать реальный файл.

Upstream [CLI Credential Mounts](https://github.com/bytedance/deer-flow/blob/v2.2.0/backend/docs/CONFIGURATION.md)
описывает один файл и `CODEX_AUTH_PATH`; полный `.codex` уже не обязателен.
В том же документе осталась противоречащая строка о mounts во всех режимах:
приоритет имеют конкретный раздел и проверенный Compose. Наш Compose
генерируется отдельно и mounts Codex не содержит. Исторический
[bug #1246, 22.03.2026](https://github.com/bytedance/deer-flow/issues/1246)
показывает реальный класс проблемы «credentials на host, но не в container»;
он не доказывает ни нашу live-совместимость, ни необходимость полного mount.

| Сейчас в setupAIv2 | Минимальное изменение после отдельного допуска |
|---|---|
| `tools/deerflow_platform.py` генерирует ChatOpenAI и требует `OPENAI_API_KEY` | Явный subscription profile с CodexChatModel, без зависимости от API key и без fallback; менять generator, не вручную перезаписываемый JSON |
| Изолированные Fortune/Orbita Gateway, SQLite и snapshots | Сохранить; сначала отдельный probe Orbita без доступа к рабочему repo |
| Только шесть файловых tools, без shell/GitHub/runner | Для model probe достаточно одного синтетического read-only tool; полноценный цикл затем подключает уже существующий runner/controller |
| SIWC canary ожидается до продолжения модели | Разделить результаты разных transport. Неуспех public Responses не является отрицательным тестом CodexChatModel/ACP |
| Embedded resume уже прошёл с fake model | Переиспользовать схему progress; отдельно проверить Gateway, credential renewal и reconciliation внешних эффектов |

Это **документированный и офлайн проверенный** quickstart, а не выполненный
нами живой запуск. Конфигурация коллеги в Tendis неизвестна. Встроенный provider
или ACP — правдоподобные объяснения простого старта, но не факты о его проекте.

## Где лишняя сложность, а где требования нужны

Лишняя зависимость — собственная регистрация SIWC, scopes, token storage/refresh
и новый transport до оценки уже готовой модели. Дополнительное усложнение —
смешать agent worker (ACP/exec) и model provider, получив два агентных цикла
там, где пока достаточно одного. SIWC нужен, если осознанно выбираем свой
вход через публичный API; его ограничения и lifecycle реальны:
[sign-in](https://developers.openai.com/siwc/token-sharing-open-source/sign-in),
[inference](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference),
[preview limitations](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations).
Запрет перенаправлять **SIWC** токен на backend-api относится к этому flow;
его нельзя обобщать на любой Codex CLI login.

Нужны и остаются: раздельные проектные состояния/доступы, изолированное
исполнение кода без credentials, single-owner claim, сохранение прогресса,
ограничения попыток/времени, независимое ревью, CI на текущем SHA, политика
merge, reconciliation после неопределённого push/PR. Эти требования следуют
из [CONTEXT](../../CONTEXT.md) и [плана](../plans/deerflow-autonomous-cycle.md).
Простота установки коллеги не свидетельствует, что у него решён тот же
полный ночной цикл. Граница между credential-bearing Gateway и runner
особенно нужна при включении ACP.

## Минимальный план перехода и критерии приёмки

1. Принять или отклонить предложенный model path отдельно от поддержки SIWC.
   Сохранить canary и Support-текст как evidence альтернативного transport;
   не повторять попытку и не принимать #30 по результатам этого исследования.
2. Подготовить отдельный subscription profile в generator, без API fallback,
   с проверкой retries, временного лимита и отсутствия секретов в workspace.
   Отдельно выбрать reviewed upstream version/backport отмены; рабочие images
   до review и проверки не обновлять. Не переписывать Harness.
3. До credential mount отдельно согласовать конкретный доступ доверенного
   runtime: только необходимый auth-файл, без истории `.codex`, без передачи
   модели/runner и без содержимого в логах. Эта граница следует из текущего
   поручения и ранее зафиксированной политики; исследование её не меняет.
4. После этого — ограниченный model probe: отдельный процесс/контейнер,
   синтетический prompt, один read-only `echo` tool, максимум **два** model
   calls (tool call и ответ после tool result), provider retry=1, без внешнего
   retry, без GitHub и API key, hard process deadline 90 секунд. Успех:
   завершённый ответ, валидные arguments/call_id, ровно один разрешённый tool
   effect, оба запроса в пределах лимита, нет fallback. Любой 401/403/429,
   timeout, failed/incomplete или неясный результат — сохранённая остановка,
   без автоматической повторной авторизации/запроса. HTTP 200 недостаточно.
   Deadline ограничивает локальный процесс, не гарантирует бюджет/remote stop.
5. Отдельная offline acceptance: fake 401/429, отмена до headers и во время
   SSE с peer EOF, fake-token rotation/recreate; затем согласованная живая
   проверка refresh/resume. До неё ночная работа не принята.
6. Подключить #28/#29, trusted execution handoff, runner/publisher, #31/#32;
   по действующей политике пройти одну Issue полностью, затем две подряд,
   пустую очередь, restart и ночной пилот. Merge и следующая Issue остаются
   обязательной целью, даже если на промежуточной приёмке merge делает человек.

Это план, а не выданное разрешение на следующие внешние действия. Он не требует
ответа коллеги для полезного продолжения. Один точный вопрос для сравнения:
**«Какие у тебя версия/commit DeerFlow и способ подключения Codex: значение
`models[].use` или название/версия ACP-адаптера, и используется ChatGPT login
или API key? Нужны только названия, без токенов и auth-файлов».**

## Офлайн-доказательство и воспроизведение

27.11.2026 в pinned backend выполнен небольшой MockTransport probe настоящего
CodexChatModel. Credentials заменены объектом `CodexCliCredential` с заведомо
искусственными строками до создания модели; credential loader не читал файлы.
HTTP полностью заменён MockTransport, Docker network=none. Проверены tool
call/result, текст, отказ на incomplete, отсутствие retry на 429 при
`retry_max_attempts=1`, отсутствие synthetic token в serialization.

```json
{"status":"passed","mock_http_requests":4,"live_requests":0,"checks":["tool_roundtrip","text","incomplete_rejected","429_no_retry","serialization"],"native_async_override":false}
```

Это не upstream full suite, не Gateway acceptance, не проверка реальной
авторизации, доступности модели, remote cancellation или расхода подписки.
Первый запуск с системным `python` завершился `ModuleNotFoundError: httpx`;
успешный запуск использовал штатный `/app/backend/.venv/bin/python`.

Для воспроизведения сохранить код из приложения в игнорируемый
`.scratch/codex-provider-offline-probe.py`, затем из корня репозитория:

```powershell
docker run --rm --pull never --network none --read-only --cap-drop ALL `
  --security-opt no-new-privileges --pids-limit 128 --memory 1g `
  --tmpfs /tmp:rw,nosuid,nodev,size=128m `
  --mount type=bind,source=C:/Git/setupAIv2/.scratch/codex-provider-offline-probe.py,target=/probe.py,readonly `
  --entrypoint /app/backend/.venv/bin/python `
  ghcr.io/bytedance/deer-flow-backend@sha256:0eff3cb395700c5bfee09a6bc57f878dcdc6232591f2210896e5fe5fdf9c145b `
  -B /probe.py
```

Ожидаются exit 0 и приведённый JSON. Нет загрузок пакетов/образов и mount
рабочих данных. Таким образом подтверждена только офлайн-совместимость
базового model/tool контракта этапа 2; полный цикл ещё не принят.

### Приложение: код офлайн-проверки

```python
import json
from unittest.mock import patch
import httpx
from langchain_core.messages import HumanMessage, ToolMessage
from deerflow.models import openai_codex_provider as provider
from deerflow.models.credential_loader import CodexCliCredential

requests = []
mode = 'tool'
def respond(request):
    requests.append(json.loads(request.content))
    if mode == '429':
        return httpx.Response(429, request=request)
    if mode == 'incomplete':
        event = {'type': 'response.incomplete', 'response': {'incomplete_details': {'reason': 'fixture'}}}
    else:
        output = ([{'type': 'function_call', 'name': 'echo', 'call_id': 'call-fixture', 'arguments': '{"value":"ok"}'}]
                  if mode == 'tool' else [{'type': 'message', 'content': [{'type': 'output_text', 'text': 'ok'}]}])
        event = {'type': 'response.completed', 'response': {'output': output}}
    return httpx.Response(200, headers={'content-type': 'text/event-stream'}, text='data: ' + json.dumps(event) + '\n\n')

real_client = httpx.Client
def fake_client(*args, **kwargs):
    return real_client(transport=httpx.MockTransport(respond), trust_env=False)

with patch.object(provider, 'load_codex_cli_credential', return_value=CodexCliCredential(access_token='synthetic', account_id='fixture')), patch.object(provider.httpx, 'Client', fake_client):
    model = provider.CodexChatModel(retry_max_attempts=1)
    bound = model.bind_tools([{'name': 'echo', 'description': 'fixture', 'parameters': {'type': 'object', 'properties': {'value': {'type': 'string'}}}}], tool_choice='required')
    answer = bound.invoke([HumanMessage('fixture')])
    assert answer.tool_calls[0]['id'] == 'call-fixture'
    mode = 'text'
    result = model.invoke([HumanMessage('fixture'), answer, ToolMessage('ok', tool_call_id='call-fixture')])
    assert result.content == 'ok'
    assert any(i.get('type') == 'function_call_output' and i['call_id'] == 'call-fixture' for i in requests[-1]['input'])
    for mode in ['incomplete', '429']:
        before = len(requests)
        try:
            model.invoke([HumanMessage('fixture')])
        except (RuntimeError, httpx.HTTPStatusError):
            pass
        else:
            raise AssertionError(mode)
        assert len(requests) == before + 1
    assert all(r['store'] is False and r['stream'] is True for r in requests)
    assert 'synthetic' not in json.dumps(model.to_json())
print(json.dumps({'status': 'passed', 'mock_http_requests': len(requests), 'live_requests': 0, 'checks': ['tool_roundtrip', 'text', 'incomplete_rejected', '429_no_retry', 'serialization'], 'native_async_override': '_agenerate' in provider.CodexChatModel.__dict__}))

```
