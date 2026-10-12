# Реестр приёмки и разделение работ — 12 октября 2026

Снимок GitHub на 12 октября 2026. Все 23 открытые issue учтены; PR учитываются отдельно. Проверены bodies, последние комментарии и delivery receipts. Статусы ниже — аналитическая классификация, а не изменение GitHub labels или assignees. Само наличие слова «complete» в комментарии не закрывает issue.

## Основание

- Исходный master: `2be4e2bbc488aa0072421be31ceda67d83eaa2b6`, [CI 37776470448](https://github.com/ozand/eeebot-ops-dashboard/actions/runs/37776470448): 1457 passed, 57 skipped. Среди пропусков — browser и parent-monorepo проверки.
- D2 #318 смержен; final head `cddeea28bafdc2aed05d4170093270b82d1cff23`. Private host activation этим не подтверждена.
- Публичный артефакт прочитан из gh-pages commit `361303aeadbb9c38818d44e31bc86251ebf01766`: generator `2be4e2b`, generated 2026-10-12 05:16:23 UTC, snapshot `1791782168-17422b035e21`. Это проверка сохранённого HTML в GitHub, не live HTTP или eeepc.
- [#327](https://github.com/ozand/eeebot-ops-dashboard/issues/327) и [#413](https://github.com/ozand/eeebot-ops-dashboard/issues/413) закрыты и не входят в открытый реестр.

## Открытые задачи

| Issue | Аналитический статус | Доказательство / граница | Следующий шаг | Предлагаемый исполнитель |
|---|---|---|---|---|
| [#329](https://github.com/ozand/eeebot-ops-dashboard/issues/329) | Готово к приёмке | #330: scanner fixes; комментарий завершает техническую приёмку · [receipt](https://github.com/ozand/eeebot-ops-dashboard/issues/329#issuecomment-6032993569) | Зафиксировать решение владельца; повторная реализация не нужна | Владелец / репозиторий |
| [#338](https://github.com/ozand/eeebot-ops-dashboard/issues/338) | Код принят | #405: три scoped redaction-length fixes; full-render regressions · [receipt](https://github.com/ozand/eeebot-ops-dashboard/issues/338#issuecomment-6010760830) | Приёмка и закрытие scope | Владелец / репозиторий |
| [#364](https://github.com/ozand/eeebot-ops-dashboard/issues/364) | Код принят | #401: generator SHA в digest; CI и regression receipt · [receipt](https://github.com/ozand/eeebot-ops-dashboard/issues/364#issuecomment-6009828052) | Закрыть code scope; live republish учитывать отдельно | Владелец / репозиторий |
| [#374](https://github.com/ozand/eeebot-ops-dashboard/issues/374) | Публичный случай подтверждён | #376 и public capture 7 октября; missing latest проверен тестами · [receipt](https://github.com/ozand/eeebot-ops-dashboard/issues/374#issuecomment-6035994046) | Принять тестовый и публичный scope | Владелец |
| [#382](https://github.com/ozand/eeebot-ops-dashboard/issues/382) | Код принят | #398: stderr warning, non-raising swap и regression · [receipt](https://github.com/ozand/eeebot-ops-dashboard/issues/382#issuecomment-6009823698) | Закрыть code scope без требования нового host rollout | Владелец / репозиторий |
| [#385](https://github.com/ozand/eeebot-ops-dashboard/issues/385) | Приёмка заявлена | #387: fault fixtures и presentation receipts; полный независимый audit здесь не повторялся · [receipt](https://github.com/ozand/eeebot-ops-dashboard/issues/385#issuecomment-6009885933) | Сверить evidence с критериями и принять | Владелец / репозиторий |
| [#388](https://github.com/ozand/eeebot-ops-dashboard/issues/388) | Готово к приёмке | #389: clock fix; UTC/Moscow verification receipt · [receipt](https://github.com/ozand/eeebot-ops-dashboard/issues/388#issuecomment-5987717358) | Принять тестовое исправление | Владелец / репозиторий |
| [#390](https://github.com/ozand/eeebot-ops-dashboard/issues/390) | Публичный случай подтверждён | #392 и recorded public proposed-only row · [receipt](https://github.com/ozand/eeebot-ops-dashboard/issues/390#issuecomment-5985338984) | Принять bounded presentation scope | Владелец |
| [#394](https://github.com/ozand/eeebot-ops-dashboard/issues/394) | Код принят | #383: absent-root/public-only/seed tests; live host excluded · [receipt](https://github.com/ozand/eeebot-ops-dashboard/issues/394#issuecomment-6009830836) | Закрыть code scope; private rollout отдельный этап | Владелец / репозиторий |
| [#399](https://github.com/ozand/eeebot-ops-dashboard/issues/399) | Документ поставлен | #400; checkpoint обновлён в #416 · [receipt](https://github.com/ozand/eeebot-ops-dashboard/issues/399#issuecomment-6008789448) | Принять актуализированный документ; это не D2 rollout | Владелец / репозиторий |
| [#402](https://github.com/ozand/eeebot-ops-dashboard/issues/402) | Импорты и public-only подтверждены | #403; обычный publisher ea035fb, 8 matching assets, host root absent · [receipt](https://github.com/ozand/eeebot-ops-dashboard/issues/402#issuecomment-6009696642) | Принять flat-import scope; не заявлять private activation | Владелец |
| [#406](https://github.com/ozand/eeebot-ops-dashboard/issues/406) | Публичные labels подтверждены | #407; limited visible-row receipt, точное время GET неизвестно · [receipt](https://github.com/ozand/eeebot-ops-dashboard/issues/406#issuecomment-6027967211) | Принять labels scope; не расширять доказательство на весь publisher | Владелец |
| [#408](https://github.com/ozand/eeebot-ops-dashboard/issues/408) | Публичные timing подтверждены | #409; captured row отдельно cycle/bridge/LLM durations · [receipt](https://github.com/ozand/eeebot-ops-dashboard/issues/408#issuecomment-6029061172) | Принять bounded timing scope | Владелец |
| [#316](https://github.com/ozand/eeebot-ops-dashboard/issues/316) | Код смержен; rollout открыт | #318 final cddeea28; #403 убрал импортный блокер · [receipt](https://github.com/ozand/eeebot-ops-dashboard/issues/316#issuecomment-6009400297) | Проверить installed SHA, private serving, audience/auth и rollback | Локальные агенты |
| [#391](https://github.com/ozand/eeebot-ops-dashboard/issues/391) | Код смержен; длительный запуск не проверен | #393/#411; --once receipt, no 24-hour run · [receipt](https://github.com/ozand/eeebot-ops-dashboard/issues/391#issuecomment-6033224288) | Провести bounded длительную проверку cadence/deadline/lock lifecycle | Локальные агенты |
| [#395](https://github.com/ozand/eeebot-ops-dashboard/issues/395) | Частично поставлено | #412: latest ledger observation; current status unknown · [receipt](https://github.com/ozand/eeebot-ops-dashboard/issues/395#issuecomment-6035912278) | Решение по Proposed ADR-004, новому rest path и безопасным полям; затем reader | Владелец → локальные агенты |
| [#386](https://github.com/ozand/eeebot-ops-dashboard/issues/386) | Producer dependency | #2058 в eeebot открыт; consumer contract есть · [receipt](https://github.com/ozand/eeebot-ops-dashboard/issues/386#issuecomment-5976567565) | Подтвердить producer merge/release и SOURCE_COMMIT/schema, затем consumer | Локальные агенты |
| [#365](https://github.com/ozand/eeebot-ops-dashboard/issues/365) | Зависимость / нет delivery receipt | eeebot#1930 открыт; в issue только claim, нет готового PR · [receipt](https://github.com/ozand/eeebot-ops-dashboard/issues/365#issuecomment-5987742110) | Подтвердить prerequisite и провести real-pipeline privacy canary | Локальные агенты |
| [#375](https://github.com/ozand/eeebot-ops-dashboard/issues/375) | План | Нет PR/evidence реализации в issue | Реализовать bounded failed-attempt metadata без raw prompt/response | Локальные агенты |
| [#369](https://github.com/ozand/eeebot-ops-dashboard/issues/369) | Discovery | D1 follow-ups зафиксированы; актуальное воспроизведение не представлено · [receipt](https://github.com/ozand/eeebot-ops-dashboard/issues/369#issuecomment-5876419959) | Воспроизвести каждый пункт на текущем master; ограниченные fixes | Локальные агенты |
| [#381](https://github.com/ozand/eeebot-ops-dashboard/issues/381) | Архитектурное решение | Legacy render_page обслуживает тестовый путь · [receipt](https://github.com/ozand/eeebot-ops-dashboard/issues/381#issuecomment-5894607221) | Выбрать удаление или публичный контракт; затем staged refactor | Владелец → локальные агенты |
| [#339](https://github.com/ozand/eeebot-ops-dashboard/issues/339) | Пауза | Architect pause: warm <=3min; resume warm >3min или cold >10min · [receipt](https://github.com/ozand/eeebot-ops-dashboard/issues/339#issuecomment-5852958423) | Не возобновлять без новых измерений порогов | Локальные агенты / владелец |
| [#410](https://github.com/ozand/eeebot-ops-dashboard/issues/410) | Гипотеза опровергнута | Bounded independent probes не воспроизвели дефект · [receipt](https://github.com/ozand/eeebot-ops-dashboard/issues/410#issuecomment-6032973533) | Решение владельца о закрытии discovery; код по гипотезе не менять | Владелец |

## Порядок приёмки

1. Сверить и принять уже поставленные code/documentation scopes: #329, #338, #364, #382, #388, #394, #399; для #385 сверить reported receipts с полными критериями. Не добавлять host rollout как условие закрытия задачи, которая его исключает.
2. Принять bounded публичные случаи #374, #390, #402, #406, #408 с указанными ограничениями evidence. Не объявлять отсутствующий live corner case проверенным на основании одного present-value capture.
3. Локальным агентам провести private-D2 preflight/acceptance #316 и bounded мониторинг #391. Проверять сеть, proxy и app authorization отдельно; не копировать private payloads в публичный отчёт.
4. Владелец принимает решения по Proposed ADR-004/new rest reader (#395) и legacy renderer (#381). Локальные агенты реализуют принятый scope; #386 зависит от producer #2058, #365 — от #1930.
5. Новая реализация: #375; воспроизведение и ограниченные fixes #369. #339 остаётся на паузе; #410 требует owner disposition после фальсификации гипотезы, а не фикса выдуманной уязвимости.

## Выполнено в рамках этого обновления

Документация D2 актуализирована в [#416](https://github.com/ozand/eeebot-ops-dashboard/pull/416). Review #384 выявил гонку snapshot/state: обновлённая инструкция ждёт стабильную пару ограниченное время и не считает in-flight publisher провалом cutover. Синтетические проверки runbook не являются исполнением команд на eeepc.

GitHub issues в ходе подготовки реестра не закрывались, их метки и назначения не менялись. Host rollout, новый источник данных и изменение runtime относятся к отдельному локальному пакету работ.

