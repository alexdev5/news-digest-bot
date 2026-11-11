# Style guide: News Digest UA

Audience: one reader who wants to scan Ukrainian news (national + Dnipro region) in a
minute. Write the digest in Ukrainian. These rules are about style only; grouping,
facts and the JSON format follow the system instructions.

## Headline (`title`)
- 3–8 words, under 80 characters, no final period, no quotation marks around it.
- Say what happened and, if it matters, where: "Відновили світло у трьох громадах".
- Prefer an active verb and concrete nouns over abstractions ("Обстріляли Нікополь",
  not "Ситуація в Нікополі").
- No ALL CAPS, no "Терміново", "Увага", "Breaking" in the headline text.
- For a development of an already published story, start with "Оновлення:".

## Summary (`summary`)
- 1–2 short sentences, ideally under 200 characters.
- Do not repeat the headline; add the key detail: how many, when, who said it, what
  changes for people.
- A second sentence only if it adds a new fact (status, next step, official source).
- Numbers as digits, units as usual in Ukrainian media: "3 громади", "15:30", "120 т".
- Attribute claims to people or institutions: "за словами голови ОВА", "повідомили в
  ДСНС". Never mention Telegram channels or media as sources ("за даними «…»",
  "за твердженням «…»", "як пише …") — the source link is already at the end of the item.
- Avoid empty filler such as "стало відомо", "повідомляють", "як зазначається".
- Leave out the summary (empty string) if the headline already says everything.

## Direct quotes
- A direct quote of a named person may stay exactly as written, in its original
  language and wording, in «» quotes, with attribution: «…», — заявив Зеленський.
- Everything that is not a direct quote is in Ukrainian.

## Emoji (`emoji`)
- One emoji per item, matching the topic. 🔥 and ❗️ are allowed.
- Examples: ⚡ electricity, 💧 water, 🚨 alerts and attacks, 💥 explosions, 🚆 trains,
  🚌 city transport, 🏛 government and decisions, 💰 money and prices, 🏥 health,
  🎓 education, ⚽ sport, 🌦 weather, 🪖 front line, 🕯 losses and mourning.

## Tone
- Neutral and factual. No own assessments ("жахливий", "нарешті", "ганебно"), no calls
  to action, no ads, no jokes.
- Losses and tragedies: plain wording, no sensational details.

## Examples

Good:
- title: "Відновили електропостачання у трьох громадах", emoji: ⚡,
  summary: "Після ремонту підстанції світло повернули близько 4 тис. споживачів.
  На інших ділянках роботи тривають."
- title: "Оновлення: водогін на лівому березі", emoji: 💧,
  summary: "Ремонт магістралі завершили, подачу води відновлюють поступово."

Bad (and why):
- title: "ТЕРМІНОВО!!! Світло є!!!" — caps, exclamation marks, no facts.
- summary: "Стало відомо, що в місті сталася важлива подія." — filler, no details.
- summary repeating the headline word for word.
