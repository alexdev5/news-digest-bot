# Експлуатаційний контракт

Стан: проєктний runbook, 2026-11-11. Наведені CLI — **запланований інтерфейс**;
виконуваного `digest`, Dockerfile та Compose у workspace поки немає.
Реальні команди запуску доповнюються після відповідних етапів реалізації.

## Дані для середовища

Підготувати `config/config.json` (schema 4, `target_channels`): цільові канали,
їхні Telegram-джерела й сайти, `scan_start_at`, розклади, промпти, budgets і чинні
тарифи; account ID і pinned peer IDs. Окремий реєстр прав не потрібен (D-010).
Telegram API credentials і OpenAI key зберігаються у secret files, session — окремо.
Коди входу/2FA пароль не залишаються в конфігурації або документації.

Для OpenAI перевірити саме прив'язану API-організацію/проєкт, баланс і доступ
до `gpt-5.6-sol`. Pro-кредити потребують активації; поточний залишок не відомий
цьому проєкту. Не вважати денний бюджет автоматичним місячним обмеженням.

Production target: один Linux Docker host, один non-root worker, local storage.
Початкові 2 vCPU / 2 GB RAM / 10 GB SSD — оцінка v4, не перевірені вимоги.
Окремі writable data/cache/state/session, read-only config, tmpfs для socket/temp,
без зовнішнього admin port, privileged або Docker socket.

Конкретне розгортання на VPS (спільний PostgreSQL, скрипти `deploy/`) — у
[deployment.md](deployment.md).

## Перший запуск після реалізації

1. Локально `validate-config`; перевірити schema 4, clocks/timezone, permissions.
2. Із зупиненим worker: `login` під спільним process lock; перевірити account ID.
3. Запустити worker у preview mode; `doctor`, `dialogs`, `schedule --all`.
4. `ai-check`; `--probe` — окремий явний синтетичний виклик у межах бюджету.
5. Перевірити ручний/плановий `preview`: всі події, хронологію, посилання на найраніший допис, поділ, entities,
   відсутність insert/guards і cleanup. За потреби ручного publish використовувати `prepare`.
6. У тестовому destination перевірити серію, receipts, N confirmed rows і cache cleanup.
7. Виконати fault-injection/backup restore та стендовий прогін, заповнити звіт.
8. Застосувати production config; після цього планові випуски надсилаються
   автоматично, без ручного підтвердження кожної частини.

Під час усіх startup/doctor/healthcheck операцій джерельна історія не збирається.
Idle disconnect і очікування cutoff — нормальна робота, а не збій.

## Операції CLI

Зберігаються інтерфейси v4 §22: validate-config, login, dialogs, doctor,
schedule, schedule-diff, ai-check, status, preview, publish,
reconcile, cache-status, cache-clean, reload-config, backup, healthcheck.
Додається `prepare --channel ... --window-end ...`, що повертає ID збереженої серії.
`migrate-config` не потрібен (D-021). Параметр `--profile` у командах v4 читати як
`--channel <id цільового каналу>`; точні імена опцій фіксуються в CLI schema.

`status` має показувати digest window, N, committed parts, першу unresolved
частину, cutoff/TTL та блокування наступного випуску. `reconcile` перевіряє всі
unresolved parts профілю; точний фільтр part key/формат JSON та exit codes
визначаються CLI schema. Не додавати `--force`, що обходить guards.

Online-команди працюють через worker socket; login/offline-команди не відкривають
одну session паралельно. Звичайний `preview` не є підготовкою до `publish`.

## Несправності

| Симптом | Дія |
| --- | --- |
| `AUTH_REQUIRED` | Зупинити Telegram-операції; інтерактивно відновити сесію, без циклу кодів |
| FloodWait | Поважати durable `pause_until`; restart не скидає очікування |
| OpenAI quota/credits exhausted | Діагностика balance/limits; випуск не публікується, alert; без сліпого retry |
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
