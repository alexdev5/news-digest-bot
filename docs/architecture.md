# Архітектура й контракт доставки

Робоча технічна деталізація [поточного ТЗ](specification.md), 2026-11-11.
Основні Telegram/AI/delivery модулі реалізовані. Опис також містить заплановані
частини: сайти, повний backup/restore та операторські інструменти;
фактична готовність — у [context.md](context.md).

Термін «профіль» у цьому документі = **цільовий канал** (`target_channels[i]`),
`profile_id` = його `id`. Рішення D-010–D-021 від 2026-11-11 — у [decisions.md](decisions.md).

## Межі відповідальності

| Модуль | Вхід / результат | Заборонена відповідальність |
| --- | --- | --- |
| config / resolver | Валідований JSON-конфіг (schema 4), pinned peers, immutable snapshot | Прихована зміна peer, сортування джерел, секрети в конфігу |
| scheduler / windows | Планова межа + `scan_start_at` → `RunIdentity` та `[start,end)` | Читання джерел або генерація контенту |
| history_scanner | Вікно + `source_channels` → cache records і coverage | Insert у БД публікацій, фоновий ingestion |
| web_scanner | Вікно + `source_sites` → очищені статті в кеші й coverage | Доступ поза налаштованими доменами, фонове опитування, архів сторінок |
| cache | JSON/JSONL, manifests, cursors, TTL, shared refs | Постійний архів або backup raw |
| pipeline / AI | Snapshot + published memory → валідовані унікальні події | Вибір destination, вільний доступ до інструментів |
| attribution | Подія + учасники з часом публікації → найраніший допис (D-015) | Вибір за популярністю або перевагою LLM |
| renderer / splitter | Події → весь `PublicationPlan` із text/entities | Мережевий I/O або відкидання подій за довжиною серії |
| publisher | Незмінна частина → confirmed receipt / uncertain | Новий текст на retry або запис непідтвердженого як published |
| published_repository | `ConfirmedPart` → транзакційний insert | Raw, drafts, failed runs або pending outbox |
| service_state | Технічні JSON-файли під lock | Неопубліковані summaries, prompts або evidence |

Один worker володіє Telegram-сесією. `run-now` делегує запущеному worker запит
через `/state/requests/`; решта online CLI використовує process lock і потребує
вільної сесії. Unix socket поки не реалізовано. Telegram rate limits спільні
для каналів; token/cost budget reservations не застосовуються (D-026).

### Уточнення реалізації після bugfix

- Робочий кеш — `/cache/runs`, `scan`/`preview` — `/cache/inspection/runs`.
  Preview ніколи не перезаписує publication plan і random IDs робочої доставки.
- Idle-цикл пробує pending delivery до нових вікон. Backoff 60–900 секунд
  зберігається у `RunRecord`; він не запускає повторний scan або AI.
- Відмова send лишає `prepared` guard і `partial` run; після невизначеного send —
  звірка, після ack — тільки DB commit. Часткова серія блокує наступні.
  Unresolved guards блокують відправку навіть за втрати run record; незавершені
  runs не витісняються історією. Payload, manifest, N, random ID і destination незмінні.
- Втрата кешу після send → hold. Якщо всі guards і відповідні рядки БД вже
  committed, run завершується без повторного send навіть після cleanup/crash.
- Heartbeat кожні 30 секунд працює й під час scan/AI/send. `status --details`
  показує вікно в timezone каналу, per-source counts і виключені ID/причини без текстів.
- Повний вхідний текст ділиться на пачки/фрагменти. Після кількох пачок або
  повторів окремий запит повертає лише групи ID дублікатів; з оригіналів
  переписуються тільки ці групи, решта чернеток не змінюється (D-036).
  Необлікований після окремого повтору кандидат → `ai_unprocessed`, випуск виходить.
