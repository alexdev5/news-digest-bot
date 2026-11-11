# План реалізації

Стан на 2026-11-11: **виконана підготовка документації**. Жоден етап застосунку
нижче ще не виконаний; `main.py` залишається шаблоном. Базові вимоги — у
[specification.md](specification.md), прогалини — у [review.md](review.md).

## 1. Каркас, схеми та офлайн-правила

Результат:

- Python 3.13 package, CLI entry point `digest`, `pyproject.toml` і lockfile;
  uv/uv.lock, Typer, pytest/pytest-asyncio, Ruff і mypy за
  [technology-stack.md](technology-stack.md). Налаштування й виконувані команди
  перевірок додаються разом із каркасом, не вважаються вже наявними.
- Pydantic-моделі config schema 4, strict JSON schemas, duplicate-key-safe YAML.
- `config/config.example.yml`, versioned state/manifest/confirmed-part schemas.
- Публічні enums errors/statuses, exit codes, schema compatibility policy.
- Чиста логіка schedule/windows/DST/cutover й config precedence.
- Порожня confirmed-only SQLite-схема з підтримкою N частин і nullable recovery metadata.

Приймання: offline validation не звертається до Telegram/OpenAI; тести вікон,
schema, `all_unique` та per-part uniqueness проходять на синтетичних даних.

## 2. Cache/state і Telegram-адаптер

Результат:

- Atomic state storage, process/per-profile locks, bounded cache TTL/size,
  reference counting shared scan batches, crash-safe cursors.
- Login, pinned account/peers, dialogs/doctor, права й credentials як secret files.
- History pagination лише для належного run; coverage; idle disconnect;
  єдиний bounded Telegram retry/FloodWait controller.
- Scheduler із terminal preview metadata та відновленням фіксованих вікон.
- Локальний control socket; online CLI не створює другий клієнт.

Приймання: немає фонового scan; alias не множить coverage; crash між записом
сторінки й cursor не губить дані; TTL не видаляє unresolved guard.

## 3. Події, attribution і серія повідомлень

Результат:

- Фільтри, точна/наближена дедуплікація, rule-based pipeline, published-memory.
- Ordered-source attribution, побудова message URLs і UTF-16 entities.
- Повний `PublicationPlan`, packing цілих пунктів, стабільні `i/N` і hashes.
- Окремі `preview` і `prepare`, облік кожної прийнятої події у всіх частинах.

Приймання: жодного top-N; усі прийняті події представлені рівно раз; довжина
кожної частини коректна разом із header/footer; порядок джерел детермінований.

## 4. OpenAI та оцінка якості

Результат:

- Async Responses provider для `gpt-5.6-sol`, версіоновані prompts/schemas,
  grounded claims, перевірка refs/evidence, відсутність tools.
- Batching і міжпачкове злиття без втрати подій; versioned temporary AI cache.
- Чинна тарифна таблиця з expiry, atomic reservations, budgets і unknown usage.
- Один retry controller поверх SDK; refusal/incomplete/fallback із явними statuses.
- Розмічений дозволений/синтетичний корпус і звіт метрик якості.

Приймання: офлайн mock-контракти проходять. Явний live `ai-check --probe`
проводиться окремо за наявності ключа/бюджету; не є частиною healthcheck.

## 5. Доставка, reconciliation і backup

Результат:

- Перевірений raw MTProto send зі збереженим random ID, per-part guards/receipts.
- Confirmed-only insert, resume непідтвердженої частини, hold для uncertain.
- Reconciliation за marker/hash/peer; restore частин із partial metadata.
- Cleanup після N commits, TTL із зупиненням активних читачів.
- Узгоджений backup без raw/cache/session; restore hold і budget recovery.

Приймання: fault injection на кожному кроці send/ack/commit та між частинами;
підтверджені повідомлення ніколи не надсилаються знову через звичайний retry.

## 6. Docker і стенд

Результат:

- Non-root/read-only Docker image, Compose secrets, окремі data/cache/state/session,
  локальний control socket, SIGTERM, healthcheck без зовнішніх викликів.
- CI для офлайн-перевірок; синтетичні fixtures; захист build context від secrets/cache.
- Навантажувальний звіт: 50 джерел, 5 профілів, до 10 000 постів/добу,
  одночасні й різні межі, first/last part delays, coverage і витрати.
- 7 днів тестового прогону в каналах власника, backup/restore rehearsal,
  заповнений operations runbook із фактичними командами й результатами.

Приймання: актуалізовані T01–T81 і нові M-тести; вирішені O-пункти, потрібні
для цього середовища; документований контрольований production запуск.

## Порядок робіт

Не додавати always-on collector як тимчасове production-рішення. Розробка
доставки має спиратися на модель частин із першого етапу, а не на стару UNIQUE
схему одного поста. Стару schema 2→3 реалізовувати лише за наявності реального
legacy-конфіга; у поточному workspace таких даних не виявлено.

Функціональні зміни супроводжувати відповідною документацією в `docs/`.
Для документаційної правки не створювати дзеркальні unit-тести тексту.
