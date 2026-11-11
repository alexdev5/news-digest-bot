# План реалізації

Стан на 2026-11-11: етапи 1–4 (основне) виконані, **етап 5 — доставка без backup**
(див. «Результат етапу 5»). Етапи 2a, 6 не розпочаті. Базові вимоги — у
[specification.md](specification.md), прогалини — у [review.md](review.md).

## Bugfix після першої публікації (2026-11-11)

2026-11-12, D-036 (`group-v6`): редакторські правила більше не зупиняють випуск.
`ai/editorial.py` — нормалізація форми й змістові зауваження (числа з
еквівалентами, довжина, атрибуція каналу) з одним повтором; `ai/grouping.py` —
окремий запит на кожен невраховано кандидат, `ai_unprocessed`, перевірка
дублікатів через групи ID (`DEDUPE_INSTRUCTIONS`) і переписування лише
дубль-груп; `pipeline/render.py` — продовження завеликого пункту в наступній
частині; `plan.py` не дублює metadata продовження. Видалено `length_basis`,
carried-бази, `MERGE_INSTRUCTIONS`, ліміти 2400/240.

2026-11-12: після `ai_incomplete_coverage` додано AI trace тільки в тимчасовому
кеші, конкретне пояснення збою в ручних командах/`status --details` та `corrections`
з попередньою чернеткою на retry. Причини більше не губляться за загальним кодом.
Виправлено втрату бази довжини підподій у merge та хибне відхилення часу metadata.
Офлайн: 297 тестів, Ruff і mypy без помилок. Жива перевірка фінального merge на
кешованих даних: 56 пунктів / 5 частин, без публікації. Див. [формат і діагностику](editorial-format.md).

Додано D-034–D-035: `group-v5`, compact для коротких новин, списки підподій,
дослівна цитата без вступу, контроль сумарної довжини й повторне скорочення
проблемних пунктів. Під час merge зберігається початкова база порівняння.
Нові metadata сумісні зі старими планами, без міграції. Контракт —
[editorial-format.md](editorial-format.md); результати перевірок — [testing.md](testing.md).

Додано D-033: `run-window --from … --to … --ignore-published` — явний ручний
scan/AI/publish локально або через чергу worker на сервері. Окремі request/run IDs,
відновлення зі збереженим прапорцем, плановий coverage без прогалин.
Інтерфейс і перевірки — [manual-window.md](manual-window.md).

Гілка `bugfix/digest-completeness-and-recovery`: завершені інтервали для `run-now`
(D-031), усі міжнародні новини й достатній виклад (D-032), повні вхідні тексти,
злиття між AI-пачками, блокування неповної AI-відповіді. Виправлено спільний кеш
preview/доставки, повтори й ordering часткових серій, відновлення після restart,
помилки читання Telethon, heartbeat та лічильники причин виключення.
Докладно: [зміни та обмеження](bugfix-2026-11-11.md), [перевірки](testing.md).
Історичні результати етапів нижче описують стан на момент їх завершення.

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
- `config/config.example.json`, `config/prompts/news-ua.md`.
- Команди: `digest validate-config`, `digest schedule --channel <id> | --all`, `digest migrate`.
- Docker: `Dockerfile`, `docker-compose.yml` (db + app), обгортки `run.cmd`/`run.ps1`/`run.sh`.
- 108 тестів (87 без БД + 21 PostgreSQL) проходять у Docker (`.\run.cmd check`);
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

### Результат етапу 2 (2026-11-11)

Зроблено:

- `state/`: атомарні JSON-файли з версією й checksum (`runtime.json`,
  `rate_limits.json`, `heartbeat.json`), пошкодження → `StateCorruptError`, не reset;
  Pydantic `extra=forbid` як whitelist (без новинних текстів); `ProcessLock`.
- Ревізії розкладу в стані: зміна конфігу → перехідне вікно `[C,N)`, повторна зміна
  до C замінює pending (T06, T09); вимкнення/увімкнення без лавини (T10).
- `cache/`: `/cache/runs/<digest_key>/manifest.json` + `messages.jsonl`; сторінка
  пишеться до cursor (T16); TTL від створення (T28), ліміт розміру, cleanup без
  активних runs; прострочені runs → `cache_expired`.
- `telegram/scanner.py`: пагінація від end до start (T15), фільтр `[start,end)` (T13),
  виключення service/media_only (T21), поточна версія відредагованих після межі
  (D-024, замість T20), coverage
  complete/partial/unavailable/excluded (T22), продовження partial з cursor,
  закріплення peer і блок при зміні username (v4 §6).