- `group-v6`: окремі compact/detailed, bullets з refs та дослівна quote з provenance.
  `ai/editorial.py` нормалізує форму сам; числа, довжина й атрибуція каналу — одна
  спроба моделі, далі `ai_warning_*` (D-036). Подробиці — [editorial-format.md](editorial-format.md).
  Renderer формує маркери/лапки, `items.short_summary` включає підподії для пам'яті
  AI, `quote_source_refs` містить лише ID. Міграції та зміни старих планів не потрібні.
- Retry отримує попередній невдалий пункт і всі його зауваження. AI trace
  (payload/response/перевірки) та деталізація збою зберігаються лише в `run/ai/`
  тимчасового кешу. CLI читає звідти пояснення; у `/state` лишається тільки код.

## Моделі й ідентичність

- `RunIdentity`: profile ID, UTC start/end, незмінний config/schedule snapshot.
- `SourceMessage`: тип джерела (`telegram`/`site`), source ID, peer/message або
  канонічний URL статті, час публікації, version refs; текст тільки в кеші.
- `ValidatedEvent`: власний короткий виклад, claims/evidence refs, attribution,
  is_update; до підтвердженої публікації теж тільки в кеші.
- `PublicationPlan`: впорядкований повний список частин і hash manifest.
- `OutgoingPart`: part index/count, text/entities, payload hash, random ID.
- `ConfirmedPart`: фактичний Telegram message ID/server time та metadata пунктів.

`digest_key` — hash канонічного набору `(profile_id, start_utc, end_utc)`.
Config revision не робить те саме вікно новим випуском. `part_key` —
`digest_key + part_index`, де index починається з 1. N та manifest hash
перевіряються для всіх частин; вони не створюють нову ідентичність для повтору.
Файлові шляхи будуються з безпечних hashes, не з channel title чи сирого timestamp.

D-033: явний `run-window` має ключ `manual` + hash(канал, межі, request ID).
Той самий запит після restart має той самий ключ; нова команда створює новий.
Плановий coverage не враховує ці runs/рядки БД, але їхні source IDs і власні
пункти лишаються в published history. Прапорець `ignore_published` і request ID
зберігаються в безконтентному `RunRecord` для відновлення. Черга запитів має
окремий файл на ID; worker видаляє його після виконання, а не перед ним.

Snapshot відкритого вікна закріплюється на його початку; на cutoff виконується
сканування вже визначеного контракту. При першій активації всередині вікна
використовується конфігурація активації до найближчої майбутньої межі.
Reload зберігає pending revision до cutover; негайні заборони прав/вимкнення
перевіряються поверх snapshot. Цей state не містить джерельного контенту.

Видимого marker/hashtag у тексті поста немає (D-014). Ідентичність частини —
`part_key`, payload hash і random ID у `/state` та БД. Звірка невизначеної відправки
(D-017): у pinned цільовому каналі шукати власні пости акаунта з датою в інтервалі
`[sending_at − δ, sending_at + Δ]` (δ — допуск годинника, Δ — таймаут send + запас;
конкретні значення визначаються в конфігу реалізації). Рівно один кандидат, ID якого
ще не зайнятий іншою частиною в БД/guards, → `confirmed` з `confirmation_method=reconciled`;
інакше `uncertain`. Текст не порівнюється. Сторонні пости (не від акаунта) не вважаються своїми.

## Робота з Telegram

History читається посторінково до start із додатковим локальним фільтром
`start <= message.date < end`. Source snapshot містить observed версії,
coverage і порядок. Cursor оновлюється тільки після надійного запису сторінки.

