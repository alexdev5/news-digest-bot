# Робочий контекст проєкту

Спільна пам'ять для асистентів і агентів, які працюють із репозиторієм.
**Читай цей файл першим**, потім `AGENTS.md` і потрібні документи з `docs/`.
Після своєї задачі допиши розділ «Журнал» і онови «Поточний стан» / «Наступні кроки».

Правила ведення:

- Тільки стислі факти й посилання; повні формулювання рішень — у [decisions.md](decisions.md),
  контракт — у [specification.md](specification.md). Тут не дублювати їх цілком.
- Розрізняй **рішення власника** і **тлумачення/вибір асистента** (позначай явно).
- Дати — абсолютні (YYYY-MM-DD). Не записувати секрети, ключі, session, тексти новин.
- Застарілі пункти не видаляти мовчки: позначати «замінено …» з датою.

Останнє оновлення: **2026-11-11**.

## Що будуємо (одним абзацом)

Python-сервіс у Docker. Один звичайний Telegram-акаунт власника за розкладом читає
налаштовані Telegram-канали; сервіс також завантажує налаштовані сайти. Усе зібране
за вікно лягає в тимчасовий кеш, модель OpenAI `gpt-5.6-sol` (Responses API) групує
однакові за змістом новини й пише короткий український виклад, сервіс публікує
результат у власний Telegram-канал. Кожен власний канал — елемент `target_channels`
JSON-конфігу зі своїми джерелами, промптом і розкладом. Надалі конфіг може
переїхати в БД з адмінкою.

## Поточний стан

- Git: `main`, remote `origin` (GitHub). Push робить лише власник.
- Етап 1 частково готовий: `src/newsdigest` — JSON-конфіг (Pydantic), розклади й
  вікна, `digest_key`/`part_key`, PostgreSQL-схема частин (Alembic); CLI `validate-config`,
  `schedule`, `migrate`; 87 тестів без БД проходять, 21 тест PostgreSQL ще не запускався. Деталі — [implementation-plan.md](implementation-plan.md).
- Немає: Telegram, OpenAI, сайтів, `/state`/кешу, доставки, Docker.
- Кореневий `main.py` — шаблон PyCharm, не точка входу (не видаляли — файл власника).
- Python 3.13 встановлено через uv (системний Python — 3.14). Команди — `uv run …`;
  якщо `uv` нема в PATH, `python -m uv run …`.
- Документація в `docs/` оновлена під рішення D-001–D-021 (2026-11-11).
- Оригінал ТЗ `docs/specs/newsdigest-v4.txt` не змінювався й не змінюється.
- Розробка ведеться на Windows власника (OSPanel); production-ціль — Linux Docker.

## Ключові рішення власника (2026-11-11)

Повна таблиця — [decisions.md](decisions.md).

| ID | Коротко |
| --- | --- |
| D-001 | Усі унікальні новини, без top-N і відкидання «неважливих» |
| D-002 | Завеликий випуск → частини `1/N`, `2/N` (при ~20 новинах зазвичай одна) |
| D-009 | Однакові за змістом новини групує модель |
| D-010 | Публічні підписані канали можна читати, обробляти AI й публікувати собі; реєстру прав немає. Канал-ціль бачить лише власник |
| D-011 | AI тільки `gpt-5.6-sol`, ключ є; rule-based заміни немає — збій AI → без публікації + alert |
| D-012 | Немає новин → нічого не публікуємо; допис із уже опублікованим ID не беремо вдруге |
| D-013 | `scan_start_at` у конфігу: раніше нього не читаємо |
| D-014 | Видима лише назва джерела-посилання; маркер/хеш тільки в БД; у БД масив `source_message_ids` |
| D-015 | Посилання групи — на найраніший за часом допис; рівний час → вище в списку |
| D-016 | Хронологія як у Telegram: найновіші внизу; позиція групи — за її найновішим дописом |
| D-017 | Звірка невизначеної відправки — за часом публікації в цільовому каналі, без порівняння тексту |
| D-018 | Обсяг ~50 вхідних дописів/добу, ~20 унікальних |
| D-019 | Публікуємо прочитане; непрочитане пропускаємо без позначки в пості |
| D-020 | Сайти — додатковий тип джерела (RSS або сторінка + селектори) |
| D-021 | Конфіг у JSON; верхній ключ `target_channels` |

## Тлумачення й вибір асистента (власник може змінити)

- Внутрішні ключі джерел: `source_channels` і `source_sites` (власник спершу
  пропонував `channels`/`sites`; на пропозицію `source_*` прямо не відповів).
