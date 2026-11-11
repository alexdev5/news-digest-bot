# Технологічний стек і готовність до реалізації

Дата: 2026-11-11. Основні технології задані в ТЗ v4; тут доповнено
інструментальну частину як технічні defaults. Docker image ще не створений.

Зафіксовано в `uv.lock` (2026-11-11, Python 3.13.18, uv 0.13.7): pydantic 2.14.3,
typer 0.29.1, tzdata 2026.6, sqlalchemy 2.1.8, psycopg 3.3.8, alembic; dev — pytest 9.2.0, pytest-asyncio 1.4.1.post1,
ruff 0.17.4, mypy 2.4.1. Telethon, openai та бібліотеки для сайтів ще не додані —
вони з'являться на відповідних етапах. На Windows `tzdata` обов'язковий для `zoneinfo`.

## Повний вибір для MVP

| Частина | Технологія | Призначення |
| --- | --- | --- |
| Сервер виконання | Linux, Docker Engine, Docker Compose v2 | Один сервіс/worker, окремі data/cache/state/session |
| Мова | Python 3.13 | Задана в початковому ТЗ версія |
| Асинхронний runtime | asyncio | Мережеві виклики, черги й планувальник без блокування event loop |
| Telegram | Telethon 1.x, MTProto | Вхід звичайним акаунтом, історія каналів, send і reconciliation |
| AI | Офіційний `openai` Python SDK, `AsyncOpenAI`, Responses API | `gpt-5.6-sol`, structured events/claims та короткий огляд |
| Схеми | Pydantic 2.x, JSON Schema | Config/state/cache/API-result validation, unknown fields заборонені |
| Конфігурація | Стандартний `json` (`object_pairs_hook` для заборони дублікатів) + Pydantic | JSON schema 4 з `target_channels` (D-021); у майбутньому — та сама модель із БД |
| Сайти: HTTP | `httpx` (async) | Завантаження RSS/сторінок/статей із таймаутами, лімітами розміру й контролем redirect |
| Сайти: RSS | `feedparser` | Читання RSS/Atom, дати публікації записів |
| Сайти: HTML | `selectolax` (CSS-селектори) + `trafilatura` (виділення основного тексту) | Сторінки-списки за `selectors` і очищення статей до тексту |
| Власні публікації | PostgreSQL 16 (`postgres:16-alpine`), SQLAlchemy 2, psycopg 3, Alembic | Confirmed-only таблиця, JSONB, транзакції, міграції; той самий стек, що в crypto-bot (D-022) |
| Тимчасові дані | Файли JSON/JSONL, стандартні pathlib/json/tempfile/os | Run snapshots, source texts, drafts, cursors, atomic state writes |
| Розклади | Власний невеликий модуль на asyncio + zoneinfo; tzdata в образі | Точні fixed windows, DST, revisions/cutover і незалежні профілі |
| CLI | Typer | Команди `digest`, typed options, help та коди завершення |
| Контроль worker | asyncio Unix domain socket | Локальна online-адміністрація без другого Telegram-клієнта |
| Логи | Стандартний logging + JSON formatter | Структуровані технічні записи без новинних текстів/секретів |
| Залежності/оточення | uv, `pyproject.toml`, `uv.lock` | Відтворюване оточення розробки та Docker build |
| Тести | pytest + pytest-asyncio | Unit, async integration, mock I/O, crash/recovery scenarios |
| Форматування/lint | Ruff | Єдині style/import/lint перевірки |
| Типи | mypy | Статична перевірка type hints і контрактів модулів |

Бібліотеки для сайтів додані 2026-11-11 через D-020 як **кандидати**: остаточний
вибір і версії фіксуються під час реалізації web_scanner після перевірки сумісності
з Python 3.13 і якості очищення на реальних сайтах власника (O-009). `feedparser` і
`trafilatura` — синхронні; виконуються поза event loop.

Синхронні файлові операції й запити до БД виконуються поза критичним event loop
(або через async engine SQLAlchemy з psycopg — рішення на етапі 5; на Windows async
psycopg потребує SelectorEventLoop, робоча ціль — Linux-контейнер).

