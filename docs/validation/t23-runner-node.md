# T23: тесты в runner-контейнере (DinD), lifecycle и reconciliation

Тикет: [#23](https://github.com/prmAim/setupAIv2/issues/23). Образ, кэши и сетевая
модель — из #21 (`infra/runner/`, image `sha256:60339a5f…`).

## Что сделано

`tools/container_test_runner.py`:

- `ContainerTestRunner(run_id, repo_path, profile)` — `start()`, `exec(cmd, timeout_s)`,
  `stop(cleanup=True)`, `reconcile()`, плюс `run_profile(profile, timeout_s, log_path)`.
- `DockerFacade` + `SubprocessDocker`: все вызовы Docker — argv, без `shell=True`;
  host socket не монтируется, CLI docker в runner отсутствует.
- `PROFILES`: `fast` — ruff check + ruff format --check + unittest + orbita-check;
  `full` — frontend `npm ci --ignore-scripts --offline && npm run build`, затем
  backend `./mvnw -B -ntp spotless:check verify`. `--offline` обязателен: фаза тестов
  сидит в internal-сети без egress (#21); пакеты берутся из прогретого npm-кэша.
- Граф: `TaskSpec.runner` (`RunnerConfig`: `run_id`, `fast_timeout_s`,
  `full_timeout_s`, `full`). При заданном `runner` `test`-узел выполняет fast-профиль,
  `gates`-узел — full-профиль, оба в одном runner-контейнере. Задания без `runner`
  (герметичные фикстуры unit-тестов) сохраняют прежний хостовый путь — это не
  production-режим.

## Lifecycle

`start()`:

1. `docker version` с exit≠0 → состояние `unavailable`, `exec` вернёт `not_run`.
2. Если живой dind другого `run_id` уже держит общий volume `orbita-runner-dind` →
   `failed` с причиной (один контейнерный стек на хост; иначе два dockerd пишут в
   один `/var/lib/docker`).
3. `reconcile()`: `docker ps -a --filter label=agent-run=<run_id>`. Dind — берётся
   первый (запускается, если остановлен), лишние dind этого run удаляются. Runner —
   удаляются все (пересоздаются), чтобы не было двух параллельных runner.
   Контейнеры с другим `agent-run` не трогаются.
4. Создаётся internal-сеть `runner-net-<run_id>`; dind — privileged,
   `dockerd --host=tcp://0.0.0.0:2375 --tls=false`, alias `dind`, volume
   `orbita-runner-dind:/var/lib/docker`; дополнительно подключается egress-сеть
   `runner-egress-<run_id>` (pull образов, как в warm-фазе #21).
5. Ожидание готовности dind (`docker -H tcp://127.0.0.1:2375 version`), затем runner:
   сеть только internal, `sleep infinity`, `-w /work`.

Повторный `start()` при живом runner — no-op (проверка по `docker inspect`).
`stop(cleanup=True)` удаляет runner, dind и обе сети; named volumes и кэши остаются.

## Env/изоляция (снято с живого контейнера)

```
RUNNER_UID=1000
codex=absent
docker-sock=absent
docker-cli=absent
DOCKER_HOST=tcp://dind:2375
TESTCONTAINERS_HOST_OVERRIDE=dind
secrets=absent
```

`docker inspect` runner: `Privileged=false`, сеть ровно одна
(`runner-net-t23-iso-evidence`), mount `/work` — только чекаут, плюс named volumes
`orbita-runner-{uv-cache,uv-venv,npm-cache,m2,uv-python}`; `/var/run/docker.sock`
отсутствует. `target` и `src/main/resources/static` — tmpfs (EPERM на virtiofs, #21).

## Evidence

Каждый контейнерный gate пишет в `gate-<name>.json` и `report.json`:
`verdict` (pass/fail/timeout/not_run), `container_id`, `image_id`,
`platform` (`linux/amd64`), `duration_ms`, `exit_code`, `log_path`. Полные логи —
`tests.log` (fast) и `full.log` в evidence-каталоге задачи.

## Замеры (orbita baseline f16e569)

| Прогон | Gate | duration_ms | Результат |
|---|---|---|---|
| граф `t23-orbita-graph` | fast (`tests`) | 14 766 | pass, `Ran 58 tests … OK (skipped=1)` |
| граф `t23-orbita-graph` | full (`full`) | 535 033 | pass, `Tests run: 268`, `BUILD SUCCESS` |
| probe (прямой runner) | fast | 17 502 | pass |
| probe (прямой runner) | full | 586 433 | pass |
| resume `t23-kill-resume` | fast / full | 14 240 / 529 129 | pass |

Container/image в отчёте графа: `06b8d11e2b14…` /
`sha256:60339a5f03c5ac76717f8641161e4bfa7ea581af2d720824add147a03621d59e`,
`platform=linux/amd64`; full-лог — `C:\Git\_wf\t23-evidence\t23-orbita-graph\full.log`.

## Reconcile и kill

1. Граф запущен, дождались runner во время fast-гейта, процесс убит
   (`taskkill /F /T`): контейнеры остались — dind `a86e78952acb`, runner
   `57f5b0d7758c`; `report.json` отсутствует.
2. `--resume` с тем же `run_id`: reconcile подхватил dind `a86e78952acb` (id не
   изменился), удалил убитый runner и создал один новый `eeda84fdfea9`. Наблюдение
   раз в секунду: всё время ровно 1 dind и 1 runner. Resume завершился `pass`
   (`resume.next = ["test"]`, `evidence_writes=1`), после — контейнеров нет.

Unit-тесты на fake-Docker дополнительно фиксируют: adopt dind, пересоздание runner,
схлопывание дублей dind, отсутствие второго runner, неприкосновенность чужих
`run_id`, timeout → kill, недоступный Docker → `not_run` без хостового fallback.

## Docker unavailable

`docker version` exit≠0 (в тесте — несуществующий бинарь через `DOCKER_BIN`):
`start()` → `unavailable`, `exec()` → `verdict=not_run`, причина в `detail` и в логе,
ни одного `docker run` не выполнено. Хостовый запуск тестов не подставляется.

## Тесты

`uv run --locked python -m unittest discover -s tests -v` — 66 тестов, OK (126.7 с,
включая 3 интеграционных с реальным Docker: fast на baseline, adopt dind при том же
`run_id`, изоляция runner).

## Что не проверено

- Параллельные `run_id` на одном хосте: общий `orbita-runner-dind` сериализуется
  guard'ом, отдельные dind-volume не делались.
- ARM64 и не-Windows хосты.
- Рестарт Docker Desktop посреди прогона (наблюдался в #21) — специально не
  воспроизводился.
- Задачи без `runner` (герметичные фикстуры) по-прежнему выполняют команды на хосте;
  это compatibility-путь unit-тестов, не production-профиль orbita.
- Compromised тест-код в runner имеет root внутри dind (граница — runner/host, #21).