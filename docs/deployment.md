# Розгортання на сервері

Дата: 2026-11-11. Сервер — Hostinger VPS, на якому вже працює crypto-bot (D-023).
**Стан:** конфігурація й скрипти готові, на сервері ще не виконувались. Worker
(`digest run`) ще не реалізований, тому зараз можна підняти базу, застосувати
міграції й перевірити конфіг; сам бот не запускається.

## Топологія

Окремий Docker-проєкт **`news-digest`** із двома контейнерами. У Docker Manager
Hostinger він з'явиться поруч із `crypto-bot` і `postgres`.

| Що | Де | Примітка |
| --- | --- | --- |
| Код | `/srv/news-digest` (git clone) | `docker-compose.prod.yml`, `name: news-digest` |
| `db` | контейнер `news-digest-db-1`, `postgres:16-alpine` | Свій том `news-digest_pgdata`, порт не публікується |
| `app` | контейнер бота, образ `local/newsdigest:prod` | Profile `worker` до етапу 2; хост БД — `db` |
| Пароль БД | `/srv/news-digest/secrets/postgres_password` | Генерує `deploy/init-server.sh`; не в `.env` |
| Налаштування | `/srv/news-digest/.env` | Лише `POSTGRES_DB`/`POSTGRES_USER`; шаблон `.env.prod.example` |
| Конфіг каналів | `/srv/news-digest/config/config.json` | Не в git; монтується read-only |
| Робочі дані | `/srv/news-digest/{state,cache,session}` | Власник UID 10001 |
| Бекапи | `/srv/news-digest-backups/news_digest_*.dump` | `deploy/backup.sh`, 14 днів |

Crypto-bot і його стек `postgres` не використовуються й не змінюються: спільні лише
сам сервер і Docker. Мережа, том, пароль і бекапи NewsDigest — свої.

Ресурси: при кількох записах на день PostgreSQL займає орієнтовно 50–100 MB RAM.
Вільну пам'ять сервера варто перевірити в панелі Hostinger перед запуском.

## Первинне налаштування (один раз)

1. **Доступ сервера до репозиторію.** GitHub deploy key унікальний для репозиторію:
   для `news-digest-bot` потрібен окремий ключ. Ключ crypto-bot не чіпати.
   ```bash
   ssh-keygen -t ed25519 -N "" -C "news-digest VPS" -f ~/.ssh/news_digest_deploy
   cat >> ~/.ssh/config <<'EOF'

   Host github-news-digest
       HostName github.com
       User git
       IdentityFile ~/.ssh/news_digest_deploy
       IdentitiesOnly yes
   EOF
   cat ~/.ssh/news_digest_deploy.pub
   ```
   Вміст `.pub` — у GitHub → Settings → Deploy keys → Add new, **без** «Allow write
   access» (сервер лише читає; push робить власник). Перевірка: `ssh -T git@github-news-digest`.
2. **Код** (через аліас `github-news-digest`, щоб узявся саме цей ключ):
   ```bash
   sudo mkdir -p /srv/news-digest && sudo chown "$USER":"$USER" /srv/news-digest
   git clone git@github-news-digest:alexdev5/news-digest-bot.git /srv/news-digest
   cd /srv/news-digest
   ```
3. **Каталоги, пароль БД, `.env`:**
   ```bash
   sudo bash deploy/init-server.sh
   ```
4. **Конфіг:** покласти робочий `config/config.json` і промпти в `config/prompts/`.
5. **Деплой:** `bash deploy/deploy.sh` — підніме `db`, створить схему й перевірить конфіг.

## Telegram (етап 2)

1. На https://my.telegram.org → API development tools отримати `api_id` і `api_hash`.
2. На сервері записати їх у файли (по одному значенню, без лапок):
   ```bash
   cd /srv/news-digest
   sudo sh -c 'printf %s 1234567 > secrets/telegram_api_id'
   sudo sh -c 'printf %s abcdef0123456789 > secrets/telegram_api_hash'
   ```
   (`init-server.sh` створює порожні заготовки з правами 0444.)
3. Вхід в акаунт — інтерактивно, worker має бути зупинений:
   ```bash
   docker compose -f docker-compose.prod.yml --profile worker stop app
   docker compose -f docker-compose.prod.yml --profile worker run --rm app digest login
   ```
   Номер телефону, код із Telegram і пароль 2FA вводяться лише в терміналі й
   ніде не зберігаються; сесія — у `session/`.
   Команда показує, куди Telegram надіслав код; порожній Enter запитує його знову.
   Якщо код не приходить — `digest login --qr`: QR сканується телефоном у
   Налаштування → Пристрої → Підключити пристрій (на час входу клієнт приймає
   оновлення лише для сигналу про скан).
4. Перевірка без читання історії джерел:
   ```bash
   docker compose -f docker-compose.prod.yml --profile worker run --rm app digest dialogs
   docker compose -f docker-compose.prod.yml --profile worker run --rm app digest doctor --config /config/config.json
   ```
5. `bash deploy/deploy.sh` (або наступний push) запустить worker. Стан:
   `… run --rm app digest status --config /config/config.json`.