Власний scheduler обраний через специфічні вікна/cutover і малу кількість профілів.
Він очікує час, а не сканує Telegram у фоні. Wall clock визначає планову межу,
monotonic clock — timeout; clock jumps і зміни timezone перевіряються тестами.

Локальна розробка можлива з Windows; робочий worker із Unix socket/locks
перевіряється в Linux-контейнері. Нативний Windows production worker не є метою MVP.

## Що ще потрібно технічно зробити

Це звичайні результати реалізації, а не питання, які власник має проєктувати
замість розробника:

1. Зафіксувати Python image digest (lockfile для поточних залежностей уже є).
2. Описати Pydantic/JSON schemas: config 4 (JSON, `target_channels`), AI stages,
   cache, state, part manifest, confirmed publication; завершити схему БД та CLI
   error/exit-code контракт.
3. Написати системні prompts із визначенням події, розвитку й суперечності,
   прикладами різних формулювань та обов'язковими source refs; промпти цільових
   каналів — доповнення до них. Налаштувати batching/token budget.
4. Реалізувати всі модулі й guards серії; перевірити Telethon SDK settings,
   raw send/random ID, timeout/retry/FloodWait та scheduled-only мережеву поведінку.
   Реалізувати web_scanner із SSRF-захистом.
5. Додати Dockerfile/Compose, локальні fixtures, CI-команди та fault-injection.
6. Виміряти latency, coverage, якість і витрати, завершити operations runbook.

Окремий вибір CI-провайдера залежить від майбутнього remote repository;
pipeline перевірок не повинен залежати від конкретного hosting vendor.

## Чого бракує від власника/середовища до live-запуску

| Потрібно | Навіщо |
| --- | --- |
| Цільові канали, їхні Telegram-джерела й сайти | Реальний конфіг; O-001, O-009 |
| Остаточні розклади | У ТЗ є можливості й шаблони, але приклади не є готовим production-config |
| Контрольований Telegram-акаунт, API credentials | Первинна авторизація; секрети вводяться локально |
| OpenAI key (є у власника), доступ до моделі | Реальні AI-виклики; доступ ключа до `gpt-5.6-sol` ще не перевірено |
| Budget caps на випуск/день/місяць | Контроль витрат; O-003 |
| Промпти цільових каналів | Стиль і тематика кожного каналу; O-010 |
| Невелика добірка прикладів з очікуваним результатом | Перевірка злиття, оновлень, джерел і стилю; O-005 |
| Параметри Linux-сервера, backup і аварійних сповіщень | Deployment, виміри й recovery цілі |

Для старту офлайн-розробки цього чекати не потрібно. Основний стек достатній,
процес продукту визначений; наступний результат — каркас і виконувані схеми.
Наявність документації сама собою не означає наявності готової реалізації.

## Офіційні джерела інструментів

- [OpenAI Python SDK](https://developers.openai.com/api/reference/python): async client.
- [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs):
  структуровані результати з Pydantic/JSON Schema; схема не замінює перевірку фактів.
- [uv](https://docs.astral.sh/uv/concepts/projects/sync/): locking/sync оточення.
- [Typer](https://typer.tiangolo.com/): CLI на основі Python type hints.
- [pytest](https://docs.pytest.org/en/stable/),
  [pytest-asyncio](https://pytest-asyncio.readthedocs.io/en/stable/): тестовий стек.
- [Ruff](https://docs.astral.sh/ruff/),
  [mypy](https://mypy.readthedocs.io/en/stable/): lint/format та перевірка типів.
- [HTTPX](https://www.python-httpx.org/), [feedparser](https://feedparser.readthedocs.io/),
  [selectolax](https://selectolax.readthedocs.io/), [trafilatura](https://trafilatura.readthedocs.io/):
  кандидати для web_scanner; документацію не звіряли, версії не перевірені.

Ці можливості звірено 2026-11-11. Version resolution і інтеграційні перевірки
для обраного Python/SDK будуть виконані при створенні реального оточення.