- `telegram/rate_limit.py`: один запит одночасно, пауза ≥1 с, FloodWait ≤60 с —
  очікування, довший — `pause_until` у `/state` (T75).
- `telegram/telethon_gateway.py`: Telethon 1.45, `receive_updates=False`,
  `catch_up=False`, `flood_sleep_threshold=0`, підключення лише на час операції.
- `worker.py`: межі каналів, misfires із grace (T08, T14), відновлення перерваного
  run у grace, heartbeat, без читання джерел між межами (T11, T12). Run закінчується
  статусом `scanned`: обробка/публікація — етапи 3–5.
- CLI: `login`, `dialogs`, `doctor`, `scan`, `run`, `status`, `cache-status`,
  `cache-clean`, `healthcheck`; обгортки `.\run.cmd login|dialogs|doctor|scan|worker|status`.
- Docker: Telegram-секрети як compose secrets, healthcheck; `deploy.sh` запускає
  worker лише після login і заповнених секретів.
- 157 тестів проходять у Docker (`.\run.cmd check`), ruff і mypy без зауважень.

Не зроблено в етапі 2 (свідомо відкладено):

- Спільний scan batch для одного джерела в кількох каналах (оптимізація; при
  ~50 дописах на добу джерело читається окремо для кожного каналу).
- Локальний control socket: online-команди (dialogs/doctor/scan) потребують
  зупиненого worker (process lock), а `status` читає файли стану напряму.
- Delivery guards і part manifest — разом із доставкою (етап 5).
- Живу перевірку з реальним Telegram-акаунтом ще не виконано.

Початковий план етапу:

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

### Результат етапу 3 (2026-11-11)

Зроблено (`src/newsdigest/pipeline/`):

- `candidates.py`: альбом (`grouped_id`) → один матеріал; `exclude_patterns`;
  пропуск дописів, уже використаних у підтверджених частинах каналу (D-012, через
  `storage/published_repo.py`); злиття ТОЧНИХ копій (нормалізація: регістр, пробіли,
  без URL) зі збереженням усіх refs (T46); схожі тексти (Jaccard 3-shingles ≥ 0.85)
  лише позначаються підказкою `similar_to`, не видаляються (M20).
- `events.py`: модель `Event`; видиме посилання — найраніший допис групи, рівний час →
  позиція в списку (D-015, M21); порядок за найновішим дописом групи (D-016, M22).
- `render.py`: `N. виклад — Назва`, рівно назва — text-url entity (UTF-16, T45, M06);
  наскрізна нумерація; поділ між цілими пунктами, `· Частина i/N` у заголовку при N>1,
  N перераховується до стабільного (M03–M05, 9→10); завеликий пункт — `RenderError`, а
  не мовчазне відкидання; немає подій — немає частин (D-012).
- `plan.py`: незмінний `PublicationPlan` — `part_key`, payload hash, унікальний
  signed-64 `random_id` кожної частини, manifest hash, metadata пунктів і
  `source_message_ids` для БД; збереження в кеш run (`plan.json`).
- `build.py`: кеш run → кандидати → події → порядок → частини → план.
- CLI `digest preview --channel <id> [--window-end …] [--keep]`, обгортка
  `.\run.cmd preview`: сканує вікно й показує готові повідомлення без публікації.
- 180 тестів у Docker (`.\run.cmd check`).

Не зроблено / перенесено:

- Змістове групування й власний короткий виклад — етап 4 (модель). До нього preview
  використовує чорновий виклад (перші 1–2 речення допису), кожен допис — окремий
  пункт. Такий виклад **не публікується** (D-011): лише перевірка рендеру й посилань.
- Published-memory фактів (повтор події без розвитку, «Оновлення») — разом з AI (етап 4).
- `prepare`/`publish --run-id` і стан відправки — етап 5.

Початковий план етапу:

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

### Результат етапу 4 (2026-11-11)

Зроблено:

- Формат D-025 у `pipeline/render.py`: емодзі + жирний заголовок, суть, назва-посилання;
  entities `bold` і `text_url` (UTF-16). `Event` має `title` і `emoji`.
- `ai/prompts.py`: версіоновані системні інструкції англійською (`group-v2`) і strict JSON Schema:
  події (`candidate_ids`, emoji, title, summary, is_update) + `excluded` з причинами
  ad/off_topic/not_news/repeat_without_news. Промпт каналу — лише доповнення.
- D-030: промпт `group-v3` (без згадок каналів-джерел, перевірка в `review`), поле
  заголовка `{date_long}` («11 листопада»), default `format.header` = `{date_long}`.