- «Лінка [ХДніпро]» прочитано як: видимий текст = назва джерела, що є посиланням;
  квадратні дужки в пості **не** виводяться.
- Нумерація пунктів наскрізна (default v4); власник не заперечував.
- Дедуплікація за ID (D-012) — у межах одного цільового каналу; в іншому каналі
  той самий допис можна використати.
- Стаття сайту без надійної дати публікації не включається.
- Промпт цільового каналу доповнює системні інструкції сервісу, а не замінює їх.
- Бібліотеки для сайтів (httpx, feedparser, selectolax, trafilatura) — кандидати,
  не перевірені.

## Відкриті питання

Повний список — розділ «Потрібно уточнити» в [decisions.md](decisions.md).
Головне: реальні джерела/сайти/цільові канали (O-001, O-009), розклади (O-002),
бюджет (O-003), приклади для злиття (O-005), промпти (O-010), сервер/backup (O-008).
Власник сказав: «все інше будемо по ходу вирішувати» — не блокувати роботу цими питаннями.

## Наступні кроки

1. Закрити хвости етапу 1 разом з етапом 2: atomic state storage, schemas `/state`
   і part manifest, schedule revisions, process/per-channel locks, кеш із TTL.
2. Далі — Telegram-адаптер (етап 2), сайти (2a), pipeline/attribution/renderer (3).

## Журнал

### 2026-11-11 — автодеплой як у crypto-bot

- Власник: автодеплой потрібен одразу, за зразком crypto-bot.
- `.github/workflows/deploy.yml`: push/dispatch у `main` → job `check` (ruff, mypy,
  pytest із сервісом PostgreSQL) → job `deploy` (SSH, у stdin лише SHA).
- `deploy/ssh-entrypoint.sh` — forced command, ставиться в `/root/bin/news-digest-deploy-entrypoint`;
  перевіряє SHA = origin/main, бере `deploy.sh` із цього коміту, `flock` від паралельних деплоїв.
- `deploy/deploy.sh` тепер деплоїть точний `DEPLOY_SHA` (або origin/main вручну).
- На сервері користувач — `root` (ключ «сервер → GitHub» створено в /root/.ssh,
  аліас `github-news-digest`). Секрети GitHub: DEPLOY_HOST/USER/PORT/SSH_KEY — ще не задані.
- **Не перевірено в дії:** workflow ще не запускався; тести PostgreSQL уперше пройдуть у CI.

### 2026-11-11 — production: окремий проєкт news-digest (db + app), D-023

- Власник: crypto-bot — **лише для читання** (і репозиторій, і його стеки на сервері).
  Тому спільна база в контейнері crypto-bot відхилена.
- Обрано: окремий Docker-проєкт `news-digest` (`name:` у compose) з двома контейнерами:
  `db` (postgres:16-alpine, том `pgdata`, порт не публікується) і `app` (profile
  `worker` до етапу 2). Пароль — compose secret `secrets/postgres_password`.
- Скрипти: `deploy/init-server.sh` (каталоги, пароль, .env; sudo), `deploy/deploy.sh`
  (pull → build → up db → backup → migrate → validate), `deploy/backup.sh`
  (`/srv/news-digest-backups`). `deploy/create-database.sh` видалено.
- **На сервері нічого не виконувалось**; перевірено `bash -n` і `docker compose config`.

### 2026-11-11 — production на VPS зі спільним PostgreSQL (замінено пунктом вище)

- На VPS (Hostinger, Docker Manager) є проєкти `crypto-bot` і `postgres`
  (контейнер `postgres-postgres-1`). У crypto-bot: стек `/srv/postgres`, мережа
  `ok_crypto`, хост `postgres`, суперкористувач із `/srv/postgres/.env`.
- Рішення: окрема база й роль `news_digest` у тому ж контейнері, без нового контейнера БД.
- Додано: `docker-compose.prod.yml` (runtime-образ, profile `worker` до етапу 2,
  secret `postgres_password`), `.env.prod.example`, `deploy/create-database.sh`,
  `deploy/deploy.sh`, `deploy/backup.sh`, `docs/deployment.md`.
- **На сервері нічого не виконувалось.** Перевірено лише `bash -n` і
  `docker compose -f docker-compose.prod.yml config`.
- Для сервера потрібен окремий GitHub deploy key для `news-digest-bot`.

### 2026-11-11 — PostgreSQL замість SQLite (D-022)

