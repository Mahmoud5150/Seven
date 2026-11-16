# WhatsApp templates to submit to Meta

Create both in **WhatsApp Manager > Message templates > Create template**. Names, text and
buttons must match exactly: the code sends these names and fills these variables in order
(`seven/alerts.py`). Meta asks for a sample value per variable; use the ones below.

## 1. `seven_daily_digest`

- **Category:** Utility (an update about a service the merchant subscribed to). If Meta re-files it as
  Marketing, each message costs more; reply that it is a requested account alert, or accept and re-price.
- **Language:** Arabic (`ar`)
- **Body:**

  ```
  ملخص منافسيك ليوم {{1}}. عدد التغييرات: {{2}}، وعدد المتاجر: {{3}}. الأبرز: {{4}}. لعرض كل التغييرات اضغط الزر بالأسفل.
  ```

  | Variable | What goes in | Sample for Meta |
  | --- | --- | --- |
  | {{1}} | the day | 17 نوفمبر |
  | {{2}} | number of changes | 6 |
  | {{3}} | number of competitor stores | 3 |
  | {{4}} | the top changes, one line | سوق القهوة: خصم على قهوة ضحى مزيج سمو: 36.80 بدل 48.99 ر.س (-25%) • بياك: نفد دبليو نزوري |

- **Button:** type *Visit website*, text `عرض التغييرات`, URL type **Dynamic**,
  URL `https://<your domain>/{{1}}`, sample `m/AbC123xyz`

## 2. `seven_big_move`

- **Category:** Utility · **Language:** Arabic (`ar`)
- **Body:**

  ```
  تنبيه سريع: {{1}} خفّض سعر {{2}} من {{3}} إلى {{4}} ر.س ({{5}}). التفاصيل من الزر بالأسفل.
  ```

  | Variable | What goes in | Sample for Meta |
  | --- | --- | --- |
  | {{1}} | competitor name | سوق القهوة |
  | {{2}} | product | قهوة ضحى مزيج سمو 250 جرام |
  | {{3}} | old price | 48.99 |
  | {{4}} | new price | 36.80 |
  | {{5}} | change | -25% |

- **Button:** type *Visit website*, text `عرض التفاصيل`, URL type **Dynamic**,
  URL `https://<your domain>/{{1}}`, sample `m/AbC123xyz`

## Rules the code already follows

- No newlines, tabs or 5+ spaces inside a variable (Meta rejects the send: error 131009).
- Utility body max 550 characters with variables filled in; the digest line is cut to fit.
- The body doesn't start or end with a variable, and no two variables touch.

Approval usually takes minutes to a day. Until both show **Active**, keep `WHATSAPP_TOKEN` empty:
the worker then writes every message to `outbox.jsonl` instead of sending.
