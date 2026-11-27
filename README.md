# setupAIv2

Автономный агент разработки: от очереди GitHub Issues до проверенных изменений,
слияния в `main` и перехода к следующей задаче.
**[Главная цель и полный цикл работы](CONTEXT.md)** — основной ориентир проекта.

Выбранная исполнительная платформа — **DeerFlow Runtime Harness** для Fortune
и Orbita. Причины и границы решения: [ADR-0001](docs/adr/0001-deerflow-runtime.md).

Продолжение разработки: [план автономного цикла](docs/plans/deerflow-autonomous-cycle.md).

Исследование интеграции: [готовые пути Codex + DeerFlow и предложение упрощения](docs/validation/codex-deerflow-integration-research-20261127.md)
— сравнение на 27.11.2026, офлайн-доказательства и границы будущей живой проверки.

Подготовленная интеграция: [CodexChatModel, refresh и отмена — офлайн-проверки](docs/validation/deerflow-codex-integration-20261127.md).

## Текущее окружение

Python-окружение для LangGraph, LangChain, Pydantic и локального Langfuse.
Модель для первых проверок — OpenAI `gpt-6-luna` через Responses API.
Приложение: `aim_setupaiv2`, окружение: `development`.

## Установка

Один раз установите [uv](https://docs.astral.sh/uv/) для пользователя:

```powershell
winget install --id astral-sh.uv --exact --source winget --scope user
```

Откройте новый терминал. Из корня репозитория:

```powershell
uv sync --locked
uv run --locked python tools/check_environment.py
uv run --locked python -m unittest discover -s tests -v
```

Python закреплён в `.python-version`, зависимости — в `uv.lock`.
`uv` создаёт проектную `.venv`; активация при `uv run` не нужна.
Проверка окружения работает без Docker и API-ключа, не отправляет трассы.

## Локальная наблюдаемость

Нужен запущенный Docker Desktop с Linux containers и Docker Compose.

```powershell
uv run --locked python tools/init_local_observability.py
docker compose --env-file infra/langfuse/.env -f infra/langfuse/compose.yaml up -d --wait --wait-timeout 240
uv run --locked python tools/check_services.py
```

Инициализация создаёт случайные пароли и ключи один раз, существующие файлы
сохраняет. Конфигурация сервера: `infra/langfuse/.env`, приложения: `.env`.
Оба файла исключены из Git. `.env.example` содержит только названия настроек.
Не копируйте `.env.example` поверх созданного `.env`: потеряются ключи Langfuse.

Интерфейс: <http://localhost:3000>. Пользователь `admin@aim-setupaiv2.local`,
пароль — значение `ADMIN_PASSWORD` из `infra/langfuse/.env`.
Проект `aim_setupaiv2` создаётся автоматически. Регистрация отключена.

`check_services.py` использует искусственный ответ без оплаты API. Проверяет
аутентификацию, выполняет LangGraph с callback Langfuse и читает сохранённую
трассу обратно, сверяя результат и наличие генерации модели. LangSmith-трассировка
явно выключена; его SDK остаётся установленным.

Эксплуатация: [infra/langfuse/README.md](infra/langfuse/README.md).

## Реальная модель

В OpenAI Platform создайте отдельный проект `aim_setupaiv2-development`,
настройте Billing и лимиты расходов, создайте ключ проекта с правом создавать
Responses. Для приложения предпочтителен service account.
Сохраните ключ в корневом `.env` после `OPENAI_API_KEY=`. Не отправляйте его в чат.

```dotenv
APP_NAME=aim_setupaiv2
APP_ENV=development
LLM_PROVIDER=openai
LLM_MODEL=gpt-6-luna
OPENAI_API_KEY=your-local-key
```

Один реальный запрос (может расходовать баланс API):

```powershell
uv run --locked python tools/check_services.py --live
```

Это короткий синтетический запрос, максимум 256 выходных токенов, таймаут
30 секунд, без автоматических повторов. Ответ и трасса должны сохраниться
локально. `store=false` отключает сохранение Response для последующего получения;
это не является обещанием нулевого хранения данных провайдером.

Провайдер — сервис, выполняющий модель. LangGraph и LangChain не привязывают
приложение навсегда к OpenAI. В проверке предусмотрен отдельный endpoint
DeepSeek и отдельный `DEEPSEEK_API_KEY`: при смене `LLM_PROVIDER=deepseek`
нужно также задать доступную модель DeepSeek в `LLM_MODEL`. Совместимость
функций Responses API различается; DeepSeek требует отдельной живой приёмки,
здесь она не выполнена.

`APP_NAME` — метка приложения в трассах. `APP_ENV=development` отделяет учебные
запуски от `staging` и `production`, но само по себе не создаёт изоляцию доступа.
Проект OpenAI отделяет ключи и расходы; проект Langfuse отделяет наблюдаемость.
Их имена не обязаны совпадать.

У локального Langfuse нет выбора региона US/EU: данные находятся в Docker volumes
на этом компьютере. Вызов OpenAI остаётся облачным и передаёт запрос провайдеру.
Локальная наблюдаемость не означает локальный inference.

## Другие проекты

`uv`, установки Python и кэш общие для пользователя. Библиотеки устанавливаются
в отдельную `.venv` каждого проекта, чтобы обновления не ломали соседние проекты.

```powershell
uv init --bare --python 3.13.18
uv python pin 3.13.18
uv add langgraph langchain langchain-openai pydantic pydantic-settings langfuse
```

Для существующего проекта используйте его менеджер зависимостей. `.venv` не
копируйте. Этот сервер Langfuse можно использовать для других локальных проектов:
создавайте отдельный проект и ключи через его интерфейс.

Обновления выполняйте намеренно: `uv lock --upgrade`, затем `uv sync --locked`
и проверки выше. Совместимость пакетов: `uv pip check`. Docker-образы закреплены
по digest; обновлять их следует отдельно с резервной копией данных.

## Документация

- [DeerFlow v2.2.0: Fortune и Orbita — вход, запуск, остановка и ограничения](infra/deerflow/README.md)
- [LangGraph](https://docs.langchain.com/oss/python/langgraph/overview)
- [LangChain](https://docs.langchain.com/oss/python/langchain/install)
- [Pydantic](https://docs.pydantic.dev/latest/)
- [Langfuse Docker Compose](https://langfuse.com/self-hosting/deployment/docker-compose)
- [OpenAI API: первый запрос](https://developers.openai.com/api/docs/quickstart)
- [DeepSeek Responses API](https://api-docs.deepseek.com/guides/responses_api/)