- Власник: БД — PostgreSQL, як у crypto-bot. Узято той самий стек: `postgres:16-alpine`,
  SQLAlchemy 2, psycopg 3 (`postgresql+psycopg`), Alembic.
- SQLite-модуль видалено. Нове: `settings.py` (змінні `POSTGRES_*`, `POSTGRES_PASSWORD_FILE`),
  `storage/models.py`, `storage/db.py`, міграції в `src/newsdigest/storage/migrations`
  (ревізія `0001`), `alembic.ini` для ручних `alembic revision`, команда `digest migrate`.
- Compose: сервіс `db` (порт хоста 5433, volume `postgres-data`), `app` залежить від нього.
  Обгортки: `start`, `stop`, `ps`, `logs`, `psql`, `migrate`. `.env.example` додано.
- Тести БД: `tests/integration`, тимчасова база на сесію; без PostgreSQL — skip,
  у Docker (`NEWSDIGEST_REQUIRE_DB=1`) — fail. **На реальній БД ще не запускалися.**
  Перевірено лише: `alembic upgrade head --sql` генерує очікуваний DDL.
- `/state` і `/cache` лишилися файлами за документацією; перенос у БД не обговорювався.
- **Перевірити після запуску Docker Desktop:** `.\run.cmd build`, `.\run.cmd check`.

### 2026-11-11 — обгортка команд і Docker для розробки

- Власник попросив запуск команд як у `C:\OSPanel\domains\crypto-bot`: `run.cmd` →
  `run.ps1` (Windows, `DOCKER_BUILDKIT=0`), дзеркальний `run.sh` (Linux/macOS).
- Додано `Dockerfile` (цілі `dev` і `runtime`, uv, Python 3.13), `docker-compose.yml`
  (сервіс `app`, одноразові `docker compose run --rm app …`, код змонтований),
  `.dockerignore`, `.gitattributes` (LF для sh/yml/Dockerfile).
- Команди: build, shell, test, lint, fmt, typecheck, check, validate, schedule, digest.
  `start/stop/logs/ps` додати разом із worker (етап 2).
- **Збірку образу не перевірено**: Docker Desktop не був запущений. Перевірено лише
  синтаксис скриптів, `docker compose config` і коди виходу `run.cmd`.
- Питання власника про БД: спершу була SQLite з ТЗ v4; власник обрав PostgreSQL (D-022).
- Під час коміту виставити `run.sh` виконуваним: `git update-index --chmod=+x run.sh`.

### 2026-11-11 — етап 1 (каркас)

- Власник створив репозиторій; закомічено оновлену документацію. У `.gitignore`
  додано `/secrets/`, `/session/`, `/cache/`, `/data/`, `/state/`, `*.session`,
  `/config/config.json`.
- Створено каркас і модулі етапу 1 (див. «Поточний стан»). Вибір асистента:
  `exclude_patterns` — підрядки, не regex; `misfire_grace_seconds` — у `defaults`
  і `schedule`; selectors сайту: `item` (обов'язковий), `title`, `date`, `content`;
  поля заголовка: `{name}`, `{date}`, `{time}`, `{period}`.

### 2026-11-11 — правила git

- Власник: комітити тільки за прямою командою; push робить лише власник; повідомлення
  комітів максимально короткі, як від людини, без згадок AI/ботів/агентів і без
  attribution-рядків. Записано в `AGENTS.md`, розділ «Git».

### 2026-11-11 — огляд документації та продуктові рішення

- Асистент прочитав ТЗ v4 і всі `docs/`; суттєвих суперечностей між документами не
  знайдено. Поставив питання про права, fallback, неповноту, статусні пости, маркер,
  порядок, обсяг і незавершені серії.
- Власник відповів (→ D-010…D-019), попросив сайти як джерела (D-020) і об'єктну
  структуру конфігу для кількох власних каналів (новини, крипта, можливо міжнародний).
- Уточнення: посилання — на найраніший допис; хронологія з найновішими внизу; звірка
  за часом; конфіг у JSON; ключ обрано `target_channels` (варіанти `digests`,
  `publish_channels`, `output_channels`, `my_channels`, `destinations` відхилено).
- Оновлено: `AGENTS.md`, `README.md`, `docs/README.md`, `decisions.md`,
  `specification.md` (переписано), `architecture.md`, `technology-stack.md`,
  `implementation-plan.md` (додано етап 2a «Сайти»), `testing.md` (M21–M29),
  `operations.md`, `review.md`. Створено цей файл.
