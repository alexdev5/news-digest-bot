# План реалізації

Стан на 2026-11-11: документація підготовлена; **етап 1 частково виконаний**
(див. «Результат етапу 1»). Решта етапів не розпочата. Базові вимоги — у
[specification.md](specification.md), прогалини — у [review.md](review.md).

## 1. Каркас, схеми та офлайн-правила

### Результат етапу 1 (2026-11-11)

Зроблено:

- `pyproject.toml` (uv, hatchling), `uv.lock`, `.python-version` = 3.13; пакет
  `src/newsdigest`, CLI `digest` (Typer). Ruff, mypy `--strict`, pytest налаштовані.
- `config/models.py`: Pydantic-моделі JSON schema 4 (`defaults`, `target_channels`,
  `source_channels`, `source_sites`), `extra="forbid"`, валідація timezone, HH:MM,
  тригерів, URL сайтів, Telegram-ref, унікальності id, циклів «ціль = джерело».
- `config/loader.py`: JSON без дублікатів ключів і NaN, ліміт розміру, читання
  `prompt_file` лише всередині теки конфігу. `config/effective.py` — успадкування `defaults`.
- `telegram_refs.py`: нормалізація `@username`, `https://t.me/<username>`, `-100…`.
- `scheduling.py`: daily_slots і fixed_interval, DST (first_occurrence /
  shift_forward, злиття колізій), вікна `[start,end)`, `scan_start_at`, перша
  активація без backfill, misfires із grace, перехід розкладу `[C,N)`.
- `identity.py`: `digest_key`, `part_key`, канонічний UTC-формат.
- `storage/models.py` + Alembic-міграція `0001`: confirmed-only PostgreSQL (D-022),
  рядок на частину, `source_message_ids` JSONB з GIN-індексом (D-012), UNIQUE/CHECK
  для частин і JSON-полів. `settings.py` — підключення зі змінних `POSTGRES_*`.
- `config/config.example.json`, `config/prompts/dnipro_news.md`.
- Команди: `digest validate-config`, `digest schedule --channel <id> | --all`, `digest migrate`.
- Docker: `Dockerfile`, `docker-compose.yml` (db + app), обгортки `run.cmd`/`run.ps1`/`run.sh`.
- 108 тестів (87 без БД + 21 PostgreSQL) проходять у Docker (`.un.cmd check`);
  ruff і mypy без зауважень. `digest migrate` і production-образ перевірені локально.

Ще не зроблено в межах етапу 1: versioned schemas `/state` і part manifest,
публічні enums статусів/помилок поза CLI exit codes, state-модель schedule revisions
(є лише чиста функція переходу). Переносяться на етап 2 разом зі state storage.

Початковий план етапу:

Результат:

- Python 3.13 package, CLI entry point `digest`, `pyproject.toml` і lockfile;
  uv/uv.lock, Typer, pytest/pytest-asyncio, Ruff і mypy за
  [technology-stack.md](technology-stack.md). Налаштування й виконувані команди
  перевірок додаються разом із каркасом, не вважаються вже наявними.
- Pydantic-моделі JSON config schema 4 (`defaults`, `target_channels`,
  `source_channels`, `source_sites`), заборона невідомих полів і дублікатів ключів.
- `config/config.example.json`, versioned state/manifest/confirmed-part schemas.
- Публічні enums errors/statuses, exit codes, schema compatibility policy.
- Чиста логіка schedule/windows/DST/cutover, `scan_start_at` і успадкування `defaults`.
- Порожня confirmed-only схема PostgreSQL з підтримкою N частин, `source_message_ids`
  і nullable recovery metadata.

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

## 2a. Сайти як джерела (D-020)

Результат:

- web_scanner: RSS/Atom або сторінка-список + `selectors`, відбір статей за
  `[start,end)`, очищення HTML до тексту, запис у кеш як `SourceMessage`.
- SSRF-захист, ліміти розміру/часу/кількості, coverage сайту.
- Офлайн-тести на збережених синтетичних HTML/RSS fixtures.

Приймання: недозволені домени/IP/схеми блокуються; статті без дати не включаються;
між вікнами немає HTTP-запитів до сайтів.

## 3. Події, attribution і серія повідомлень

Результат:

- Фільтри, точна/наближена дедуплікація, пропуск уже опублікованих ID, published-memory.
- Attribution «найраніший допис групи» (D-015), message/article URLs і UTF-16 entities.
- Хронологічне впорядкування пунктів (D-016).
- Повний `PublicationPlan`, packing цілих пунктів, стабільні `i/N` і hashes.
- Окремі `preview` і `prepare`, облік кожної прийнятої події у всіх частинах.

Приймання: жодного top-N; усі прийняті події представлені рівно раз; довжина
кожної частини коректна разом із header; вибір посилання й порядок детерміновані;
порожній результат нічого не публікує.

## 4. OpenAI та оцінка якості

Результат:

- Async Responses provider для `gpt-5.6-sol`, версіоновані prompts/schemas,
  grounded claims, перевірка refs/evidence, відсутність tools.
- Batching і міжпачкове злиття без втрати подій; versioned temporary AI cache.
- Чинна тарифна таблиця з expiry, atomic reservations, budgets і unknown usage.
- Промпт цільового каналу поверх системних інструкцій; per-channel model override.
- Один retry controller поверх SDK; refusal/incomplete → випуск не публікується, alert (D-011).
- Розмічений дозволений/синтетичний корпус і звіт метрик якості.

Приймання: офлайн mock-контракти проходять. Явний live `ai-check --probe`
проводиться окремо за наявності ключа/бюджету; не є частиною healthcheck.

## 5. Доставка, reconciliation і backup

Результат:

- Перевірений raw MTProto send зі збереженим random ID, per-part guards/receipts.
- Confirmed-only insert, resume непідтвердженої частини, hold для uncertain.
- Reconciliation за часом публікації в pinned цільовому каналі (D-017); restore частин із partial metadata.
- Cleanup після N commits, TTL із зупиненням активних читачів.
- Узгоджений backup без raw/cache/session; restore hold і budget recovery.

Приймання: fault injection на кожному кроці send/ack/commit та між частинами;
підтверджені повідомлення ніколи не надсилаються знову через звичайний retry.

## 6. Docker і стенд

Результат:

- Non-root/read-only Docker image, Compose secrets, окремі data/cache/state/session,
  локальний control socket, SIGTERM, healthcheck без зовнішніх викликів.
- CI для офлайн-перевірок; синтетичні fixtures; захист build context від secrets/cache.
- Звіт вимірів на реальному обсязі (~50 вхідних дописів/добу на канал, D-018):
  затримка, coverage і витрати.
- 7 днів тестового прогону в каналах власника, backup/restore rehearsal,
  заповнений operations runbook із фактичними командами й результатами.

Приймання: актуалізовані T01–T81 і нові M-тести; вирішені O-пункти, потрібні
для цього середовища; документований контрольований production запуск.

## Порядок робіт

Не додавати always-on collector як тимчасове production-рішення. Розробка
доставки має спиратися на модель частин із першого етапу, а не на стару UNIQUE
схему одного поста. Міграції YAML schema 2→3→4 не реалізуються: legacy-конфігів
немає, конфіг одразу JSON schema 4 (D-021).

Після кожного етапу оновлювати цей документ і [context.md](context.md).

Функціональні зміни супроводжувати відповідною документацією в `docs/`.
Для документаційної правки не створювати дзеркальні unit-тести тексту.
