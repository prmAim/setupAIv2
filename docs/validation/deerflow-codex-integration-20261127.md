# Подготовка встроенного CodexChatModel — 27.11.2026

Основание: владелец поручил перейти на встроенный CodexChatModel, подготовить
обновление авторизации и отмену запросов, сохранить текущие изменения и
**не выполнять живой запуск**. Это продолжение этапа 2
[полного плана](../plans/deerflow-autonomous-cycle.md), а не приёмка ночного цикла.

## Результат

Подготовлен отдельный кандидат DeerFlow с native CodexChatModel и минимальной
надстройкой lifecycle. Рабочие сервисы не перезапускались, конфигурация
`.scratch/deerflow` и реальные Codex credentials не читались/не изменялись.
Login, refresh настоящего аккаунта, model/catalog API, live canary, GitHub
публикации и merge не выполнялись. Сеть использовалась для публичных
документов, исходников и закреплённого официального release archive.

Ветка осталась `codex/deerflow-platform`, исходный HEAD `0648943`.
Изменения локальны, без commit. Сохранены прежние staged/untracked файлы,
миграционный отчёт и `.kilo/`. Код #28/#29 и resume payload не менялись.

## Изменения

- `infra/deerflow/vendor/`: unchanged provider из upstream PR #8053 на
  `88cddf6c56402fd3b938a2f105a03ac9c86b2d0d`, лицензии, installer с SHA256.
  Backport относительно pinned v2.2.0 — 111 добавленных/62 удалённых строки
  provider; остальные upstream-модули не заменялись.
- `infra/deerflow/runtime/setupaiv2_deerflow/`: наследник встроенной модели,
  per-request credential snapshot, proactive refresh через официальный
  Codex app-server, межпроцессный lock, marker неопределённого refresh,
  локальные команды status/recover. Собственный OAuth-протокол не реализован.
- `Dockerfile.codex`, `tools/build_deerflow_codex.py`: отдельная сборка с
  pinned backend и Codex 0.172.0, checksum verification, network-disabled RUN.
- `tools/deerflow_platform.py`: явный candidate profile `--profile codex`,
  единственная модель без API key/fallback, provider/middleware attempts=1,
  два отдельных auth volumes. Старый CLI-профиль сохранён до cutover.
- `tests/deerflow/`: synthetic refresh/protocol/tool/error/timeout/cancel tests,
  companion regressions exact upstream commit и реальный offline handshake
  Codex binary без credentials. `tests/test_deerflow_platform.py` проверяет
  отсутствие чтения API credentials при подготовке кандидата.
- `.github/workflows/deerflow-codex.yml`: воспроизводимый build/test job без
  model credentials; файл подготовлен, удалённый CI не запускался.

Эксплуатация, pinned inputs, команды и ограничения:
[infra/deerflow/CODEX.md](../../infra/deerflow/CODEX.md).

## Конкретный подготовленный артефакт

Backend candidate image ID:
`sha256:4d999f205909f4422f9ccb0e7cc1bf89cd83a36456bbd9ac44cfda8f716882cc`.
Локальный tag: `setupaiv2-deerflow-codex:local`; runtime Compose использует ID.
База:
`ghcr.io/bytedance/deer-flow-backend@sha256:0eff3cb395700c5bfee09a6bc57f878dcdc6232591f2210896e5fe5fdf9c145b`.

Сгенерированы `.scratch/deerflow-codex/image.json` и `compose.json`, отдельные
конфигурации Fortune/Orbita и snapshots HEAD. Auth volumes только объявлены,
настоящие login/credentials не импортированы. Модель `gpt-6-luna` перенесена
как прежний идентификатор конфигурации; доступность по подписке не проверялась.
Candidate — отдельная среда для приёмки, а не незаметный перенос существующей
памяти/SQLite. Дальнейший cutover должен сохранить рабочие данные и учесть
одинаковые host ports; такой перенос в этом задании не выполнялся.

## Доказательства

| Проверка | Результат и граница |
|---|---|
| SHA256 официального Codex archive | Совпадает с release digest `a2342a8f…be392` |
| Offline image build | PASS; исходный/заменяемый provider проверены по checksum |
| Целевая suite внутри итогового immutable image | **301 passed**; 13.83 s; exit 0; без внешней сети/credentials |
| Генератор на Windows | **3/3 PASS**, включая legacy isolation и новый subscription profile |
| Compose `config --quiet` | PASS, без запуска сервисов и вывода секретов |
| Embedded checkpoint/resume на новом образе | PASS: 6 процессов, две отдельные SQLite, один synthetic model call на проект, 0 live calls/external writes; Gateway restart не запускался |
| AST parse изменённых Python и новых тестов | PASS, 13 файлов; vendor provider отдельно исполнялся контейнерными тестами |
| `git diff --check` | PASS; только обычные предупреждения LF/CRLF |

