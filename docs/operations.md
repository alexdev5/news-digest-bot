# Експлуатаційний контракт

Оновлено 2026-11-11. Telegram/AI/delivery CLI, Dockerfile і Compose реалізовані.
Backup/restore, сайти та зовнішні alerts у цьому runbook лишаються планом.

## Дані для середовища

Підготувати `config/config.json` (schema 4, `target_channels`): цільові канали,
їхні Telegram-джерела, `scan_start_at`, розклади й промпти; account ID і pinned peer IDs.
Сайти ще не читаються. Окремий реєстр прав не потрібен (D-010); budgets не застосовуються (D-026).
Telegram API credentials і OpenAI key зберігаються у secret files, session — окремо.
Коди входу/2FA пароль не залишаються в конфігурації або документації.

Для OpenAI перевірити саме прив'язану API-організацію/проєкт, баланс і доступ
до `gpt-6.1-terra`. Pro-кредити потребують активації; поточний залишок не відомий
цьому проєкту. Не вважати денний бюджет автоматичним місячним обмеженням.

Production target: один Linux Docker host, один non-root worker, local storage.
Початкові 2 vCPU / 2 GB RAM / 10 GB SSD — оцінка v4, не перевірені вимоги.
Окремі writable data/cache/state/session, read-only config, tmpfs для socket/temp,
без зовнішнього admin port, privileged або Docker socket.

Конкретне розгортання на VPS (власний PostgreSQL, скрипти `deploy/`) — у
[deployment.md](deployment.md).

## Перший запуск

1. Локально `validate-config`; перевірити schema 4, clocks/timezone, permissions.
2. Із зупиненим worker: `login` під спільним process lock; перевірити account ID.
3. До запуску worker перевірити `doctor`, `dialogs`, `schedule --all`.
4. `ai-check` — реальний синтетичний виклик OpenAI, що споживає API-кредити.
5. Перевірити ручний/плановий `preview`: всі події, хронологію, посилання на найраніший допис, поділ, entities,
   відсутність insert/guards і cleanup. `preview` не публікує; `run-now` публікує.
6. У тестовому destination перевірити серію, receipts, N confirmed rows і cache cleanup.
7. Виконати fault-injection/backup restore та стендовий прогін, заповнити звіт.
8. Застосувати production config; після цього планові випуски надсилаються
   автоматично, без ручного підтвердження кожної частини.

Під час усіх startup/doctor/healthcheck операцій джерельна історія не збирається.
Idle disconnect і очікування cutoff — нормальна робота, а не збій.

## Операції CLI

Основні операції через `run.cmd` (Windows) або `run.sh` (Linux):

```text
run.cmd validate
run.cmd schedule
run.cmd status --details
run.cmd run-now --channel <id>
run.cmd run-window --channel <id> --from <ISO-дата> --to <ISO-дата> --ignore-published
run.cmd preview --channel <id> --window-end <ISO-дата>
run.cmd reconcile --channel <id>
run.cmd cache-status
run.cmd cache-clean
```

`run-now` читає останній завершений інтервал schedule. За 08/15/21 запуск
о 21:31 бере 15:00–21:00 (D-031); уже покритий інтервал не повторюється.
Якщо worker працює, команда залишає йому запит у `/state/requests/`.
Інші online-команди потребують вільного process lock. `preview`/`scan` мають
окремий inspection-кеш; `--keep` не готує робочу публікацію.

`run-window` також передає запит зайнятому worker; інакше виконується одразу.
Це публікація довільного минулого періоду. `--ignore-published` явно дозволяє
повторно включити новини, вимикаючи ID-фільтр та AI-пам'ять. Без прапорця при
недоступній історії БД run зупиняється (`published_history_unavailable`).
Команди для Windows/Linux/production — [manual-window.md](manual-window.md).

`status` показує обидві межі в timezone каналу; `--details` додає per-source counts,
ID виключених дописів та причини. `ps` показує контейнери Docker.
`reconcile` запускає перевірку pending deliveries одразу, минаючи лише backoff.
Guards, невизначений send і незмінність payload залишаються обов'язковими.
Автоматичні delivery retries працюють і в idle (60–900 секунд), без нового scan/AI.