- `ai/provider.py`: офіційний SDK `openai` (AsyncOpenAI, Responses API), `store=false`,
  `stream=false`, без tools, `reasoning.effort` і модель із конфігу; refusal/incomplete/
  невалідний JSON → `AiError`; коди помилок API (rate_limit vs insufficient_quota тощо).
- `ai/grouping.py`: пачки до ~60 тис. символів; перевірка відповіді (відомі id, кожен
  кандидат рівно раз, межі полів, **числа у викладі мають бути в текстах джерел**);
  до 3 запитів (1 + 2 повтори) із поясненням помилок; придатні події приймаються одразу,
  повторно надсилаються лише кандидати проблемних подій; числа джерела враховують частини
  дат/дробів (11.11 → 11); невраховані кандидати → `ai_unprocessed` (видно
  в лічильниках); жодної валідної відповіді → `AiError`, випуск не збирається (D-011).
- Міжвипускна пам'ять: опубліковані пункти за останні 24 години (`recent_items`, D-027).
- `digest_runner.py`: кеш → AI → план; worker після сканування збирає план
  (`plan.json` у кеші, run → `ready`, лічильники подій/частин/токенів). Без ключа —
  run лишається `scanned` з `ai_not_configured`.
- CLI: `digest preview` використовує AI, якщо задано ключ (`--no-ai` — чорновий);
  `digest ai-check` — доступ до моделі без платної генерації.
- Секрет `OPENAI_API_KEY` (локально `.env`, на сервері `secrets/openai_api_key`).
- 196 тестів у Docker, AI — fake-клієнт без мережі.

Не зроблено:

- Бюджети й ліміти токенів свідомо не реалізуються (D-026); `daily_budget_usd`
  ігнорується. Токени кожного run пишуться в `/state` (`ai_tokens`) лише для інформації.
- Міжпачкове злиття: при кількох пачках однакова подія з різних пачок може дати
  два пункти. При ~50 дописах на вікно пачка одна.
- Перевірка evidence-фрагментів (лише числа); корпус і метрики якості (O-005).
- Живий запит до моделі ще не виконувався.

Початковий план етапу:

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

### Результат етапу 5 (2026-11-11)

Зроблено:

- `state/store.py`: `delivery_guards.json` — безконтентні guards частин
  (prepared → sending → confirmed → committed; uncertain), manifest hash, payload hash,
  random_id, message_id, час. Unresolved guards не видаляються з кешем.
- `telegram/telethon_gateway.py`: низькорівневий `messages.sendMessage` зі збереженим
  random_id, entities bold/text_url, `no_webpage`; receipt (ID + час) з відповіді;
  однозначні відмови (немає прав, завеликий текст…) → `SendRejected`; власні пости
  каналу за інтервал — для звірки.
- `delivery.py`: `Publisher` — частини по черзі; до першого send фіксуються guards усієї
  серії; `sending` пишеться ДО мережевого виклику; ack → `confirmed` → insert у БД →
  `committed`; збій БД після ack — лише дозапис без нового send (M10, T69);
  обрив/timeout → `uncertain`, наступні частини не йдуть (M09, T64); звірка D-017:
  рівно один власний пост в [sending−1 хв, sending+5 хв), не зайнятий іншою частиною
  → `reconciled` (T63, T65), інакше hold (T66); FloodWait/відмова → `prepared`, той самий
  random_id при повторі; інший payload після старту → помилка (T67).
- `storage/published_repo.py`: ідемпотентний insert (`ON CONFLICT (part_key) DO NOTHING`),
  usage/coverage — лише в першій частині (`usage_scope=series`).
- Worker: після `ready` публікує хронологічно всі незавершені runs каналу; новіший
  випуск чекає (`held`), поки старіша серія не завершена (T71). Після повної доставки
  кеш run видаляється. Без БД send не робиться (`db_unavailable`).
- CLI `digest reconcile --channel <id>`, `.\run.cmd reconcile`; `status` показує
  невирішені частини.
- D-029: `digest run-now --channel <id>` (напряму або запитом до worker через
  `/state/requests/`), початок вікна — від уже покритого (`last_published_window_end`
  у БД + зібрані/публіковані runs у стані).
- 210 тестів у Docker.

Не зроблено:

- Backup/restore з reconciliation власних каналів після відновлення (є лише
  `deploy/backup.sh` і `pg_dump` перед деплоєм).
- Операторське закриття незавершеної серії з аудитом (поки — вручну в `/state`).
- Відновлення рядка БД, якщо план зник із кешу вже після send (`plan_missing_after_send`
  → hold).
- Живої публікації ще не було.

Початковий план етапу:

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