301 тест проверяет реальные upstream функции сериализации и SSE, обычный
и async invoke, tool strict/choice, terminal events/retry-after, отмену до
headers и во время SSE с **peer-observed EOF**, repeated cancellation cleanup,
deadline, refresh rotation восьми конкурирующих вызовов, sticky failure,
kill/reap зависшего refresh subprocess, изменение/пропажу auth-файла,
account mismatch и отсутствие токенов/ответов сервера в диагностике.
Embedded Harness с LLM middleware при auth failure выбрасывает безопасный
сигнал остановки без записи успешного AI-ответа. Factory принимает профиль
наследника и сохраняет нативную Codex-ветвь конфигурации.

Real Codex binary в тесте выполняет только initialize/account-read без
credentials, с `refreshToken=false`, при `--network none`. Обновление
настоящего OAuth проверено **не было**: renewal tests используют отдельный
stdio subprocess-fixture, записывающий искусственный auth-файл.

### Промежуточные проблемы проверок

- Первый запуск расширенных upstream-тестов из v2.2.0 дал 54 ошибки и 209
  успешных проверок: старые async fixtures подменяли только sync HTTP.
  Нативный async backport требует companion tests того же commit; они
  сохранены без изменений и включены в итоговые 299 PASS. Контейнер был
  без сети, реальные credentials отсутствовали; внешних запросов не было.
- Первые локальные Windows-проверки встретили sandbox-denied TEMP/uv cache;
  те же тесты выполнены с разрешённым доступом к штатному временному каталогу.
- Полная host suite сначала обнаружила отсутствующие sqlite-checkpointer и
  tree-sitter. `uv sync --locked --offline` восстановил 5 уже закреплённых
  пакетов из кэша; `pyproject.toml` и `uv.lock` не менялись.
- После восстановления: **143 tests, 5 errors, 15 skipped** в едином процессе.
  Все пять ошибок — `git init` exit 128 в fixture setup (`test_spans`: 2;
  `test_t15_integration`: 3). При отдельном запуске эти файлы дали **2/2 и
  3/3 PASS**. Причина зависимости от порядка/окружения полностью не установлена;
  полная suite не объявляется зелёной. Эти тесты не менялись в рамках интеграции.
  Локальный лог: `.scratch/codex-integration/host-tests.log`.

### Устранение пяти ошибок Windows-suite

В последующем задании 27.11.2026 причина воспроизведена без DeerFlow:
`SettingsTests().settings()` → `git init`. Git сообщил
`missing config value GIT_CONFIG_VALUE_0`, exit 128. Даже пустой
`patch.dict(os.environ, {}, clear=True)` без создания Settings воспроизводил
ошибку. Python-словарь после выхода из patch совпадал с исходным, но дочерний
Git с наследуемым Windows environment терял пустое значение. Передача явного
`env=dict(os.environ)` восстанавливала успешный `git init`. Таким образом,
причина — изменение native environment тестовой fixture, а не Git-репозиторий,
учётные данные или интеграция Codex.

В `tests/test_service_settings.py` fixture теперь временно заменяет Python-view
`os.environ` обычным пустым словарём через `patch.object`, не очищая окружение
процесса. Добавлены проверки сохранения пустой Git config variable в реальном
дочернем процессе и изоляции Settings от ambient credentials. Регрессия сначала
дала FAIL с исходным fixture, после исправления минимальная совместная suite
`test_service_settings` + `test_spans` + `test_t15_integration` дала 12/12 PASS.
Ни пользовательская Git-конфигурация, ни product-код не менялись.

