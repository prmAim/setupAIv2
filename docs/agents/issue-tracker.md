# Issue tracker: GitHub

Задачи и спецификации хранятся в GitHub Issues:
https://github.com/prmAim/setupAIv2/issues

Используй gh CLI с явным `--repo prmAim/setupAIv2`.

## Основные операции

- Создать: `gh issue create --repo prmAim/setupAIv2 --title "..." --body-file <path>`.
- Прочитать: `gh issue view <number> --repo prmAim/setupAIv2 --json number,title,body,state,labels,assignees,comments`.
- Найти: `gh issue list --repo prmAim/setupAIv2 --state open --json number,title,labels,assignees`.
  При необходимости добавляй фильтры `--label` и `--state`.
- Комментировать: `gh issue comment <number> --repo prmAim/setupAIv2 --body-file <path>`.
- Добавить или убрать метку: `gh issue edit <number> --repo prmAim/setupAIv2 --add-label "..."` или `--remove-label "..."`.
- Закрыть: `gh issue close <number> --repo prmAim/setupAIv2`.

Многострочный текст передавай через UTF-8 файл и `--body-file`.
Временные файлы размещай в системном временном каталоге либо
в выделенном игнорируемом каталоге репозитория.

«Опубликовать в трекере» означает создать GitHub issue.
«Прочитать соответствующий тикет» означает получить тело,
метки и комментарии issue.

## Pull requests as a triage surface

**PRs as a request surface: no.**

Issues и PR используют общее пространство номеров. Если тип
ссылки неоднозначен, проверь `gh pr view <number>` с параметром
репозитория; если это issue, используй `gh issue view`.

## Wayfinding operations

- Карта — одна issue с меткой `wayfinder:map` и разделами
  Notes / Decisions-so-far / Fog.
- Дочерние тикеты связываются с картой через GitHub sub-issues.
  Если механизм недоступен, используй список задач в теле карты
  и строку `Part of #<map>` в дочернем тикете.
- Тип тикета обозначается меткой `wayfinder:<type>`:
  research, prototype, grilling или task.
- Блокировки записываются через нативные GitHub issue dependencies.
  Для API используй числовой database id задачи, а не её номер
  или node_id. Если зависимости недоступны, используй строку
  `Blocked by: #<n>, #<n>`.
- Доступный фронт работ: открытые дочерние тикеты без исполнителя
  и открытых блокирующих задач. Выбирай первый по порядку карты.
- При взятии тикета назначь исполнителя:
  `gh issue edit <number> --repo prmAim/setupAIv2 --add-assignee @me`.
- При завершении запиши результат в комментарий, закрой тикет
  и добавь краткий вывод со ссылкой в Decisions-so-far карты.