Ручне сканування одного вікна (без публікації, друкує лише лічильники):
`… run --rm app digest scan --config /config/config.json --channel <id>` — теж
лише при зупиненому worker.

## OpenAI (етап 4)

Ключ — у `secrets/openai_api_key` (порожня заготовка створюється автоматично):

```bash
 printf '%s' 'sk-…' > /srv/news-digest/secrets/openai_api_key
chmod 0444 /srv/news-digest/secrets/openai_api_key
```

Пробіл на початку першої команди не дає ключу потрапити в історію bash.
Перевірка без платної генерації (worker можна не зупиняти):
`… exec app digest ai-check --config /config/config.json`. Після перезапуску
worker (`… restart app`) кожен run після сканування збирає план (`status` → `ready`).

## Автодеплой (GitHub Actions, як у crypto-bot)

Кожен push у `main` (або ручний запуск workflow на `main`) запускає
`.github/workflows/deploy.yml`:

1. **check** — ruff, mypy, pytest разом із тимчасовим PostgreSQL у GitHub Actions.
   Якщо перевірки впали, деплою немає.
2. **deploy** — SSH на сервер окремим ключем; передає лише SHA коміту.
   На сервері forced command `deploy/ssh-entrypoint.sh` перевіряє, що SHA = поточний
   `origin/main`, бере `deploy/deploy.sh` саме з цього коміту й запускає його.

`deploy.sh`: точний коміт (`git merge --ff-only <SHA>`) → build → `up -d --wait db` →
`deploy/backup.sh` → `digest migrate` → `digest validate-config`. Оновлення бота не
перезапускає `db`. Push у інші гілки не деплоїть.

### Налаштування автодеплою (один раз)

Ключ «GitHub Actions → сервер» — **інший**, ніж ключ «сервер → GitHub» з кроку 1.

1. На сервері (під `root`, бо репозиторій і git-ключ — у `root`):
   ```bash
   ssh-keygen -t ed25519 -N "" -C "news-digest GitHub Actions" -f ~/.ssh/news_digest_ci
   mkdir -p /root/bin
   install -m 0755 /srv/news-digest/deploy/ssh-entrypoint.sh /root/bin/news-digest-deploy-entrypoint
   printf 'command="/root/bin/news-digest-deploy-entrypoint",no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty %s\n' \
     "$(cat ~/.ssh/news_digest_ci.pub)" >> ~/.ssh/authorized_keys
   cat ~/.ssh/news_digest_ci        # приватна частина — лише в GitHub Secret
   ```
   Forced command означає: з цим ключем можна лише запустити деплой, не shell.
   Якщо в `sshd_config` вхід під root заборонено (`PermitRootLogin no`), потрібен
   `PermitRootLogin forced-commands-only` або окремий користувач у групі docker.
2. GitHub → репозиторій → Settings → Secrets and variables → Actions → New repository secret:

   | Secret | Значення |
   | --- | --- |
   | `DEPLOY_HOST` | IP або домен сервера |
   | `DEPLOY_USER` | `root` |
   | `DEPLOY_PORT` | `22` (або ваш SSH-порт) |
   | `DEPLOY_SSH_KEY` | увесь вміст `~/.ssh/news_digest_ci` (приватний ключ) |

3. Після збереження секрету приватний файл на сервері можна видалити:
   `rm ~/.ssh/news_digest_ci` (публічна частина вже в `authorized_keys`).
4. Якщо `deploy/ssh-entrypoint.sh` змінюється в репозиторії, встановлену копію треба
   оновити вручну тією ж командою `install` — автоматично вона не оновлюється (навмисно).

Поки сервер не налаштований (немає `.env`, `config/config.json` або пароля БД), job
**deploy** завершується помилкою без змін на сервері — це очікувано.

## Ручний деплой

```bash
cd /srv/news-digest
bash deploy/deploy.sh
```

Без `DEPLOY_SHA` береться поточний `origin/main`. Не запускати одночасно з автодеплоєм.

## Корисні команди на сервері

```bash
cd /srv/news-digest
docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml logs -f --tail=120 db
docker compose -f docker-compose.prod.yml exec db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
docker compose -f docker-compose.prod.yml --profile worker run --rm app digest schedule --config /config/config.json --all
bash deploy/backup.sh
```

Не виконувати `docker compose … down -v`: `-v` видаляє том із базою.

## Відновлення бази

```bash
cd /srv/news-digest
set -a; . ./.env; set +a
docker compose -f docker-compose.prod.yml exec -T db \
  pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists \
  --no-owner --no-privileges < /srv/news-digest-backups/<news_digest_…dump>
```

Перед відновленням зупинити бота (коли він з'явиться) і дотриматися правил restore
з [operations.md](operations.md): publication hold і звірка власних каналів.

## Що ще не зроблено

- Worker сканує, але ще не публікує: обробка AI й доставка — етапи 3–5.
- Щоденний backup за таймером (зараз — перед кожним деплоєм або вручну).
- Копія бекапів поза сервером.