Відсутність handlers сама собою не вимикає надходження updates. Для scanner
перевірити Telethon `receive_updates=False`, `catch_up=False`, від'єднання в idle
та bounded retries/FloodWait. Login і отримання receipt потребують окремих
перевірок сумісності з цим режимом; не вмикати автоматичне збирання новин
заради reconciliation. Це технічний висновок із [контракту Telethon](https://docs.telethon.dev/en/stable/modules/client.html).

Фінальний send має використовувати збережений MTProto `random_id`.
Якщо convenience helper генерує новий ID, потрібен нижчий рівень API.
Правило не покладається лише на серверну дедуплікацію: є власні guards і звірка.

## Робота з сайтами

Для `source_sites` на межі вікна: RSS/Atom (`rss`) або сторінка-список (`url` +
`selectors`) → посилання на статті з датою в `[start,end)` → завантаження статей →
очищення HTML до тексту → запис у кеш як `SourceMessage` типу `site`. Статті без
надійної дати не включаються. Канонічний URL статті — її ID для дедуплікації
й `source_message_ids`.

Мережеві обмеження: тільки домени налаштованих сайтів; перевірка redirect і DNS;
блокування localhost, приватних/link-local IP, metadata endpoints, `file://` та
інших схем, крім `http/https`; ліміти розміру відповіді, кількості статей і часу.
Посилання з тексту статей не є довіреними адресами для завантаження. Між вікнами
сайти не опитуються. Coverage сайту: `complete`/`partial`/`unavailable`, як у Telegram.

## Побудова серії

Семантичне групування виконує AI: повідомлення зі спільною подією можуть мати
різні формулювання, а схожі слова — описувати різні події. Exact hash зберігає
provenance копій; near-text score лише формує кандидатів на порівняння.
Не видаляти додаткові факти до аналізу моделлю. При міжпачковому group зберігати
claims і source refs, щоб нові деталі не зникали за схожим коротким заголовком.

1. Завершити scan (Telegram і сайти) і зафіксувати snapshot; записати coverage.
   Відкинути записи, ID яких уже є в `source_message_ids` цього цільового каналу (D-012).
   Нуль придатних записів → результат `empty`, публікації немає.
2. Обробити всі придатні записи пачками; виконати міжпачкове group/deduplicate.
3. Відокремити повтори published facts, не виключаючи розвиток події.
4. Для кожної події обрати visible source (найраніший допис групи, D-015), сформувати пункт.
5. Впорядкувати події за зростанням часу найновішого допису групи (D-016);
   рівний час — стабільний tie-break за ID події.
6. Упакувати цілі пункти в частини, перерахувати N і заголовки до стабільного результату.
7. Перевірити, що кожна прийнята подія є рівно в одній частині; сформувати
   `PublicationPlan`, hashes і всі random IDs. Надійно записати план у `/cache`.
8. До будь-якого send зафіксувати у `/state` безконтентний manifest серії:
   key, pinned destination, N, manifest hash, усі part keys/hashes/random IDs.

Жодної генерації «наступної частини» після відправки першої. Інакше після
restart можуть змінитися кількість частин, нумерація та межі вже надісланого.
Внутрішні event IDs стійкі в межах snapshot; покриття перевіряється до очищення кешу.

## Контракт контентної БД

Предметна таблиця PostgreSQL (D-022) — `published_messages`, тільки підтверджені частини.
Схема: `src/newsdigest/storage/models.py` і міграції Alembic; JSON-поля — `JSONB`, час — `timestamptz`.
Успішні перші частини не очікують доставки всієї серії для insert.

Збережені поля v4: publication/profile/destination IDs, window/scheduled times,
published/confirmed times, confirmation method, text/entities, payload hash/random ID,
config/schedule/source-order snapshot, items, coverage/usage та recovery completeness.

Доповнення: `part_key`, `part_index`, `part_count`, `series_manifest_hash`,
`source_message_ids` (D-014) — JSON-масив ID джерельних записів, використаних у
пунктах частини: `{"type":"telegram","peer_id":…,"message_id":…}` або
`{"type":"site","url":"…"}`. Поле `marker` v4 не використовується.
Обмеження нової схеми:

```text
UNIQUE(part_key)
UNIQUE(digest_key, part_index)
UNIQUE(destination_peer_id, destination_message_id)
CHECK(1 <= part_index AND part_index <= part_count)
```

`digest_key` і `(profile_id, start, end)` **більше не UNIQUE окремо**.
Repository транзакційно перевіряє однакові profile/window/destination/N/manifest
у всіх частин digest; одного CHECK для цього недостатньо.

`items` (JSONB) містить тільки фактично видимі пункти цієї частини. Evidence-цитат,
raw, відкинутих кандидатів і майбутніх частин у рядку немає. Частина використаних
source refs може зберігатися як metadata саме опублікованих пунктів.

Run-level usage у частинах не дублюється як N незалежних сум. Повний aggregate
записується один раз у першу підтверджену частину з `usage_scope=series`;
інші мають посилання на той самий digest або nullable usage. За partial recovery
недоступні usage/provenance позначаються unknown, не нулем.

Після recovery без `items` цей запис не можна вважати повною змістовою
пам'яттю. До автоматичних наступних випусків оцінити відновлення пунктів із
власного тексту або зафіксувати degraded memory; не відновлювати raw-архів.

## Стани та доставка частин

Технічні стани run у `/state`: `materialized`, `scanning`, `processing`,
`ready`, `publishing`, `partial`, `complete`, `empty`, `failed`, `skipped`,
`cache_expired`, `needs_review`. `empty` — немає придатних новин, нічого не надіслано (D-012).
Неповний scan не блокує публікацію прочитаного (D-019); coverage пишеться в `/state`. Preview має окремий terminal result.
Не зберігати їх як непідтверджені рядки контентної БД.

Guard частини: `prepared → sending → confirmed → committed`.
Невідома відповідь після send: `sending → uncertain`; далі тільки reconciliation
до `confirmed` або явного технічного розв'язання. `prepared` ще не означає send.

Для частини i:

1. Перевірити БД та guard; підтверджена/committed частина повторно не надсилається.
2. Перевірити незмінний payload, права, pinned destination і допустимість часу.
3. Надійно записати `sending` до мережевого виклику.
4. Виконати send; receipt із конкретним message ID записати до commit БД.
5. Insert підтвердженої частини, потім `committed` у state.
6. Тільки після цього перейти до i+1. Після N — `complete` і cleanup кешу.

Пункти committed частин відразу доступні published-memory. Новіший випуск того
самого профілю очікує завершення або явного закриття старої partial серії.
Інші профілі продовжують роботу, якщо немає спільної проблеми акаунта/диска.

## Recovery й очищення

| Ситуація | Наступна дія |
| --- | --- |
| Немає send-спроб, кеш втрачений | Дозволений rescan того самого вікна в межах grace |
| Частини 1…k committed, решта payload збережена | Продовжити з першої непідтвердженої, без регенерації |
| Частина k uncertain | Звірити саме її; не надсилати k+1 або новіший випуск |
| Ack є, DB commit не вдався | Відновити confirmed insert за receipt/reconciliation |
| Після початку серії кеш втрачено/TTL минув | Hold; звірити відправлене, не генерувати іншу решту автоматично |
| Джерело вимкнене під час серії | Уже підготовлений план не переформатовується; нові вікна без цього джерела |
| Відновлено старий backup | Hold; звірити destinations, guards, БД та budget state до нових sends |

Telegram-серія не є атомарною транзакцією. Автоматичний rollback видаленням
успішних частин не робиться. Остаточне закриття незавершеної серії — окрема
операторська дія зі збереженням аудиту, без `publish --force`.

На TTL спочатку зупиняються активні читачі. Payload видаляється, unresolved
guards і безконтентний manifest залишаються. Успішний shared cache очищається
після звільнення всіх поточних споживачів, не до майбутнього слоту.

## Стійкість службового стану

Один writer; process lock; per-profile lock; versioned JSON із checksum.
Запис: temporary file → flush/fsync → atomic replace → directory sync, де підтримується.
Операції між кількома state-файлами не вважати атомарними: записати phase/revision,
визначити порядок commit і перевірити кожну точку падіння. State corruption
вмикає hold, а не скидання розкладу, guards або витрат.

Будь-який cache/state parser має whitelist полів. Шлях `/state` не використовується
для прихованих неопублікованих текстів. Backup вміщує узгоджені confirmed БД,
state та несекретну конфігурацію; кеш і звичайна копія session виключені.
