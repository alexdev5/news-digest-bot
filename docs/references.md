# Перевірені зовнішні контракти

Дата звірки: **2026-11-11**. Нижче тільки перевірені для цього огляду сторінки,
не твердження, що всі 29 джерел початкового ТЗ повторно перевірені.
Зовнішні правила/ціни/версії змінюються; це датовані спостереження.

## OpenAI Docs

- [GPT-5.6 Sol](https://developers.openai.com/api/docs/models/gpt-5.6-sol): точний ID
  `gpt-5.6-sol`, Responses, Structured Outputs, reasoning efforts, включно з medium.
  Документація на дату огляду має промоціни зі строком до 30.11.2026, контекстні
  пороги та окрему категорію cache writes. Ціни в коді не фіксувати; таблиця
  тарифів має перевірюваний строк чинності. API-доступ конкретного проєкту не перевірявся.
- [API credits for ChatGPT subscribers](https://developers.openai.com/api/docs/guides/chatgpt-api-credits):
  eligible Pro 100 може активувати $100 API-кредитів за період; перший grant
  пропорційний залишку циклу, unused credits не переносяться. Це не спільний
  ліміт чатів Pro. Баланс/активацію власника не перевіряли.
- [Data controls](https://developers.openai.com/api/docs/guides/your-data):
  `store=false` не скасовує застосовні abuse monitoring retention rules й не
  означає автоматичного Zero Data Retention. Локальний TTL — окремий контракт.

## Telegram і Telethon

- [API Terms](https://core.telegram.org/api/terms),
  [Content Licensing](https://telegram.org/tos/content-licensing): правила API
  пов'язані з умовами контенту/AI. Огляд не встановлює наявності дозволу
  для жодного реального каналу; потрібен заповнений source-rights registry.
- [Telethon TelegramClient](https://docs.telethon.dev/en/stable/modules/client.html):
  updates увімкнені за замовчуванням; `receive_updates`, `catch_up`, request retries
  та `flood_sleep_threshold` необхідно явно узгодити з scheduled-only архітектурою.
  Режим без updates обмежує handlers/conversations/QR login, тому потрібен тест
  вибраного login/receipt flow. Не видавати цю звірку за lockfile або тест Python-сумісності.
- [Telegram config](https://core.telegram.org/constructor/config):
  runtime `message_length_max` визначає довжину повідомлення в code points;
  робочі 3500 — проєктний запас, не значення Telegram API назавжди.
- [Message entities](https://core.telegram.org/api/entities): offsets/lengths
  рахуються в UTF-16 code units. Перевірка довжини повідомлення та entities — різні операції.

## Docker

- [Compose secrets](https://docs.docker.com/compose/how-tos/use-secrets/): доступ
  до secret надається конкретному сервісу й монтується як файл. Це не підстава
  називати host secret files автоматично зашифрованими.

## Перед live-впровадженням

Повторно звірити використовувані MTProto методи, точну встановлену Telethon/SDK
версію, тарифну таблицю, права, message limits, Linux filesystem durability та
фактичні доступи ключа. Інтеграційні перевірки задокументувати окремим звітом.
Не змінювати модель лише через наявність новішої в каталозі.