Документированный механизм: изменения элементов `os.environ` вызывают
`putenv`/`unsetenv`; подмена атрибута объектом словаря этого не делает.
[Python 3.13: os.environ](https://docs.python.org/3.13/library/os.html#os.environ),
[unittest.mock patch.object](https://docs.python.org/3.13/library/unittest.mock.html#patch-object).
Context7 `/python/cpython`, запрос 3.13, вернул документацию 3.13.14;
эксперимент выполнен на установленном CPython 3.13.18 Windows.

Итоговый штатный запуск **всех тестов вместе, одним процессом**:
`.venv/Scripts/python.exe -m unittest discover -s tests -v` —
**145 tests, OK (skipped=15), exit 0**, 152.670 s. То есть 130 тестов успешно,
15 существующих пропусков платформенных проверок; новые skips не добавлялись.
Все пять прежних `git init` ошибок устранены. Лог:
`.scratch/codex-integration/windows-suite-fixed.log`.
`git diff --check` также прошёл. Live-запуски, сервисы и авторизация не затрагивались.

## Ревью и исправления перед коммитом

Проверена незакоммиченная интеграция относительно `0648943`; прежние изменения
архитектуры, миграции, #28/#29 и `.kilo/` не включались в область ревью.
Пять направлений проверены отдельными агентами, исправления повторно просмотрены.

### Standards

Одно замечание P2: marker заявлен durable, но fsync файла не сохранял запись
родительского каталога. Исправлено; иных нарушений стандартов не обнаружено.

### Spec

Новых блокирующих несоответствий заданию нет. Подготовка native provider,
refresh, отмена и Windows fix соответствуют запросу; live и полный цикл
не объявлены принятыми.

### Security

Конкретных уязвимостей в проверенных изменениях не обнаружено. Проверены
границы file tools, volumes, вывод ошибок, фиксированные build inputs и RPC.
Это статический аудит данной интеграции, не приёмка всей автономной системы.

### Business Logic

Одно P2, совпадающее со Standards: до refresh теперь выполняется fsync
каталога; сбой запрещает RPC. После снятия marker, включая owner recovery,
каталог также синхронизируется. Два новых теста проверяют порядок и отказ
до внешнего действия при ошибке fsync. Повторное ревью: замечание закрыто.

### Duplication + Dead Code

Два P2 устранены: сборщик проверяет соответствие Dockerfile manifest,
CI читает base из manifest; каждая сборка получает новый временный контекст
без bytecode и удалённых исходников. Регрессия воспроизводит две сборки
после удаления модуля и проверяет отказ при расхождении base до Docker.
Повторное ревью: открытых замечаний нет.

Итого три уникальных дефекта устранены. Дополнительно path filters CI
охватывают генератор конфигурации и host-тесты сборщика/генератора.
Для стандартных API использован Context7 `/python/cpython`, запрос версии
3.13, выдана 3.13.14: [tempfile](https://docs.python.org/3.13/library/tempfile.html),
[shutil](https://docs.python.org/3.13/library/shutil.html),
[os.fsync](https://docs.python.org/3.13/library/os.html#os.fsync).

После исправлений: **301 Linux tests PASS**, **146 Windows tests, OK
(skipped=15), exit 0**, 157.567 s; то есть 131 Windows-тест успешно.
Полная Windows-suite выполнена одним процессом, лог
`.scratch/codex-integration/windows-suite-review.log`. На финальном образе
повторно PASS checkpoint/resume (шесть процессов, два SQLite), Compose config
и `git diff --check`. Настоящая авторизация/refresh/inference не выполнялись.

Набор для коммита подготовлен отдельно от существующего индекса:
`.scratch/codex-integration/reviewed-changes.patch`, сопроводительный состав
и сообщение — `commit-preparation.md` в том же каталоге. Основной индекс
с ранее staged `tests/fixtures/selection/gh` сохранён. Смешанные README
в patch содержат только добавленные этим заданием ссылки; прежний план,
CONTEXT/ADR и их изменения остаются в рабочей копии. Их согласованную
публикацию нужно учитывать при формировании общего PR. Коммит не создан.

## Что закрыто и что ещё требует приёмки

**Закрыто офлайн:** наличие воспроизводимой интеграции, контракт refresh через
официальный CLI, сериализация обновления, перечитывание файла без перезапуска
общей модели, безопасная остановка/локальное восстановление после неопределённой
ротации, реальное закрытие клиентского HTTP при cancel и отсутствие скрытого
повтора inference/fallback.

**Не подтверждено live:** первичная авторизация владельца в выделенные volumes,
серверная валидность/квота, модель по подписке, настоящий refresh и его сетевые
условия, Gateway restart, отмена удалённой генерации/расхода. JWT expiry —
локальная подсказка для renewal, не проверка подлинности или прав аккаунта.

Отдельно остаются controller handoff, остановка с `cycle.stop_reason`, runner,
публикация PR, независимое review/CI, merge по политике и следующая Issue.
Нет утверждения о готовности полного ночного цикла или production cutover.

Источники: [исследование](codex-deerflow-integration-research-20261127.md),
[DeerFlow PR #8053](https://github.com/bytedance/deer-flow/pull/8053),
[Codex release 0.172.0](https://github.com/openai/codex/releases/tag/rust-v0.172.0),
[app-server auth](https://learn.chatgpt.com/docs/app-server#auth-endpoints).