`prepare`, окремий `publish`, socket, `reload-config` та `backup` з початкового
ТЗ поки не реалізовані. Фактичний інтерфейс — `run.cmd help` і `digest --help`.

## Несправності

| Симптом | Дія |
| --- | --- |
| `AUTH_REQUIRED` | Зупинити Telegram-операції; інтерактивно відновити сесію, без циклу кодів |
| FloodWait | Поважати durable `pause_until`; restart не скидає очікування |
| OpenAI quota/credits exhausted | Діагностика balance/limits; випуск не публікується, alert; без сліпого retry |
| `ai_unprocessed` (лічильник) | Модель не врахувала допис і після окремого повтору; випуск вийшов без нього. ID — у `status --details`; за потреби `run-window` за той період (D-036) |
| `ai_warning_numbers` / `_length` / `_attribution` | Пункт прийнято після однієї спроби виправлення із зауваженням; варто глянути пост |
| `no_post_rights` / `auth_required` у run | Доставку зупиняє причина для власника: поверніть акаунту права адміністратора з публікацією в каналі або виконайте `digest login`. Worker повторює спроби кожні ≤15 хв і продовжить сам; до того `healthcheck` unhealthy, `status` — «ПОТРІБНА ДІЯ ВЛАСНИКА» (D-038) |
| `ai_dedupe_failed` | Перевірка/злиття дублікатів не вдалися; опубліковано перевірені чернетки, можливий дубль |
| `ai_incomplete_coverage` у старому run | Історичний код до D-036 |
| `ai_off_topic` у старому run | Історичний результат старого промпту; новий `group-v4` цю причину забороняє |
| AI rate limit / мережевий збій | Bounded backoff у межах бюджету/deadline; далі пропуск випуску й alert |
| Частина `uncertain` | Reconcile pinned цільового каналу за часом публікації (D-017); не надсилати наступну частину |
| Ack є, БД не записалася | Відновити confirmed row, не повторювати send |
| Частково доставлена серія, кеш збережено | Продовжити незмінний план із непідтвердженої частини |
| Кеш втрачено/прострочено після send | Hold та звірка; інша регенерована решта автоматично не надсилається |
| Несправний диск/state/checksum | Publication hold, відновлення; не обнуляти guards/budgets |
| Недоступне джерело/сайт, низький coverage | Публікується прочитане; `partial` і причина — лише в журналі/`/state` (D-019) |
| Немає новин за вікно | Нічого не публікується, результат `empty` (D-012) |

Операторське закриття partial серії не видаляє доставлені повідомлення й не
позначає unsent частини як published. Потрібен безконтентний аудит рішення.
Зміна destination username на інший peer блокує відправку до перевірки identity.
Команда операторського закриття та зовнішні alerts ще не реалізовані;
зараз результати перевіряються через `status` і технічні логи.

При деплої цього bugfix звірити ігнорований Git робочий `config/config.json`:
старий `max_output_tokens=8192` і substring-фільтри `реклама`/`розіграш` можуть
лишитися на сервері. Локально їх прибрано; код за замовчуванням не задає
output cap і не відсіює новини за такими словами. Новий промпт зберігається в Git.
Виправлення не перепубліковує старий випуск і не заповнює його пропуски автоматично.

## Backup і restore

Backup: `pg_dump` контентної БД + мінімальний state + несекретні config snapshots,
manifest/checksums; короткий lock без одночасних sends/state mutations.
Кеш не копіюється. Session backup — окремо, зашифровано, якщо потрібний.

Restore: publication hold → звірка власних destinations після часу backup →
відновлення confirmed частин → узгодження guards/schedule → budget reconciliation → resume.
Старий budget snapshot не означає нульових витрат після дати backup.
Прострочену price table перед платними викликами перевірити заново.

Сховище backup, RPO/RTO й аварійний канал поза Telegram заповнюються для конкретного
сервера. Docker healthcheck виявляє проблему; зовнішній watchdog/оператор має
визначену реакцію. FloodWait та idle не є приводом для restart loop.

## Мінімальний журнал експлуатації

Зберігати без новинних текстів: версію застосунку/config, deployment time,
backup/restore verification, model/price checked date, probe result, метрики
first/last part delay, coverage і числові usage. Невідомі значення позначати
unknown. Не створювати окремий raw export як побічний ефект діагностики.
