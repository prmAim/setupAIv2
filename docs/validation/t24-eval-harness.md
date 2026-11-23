# T24: Eval-harness (изоляция от истории, скрытые тесты, контрольные прогоны)

Тикет: [#24](https://github.com/prmAim/setupAIv2/issues/24). Набор: `research/golden-5`
(`eval/golden/manifest.json`, задачи 1–5). Код: `tools/eval_harness.py`, тесты:
`tests/test_eval_harness.py`, отчёт прогона: `eval/report-20261123T1200Z.{json,md}`.

## Зачем

«resolved» в #10 должно быть измерением, а не мнением. Gold-патч и скрытые тесты лежат
в той же истории, что и baseline, поэтому harness:

1. строит песочницу из одного baseline-коммита: `git archive <baseline>` → пустой каталог →
   `git init -b main` → один коммит; remote не добавляется, других refs нет;
2. даёт движку только `issue.md` (исключён из `git status` через `.git/info/exclude`) и дерево;
3. применяет `tests.patch` **после** движка;
4. считает `resolved` только если скрытые тесты exit 0, AST-гейт test-integrity pass и
   ни один существующий тестовый файл не изменён/удалён (флаг `test-intrusion`);
5. сверяет метрику контрольными движками `gold` / `noop` / `cheat` (`codex` — выключен до квоты #2).

## Команды

```powershell
cd C:\Git\_wf\setupAIv2-t24
uv run --locked python tools/eval_harness.py list
uv run --locked python tools/eval_harness.py run --tasks 1,2 --modes gold,noop,cheat `
  --full-verify 1 --report-ts 20261123T1200Z
uv run --locked python -m unittest discover -s tests -v
```

`run` пишет `eval/report-<ts>.json` + `.md`, evidence по кейсам — `C:\Git\_wf\t24-evidence\<mode>-task<n>\`
(`isolation.json`, `agent.diff`, `agent-diff-name-only.txt`, `integrity.json`, `hidden-tests.log`,
`hidden-tests.json`, `full-verify.log`, `report.json`), песочницы — `C:\Git\_wf\t24-runs\<mode>-task<n>`
(по умолчанию удаляются).

## Доказательство изоляции истории

Для каждого из 6 кейсов (`isolation.json` и поле `isolation` в отчёте):

```
commits=1            # git log --all --oneline
rev_list_all=1       # git rev-list --all --count
remotes=''           # git remote -v пуст
refs=["refs/heads/main"]
tree_matches_baseline=true   # HEAD^{tree} == <baseline>^{tree}
eval_dir_in_sandbox=false    # gold.patch/tests.patch в песочницу не попадают
```

Пример (task 2, noop): `daae5e7 baseline 2776016af3f2803333feeff2166f386cd1cb88b8`, одна ветка
`main`, remote нет; коммит merge (`c2d8742…`), в котором лежит ответ, недостижим
(`git cat-file -e` в песочнице падает). Юнит-тест
`SandboxIsolationTests.test_sandbox_has_one_commit_and_no_future_history` фиксирует это же
свойство, включая недостижимость merge-коммита.

Восстановление дерева: `git add -A --force` (tracked-но-ignored файлы не теряются) и
`git update-index --chmod=+x` для 100755-путей (на Windows tar теряет exec-бит,
`backend/mvnw` — единственный такой файл).

## Контрольные прогоны (2026-11-23)

| задача | язык | режим | verdict | reason | engine calls | wall ms | hidden exit | intrusion | trace |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 `orbita-42-sdk-evidence` | java | gold | resolved | hidden green; integrity clean | 1 | 43939 | 0 | нет | `38875499661c0972bfeb61550394da1a` |
| 1 | java | noop | fail | hidden-tests | 1 | 18290 | 1 | нет | `6e0f6eed2a9c12391ae5fc448ab38c20` |
| 1 | java | cheat | fail | test-intrusion | 1 | 18425 | 1 | **да** | `3b451c37d2017fa38e6960182a7b6176` |
| 2 `orbita-50-notification-retry` | python+java | gold | resolved | hidden green; integrity clean | 1 | 17436 | 0 | нет | `6ff65b4e14a7838ba6b80de754bd0660` |
| 2 | python+java | noop | fail | hidden-tests | 1 | 14916 | 1 | нет | `3ad5f68191965441ce03ec35664698c9` |
| 2 | python+java | cheat | fail | test-intrusion | 1 | 15214 | 1 | **да** | `3c04c7a78197c1e2b678d9bf15561267` |

`mismatches: []` — 2/2 resolved в gold, 0/2 в noop, 0/2 в cheat с флагом intrusion.

- Java-скрытые тесты: `mvnw.cmd -B -ntp -o -Dtest=ru.orbita.broker.OrderSdkEvidenceTest,ru.orbita.broker.OrderSdkTlsTest test`
  (38.1 с, 27 тестов, 0 падений). Полный verify 15 раз не гоняется.
- Python-скрытые тесты (task 2): `python -m unittest discover -s tests -p test_dev_checks.py -v` (10.2 с).
  Java-файлы `tests.patch` применяются, но java-часть задачи покрывается full-гейтом (в отчёте — note).
- Cheat — реальное ослабление (`assertThat(page.total()).isEqualTo(2)` → `assertTrue(true)`
  в `AccountReadIT.java`; `self.assertEqual(… )` → `assertEqual(1, 1)` в `tests/test_configuration.py`),
  фиксируется дважды: строгим `git status`-правилом (test-file-modified) и AST-гейтом
  (`test-body-modified=1`).

## Полный verify в контейнере (одна Java-задача)

Task 1 gold: `ContainerTestRunner(run_id=t24-task1-gold, profile="full")` →
frontend `npm ci --offline && npm run build`, backend `./mvnw -B -ntp spotless:check verify`:

```
verdict=pass exit=0 duration=433144 ms
container=edde742a002e9f258958a3820932372c27e7942ae9d1408e98c8da3316a13f39
image=sha256:60339a5f03c5ac76717f8641161e4bfa7ea581af2d720824add147a03621d59e
platform=linux/amd64
157 IT, 0 failures; BUILD SUCCESS (07:03 min)
```

Лог: `C:\Git\_wf\t24-evidence\gold-task1\full-verify.log`. Хостовый fallback не используется:
недоступный Docker даёт `not_run` (наследие #21/#23).

## Метрики и отчёт

На кейс: `verdict`, `reason`, `iterations`, `engine_calls`, `wall_ms`, `trace_id`, `session_id`,
`order` (isolate → agent → integrity → hidden → report), `test_integrity` (verdict, intrusion,
findings, AST summary), `hidden_tests` (exit, duration, targets, tests.patch exit, tail, log),
`isolation`, `full_verify`, `evidence`. Сверху: `meter.expected`, `digests` (sha256 `issue.md`,
`gold.patch`, `tests.patch`, `task.json`) и `mismatches`.

Digests пишутся один раз в `eval/golden-digests.json` и затем проверяются fail-closed
(тест `test_digest_drift_is_fatal`); объявленный в `task.json`/manifest `sha256` тоже
проверяется (`test_declared_sha256_mismatch_is_fatal`).

## Тесты

`uv run --locked python -m unittest discover -s tests -v` — 79 тестов, OK (см. коммит).
Из них 13 — harness: загрузка manifest/sha256, дрейф digests, изоляция (1 коммит, нет remote,
merge недостижим), порядок «agent → tests.patch», gold/noop/cheat/codex, отчёт.

## Что не проверено

- Реальный `codex`-движок: режим возвращает `not_run` (`engine:codex_disabled`) до квоты (#2);
  это единственный оставшийся вход в #10.
- Задачи 3–5 в контрольную матрицу не входят (DoD — минимум 2 задачи); harness их принимает.
- Параллельные прогоны: песочницы независимы, но runner сериализуется общим dind-volume (#21).