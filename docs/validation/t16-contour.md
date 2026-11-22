# T16: контур без LLM

Проверяемо локально, без сети GitHub и без вызова модели.

## diff-scope

Правила лежат в `tools/diff_scope_rules.json`; профиль по умолчанию — `orbita`.
Задача может передать `diff_scope_profile`, явный `diff_scope_rules` или
legacy-поле `forbidden_paths`. Любое попадание в `forbidden` — deny (exit 1);
путь вне `allow` при `enforce_allow=true` — тоже deny (`out-of-scope`).
Сравнение путей регистронезависимое, `\` приводится к `/`.

## Идемпотентный push/PR

`tools/publish.py`: ветка `agent/<issue>-<run_id>`, маркер
`<!-- agent-run:<run_id> -->`. Повторный прогон переиспользует локальную или
удалённую ветку, не коммитит пустой diff, делает идемпотентный `git push`;
существующий PR с маркером подхватывается вместо создания второго.
`gh` вызывается через `GitHubClient`; бинарь задаётся `GH_BIN`.

## Codex за PatchReport

`tools/codex_engine.py` запускает `exec --sandbox workspace-write
--output-schema <schema.json>` в cwd репозитория. Бинарь — `CODEX_BIN`.
Валидный JSON даёт `PatchReport(engine="codex")`; невалидный JSON,
ненулевой exit и таймаут возвращаются как `engine_error`
(`invalid_json`, `nonzero_exit`, `timeout`), stderr обрезан до 2000 символов
и редактирован. По таймауту дерево процессов убивается (`taskkill /F /T`).

## Спаны

Каждый вызов движка — generation-спан (`engine`, `cli_version`,
`prompt_sha256`, `output_bytes`, `task_id`). Каждая команда git/тестов — tool-спан
(`cmd`, `exit`, `duration_ms`). `session_id` = `task_id`; resume открывает новый
trace и ссылается на предыдущий через `previous_trace_id`. `NO_PROXY` для
localhost выставляется перед экспортом.

## fix-петля

Только объявленные `fix_patches`, не больше трёх попыток; после третьей
неудачи — `escalated`, без четвёртого вызова движка.