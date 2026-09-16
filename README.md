# Prehrana Plus

Picks your school lunches for you.

Our school orders meals through [MALCOMAT](https://prehrana.sc-celje.si/). Every
school day offers seven choices, MALICA 1 through MALICA 7, and you pick one per
day, every week, forever. The menu runs on roughly a monthly cycle, so most
people end up choosing the same handful of favourites over and over.

This does it for you: you rate each dish once on a slider, it works out the
order, and then it orders the best available meal for you each week.

Built for Šolski center Celje, but the only school-specific parts are the base
URL and the seven-slot layout.

---

## How it works

**1. Collect.** Every morning it logs in, reads the published menu and stores
it. This phase writes nothing back to the school's system.

**2. Rate.** Each meal is shown to you once. Hit **✕** if you don't want it:
that meal drops below the daily fallback and is never asked about or ordered
again. Otherwise give it a score from **1 to 100** on a slider. That is the
whole questionnaire; there are no head-to-head questions.

Meals scored within a couple of points of a neighbour are **highlighted in
yellow** rather than queried, because at that distance either order is fine. If
one of them does matter, move it: the list is reorderable, and a meal you move
stays where you put it.

**3. Pick.** Every morning it looks at every day the school will accept an order
for, finds the highest-ranked dish on offer, and orders it. A meal you picked
by hand for a date on the dashboard beats the ranking and is left alone on
every later run.

It runs daily rather than weekly because the cutoffs do not all fall on the
same weekday, so Monday's order closes on **Saturday**. Since the school is asked
which dates are orderable *right now*, a daily run claims each date on the
first morning it becomes available and cannot miss a cutoff. Ordering the same
day twice is harmless: an order that is already correct is recognised and left
alone.

## The part that is actually hard

**A meal's identity is its description, not its slot number.**

MALICA 3 on Monday and MALICA 3 on Tuesday are two completely different meals.
The school's API confirms this outright: the `menu_id` field is *the same
value on every date*, so it identifies the position on the page, not the food.
The dish only exists as free text in `menu_description`.

This is not theoretical. In two weeks of real data:

```
BOMBETKA S SUHO SALAMO IN SIROM   appeared as MALICA 4, MALICA 5 and MALICA 6
PIŠČANČJI BURGER                  appeared as MALICA 1 and MALICA 7
```

So the same dish has to be recognised by its text when it comes round again.
That matching is imperfect, and getting it wrong is expensive and silent: one
dish split into two records splits its ranking in half and neither half is
right.

### Why it never guesses

The tempting approach, fuzzy string matching with a similarity threshold, is
actively dangerous here. These pairs all score 0.70 to 0.87 similar and are all
*completely different meals*:

| A | B |
|---|---|
| NJOKI + **OMAKA CARBONARA** | NJOKI + **OMAKA ARRABIATA** |
| TERIYAKI STICKI **TOFU** RIŽ | TERIYAKI STICKI **PIŠČANČJI** RIŽ |
| **ZELENJAVNA** LAZANJA | **MESNA** LAZANJA |
| CURRY S **ČIČERIKO** | CURRY S **PIŠČANCEM** |
| CHEESBURGER | HAMBURGER |

A threshold loose enough to catch a genuine typo would merge every one of them.

So [`app/identity.py`](app/identity.py) works differently. Each description is
split into components, allergen and meat annotations are dropped (the kitchen
re-types those without changing the dish), and near-universal filler
(`NAPITEK`, `KRUH`, `SADJE`, `JOGURT`, `MENI SOLATA`) is set aside as carrying
no signal. What remains is the meal's identity. Then:

- **identical** core components → the same dish, merged automatically;
- **similar but not identical** → stored as a *separate* dish **and** a question
  is queued for you at `/review`;
- **unlike anything known** → a new dish.

Nothing is ever merged for merely looking similar. A lookalike becomes its own
record and stays that way until you say otherwise, because splitting a dish is
recoverable: answer the question and the records merge, ranking and all,
while silently fusing two dishes corrupts your ranking with no sign anything is
wrong.

Measured on 93 real observations covering 75 distinct dishes: **zero false
questions**, while still catching typos (38/40), dropped components (31/31) and
allergen-text churn.

### Why the questions stay manageable

Comparing every possible pair would be N×(N−1)/2 questions. Two weeks of menus
already contain 75 distinct dishes, and a term will hold around 150, which is
11,175 comparisons, which nobody finishes.

Rating instead costs **exactly one screen per meal**, and every ✕ removes a meal
from the rest of the pass. On the real 75-dish dataset a full pass was 75
screens, of which 14 were rejections. The ranking is recomputed after every
single answer, so ordering works before you have finished.

New dishes each month cost one screen each. They slot into the list at the
position their score implies, badged **NEW** until you have placed or dismissed
them. There is a **Rate everything again** button for a clean slate; it keeps
your ✕ rejections, since those mean *never ask me again*.

### A caveat about the yellow highlight

`TIE_WINDOW` in [`app/ranking.py`](app/ranking.py) is the gap below which two
neighbours are highlighted. It is set to **2**, and on a realistic list that
marks nearly everything:

```
61 meals rated between 40 and 90  →  average gap between neighbours: 0.83 points

window   highlighted
  ±0        35  (57%)   meals you gave the identical score
  ±1        57  (93%)
  ±2        60  (98%)   ← current setting
```

This is arithmetic, not a bug: 61 meals cannot be spread across 100 points with
gaps wider than a couple of points. If the highlight is marking so much that it
stops meaning anything, set `TIE_WINDOW = 0`. Then only meals you scored
*identically* light up, which is the one case where your slider genuinely said
nothing. Spreading your scores over the full range also helps.

---

## Running it

Requires Docker. On your server:

```bash
git clone <your-fork> prehrana-plus
cd prehrana-plus
cp .env.example .env

# generate a key and paste it into .env as SECRET_KEY
python3 -c "import secrets; print(secrets.token_urlsafe(48))"

docker compose up -d
```

### First run, in order

The database starts empty, so the steps have to happen in this order:

1. **Open `http://your-server:8000` and log in with your school account.** The
   same username and password you use on the school's site. There is no
   separate account to create; the credentials are checked against MALCOMAT
   itself. Logging in is what lets the app read the menu as you.
2. **Click "Zberi jedilnik"** (collect the menu). This reads roughly
   three weeks of menu and takes a few seconds. Until it has run there are no
   dishes, so there is nothing to rate, and the dashboard says so.
3. **Rate the dishes.** One slider screen per dish, ✕ for anything you never
   want. On a first collection that is around 75 screens.
4. **Read the short explainer** it shows you afterwards, then check the order
   on the **Lestvica jedi** page and fix anything in the wrong place.

To move a meal there, tap its **⠿** handle: the meal is lifted, gaps open
between every row, and tapping one puts it there. Nothing is held down while
you do it, so the list still scrolls and the search box still works, which is
what makes a long move possible on a phone: lift the meal, search for where it
belongs, tap the gap. **Na vrh** and **Na dno** cover the extremes, typing a
score moves a meal to wherever that score belongs, and on a desktop the same
handle can simply be dragged. Anything still unrated sits in the right-hand
column and is rated by putting it in the list, where it takes the score its
new neighbours imply.

Everything else happens on the dashboard: the big switch, a countdown to the
next ordering run, and one card per menu slot for each upcoming day. Tapping a
card picks that meal by hand for that date. A hand-picked meal is **green** and
stays picked; the one the ranking reached on its own is **blue** and is
recomputed on every run.

After that it runs itself: it collects every morning and orders every morning.

### Configuration

All of it lives in `.env`:

| Variable | Default | Meaning |
|---|---|---|
| `SECRET_KEY` | *(required)* | Signs sessions and encrypts stored passwords. Changing it logs everyone out. |
| `MALCOMAT_BASE_URL` | `https://prehrana.sc-celje.si` | Your school's system. |
| `PLACE_ORDERS` | `true` | `false` makes the picker compute and log its choices without ordering anything. |
| `COLLECT_CRON_HOUR/MINUTE` | `6:30` | When the daily read runs. |
| `PICK_CRON_DAY_OF_WEEK/HOUR/MINUTE` | `* 7:00` | When the ordering run happens. `*` is daily; see above for why that matters. |
| `TZ` | `Europe/Ljubljana` | Server timezone for the schedule. |
| `SUGGESTIONS_WEBHOOK_URL` | *(empty)* | Chat webhook suggestions are forwarded to. **A credential; see below.** Empty means they are stored locally only. |

**If you are starting fresh, set `PLACE_ORDERS=false` and leave it there for a
few weeks.** You cannot rank dishes you have never seen, and until there is a
ranking the picker has nothing to act on. Once `/meals` shows an order you
agree with, flip it to `true`.

### Command line

Useful for a first run, or for driving the jobs from the host's cron instead of
the built-in scheduler:

```bash
docker compose exec prehrana-plus python -m app.cli collect        # read the menu (no writes)
docker compose exec prehrana-plus python -m app.cli pick --dry-run # decide without ordering
docker compose exec prehrana-plus python -m app.cli stats          # what has been collected
docker compose exec prehrana-plus python -m app.cli questions      # open identity questions
docker compose exec prehrana-plus python -m app.cli refingerprint  # recheck identities after a parser change
```

`stats` also prints which dishes have been seen on more than one day, which is
how you find out whether the menu really does repeat monthly.

---

## Running it for your classmates

The app is multi-user: everyone logs in with their own school account and gets
their own scores, rejections and order. The collected menu archive is shared,
which is the point: one instance watching the menu serves everybody.

### Suggestions

There is a **Predlogi** page where anyone logged in can send an idea. Every
suggestion is written to the local database first and then forwarded to a chat
webhook, so an unset, rotated or briefly broken webhook loses nothing. The
text is still in the `suggestion` table and `resend_failed()` will push it
through later.

Set it up with a Discord (or Slack) webhook:

```bash
# in .env
SUGGESTIONS_WEBHOOK_URL=https://discord.com/api/webhooks/...
```

> **The webhook URL is a credential.** Anyone who has it can post into your
> channel until you delete it. Keep it in `.env`, which is gitignored. Never
> commit it, never paste it into an issue, and if one leaks, delete that
> webhook in your server settings and create a new one. This matters more than
> usual here because the project is meant to be forked.

Two safeguards are built in, because the text comes from students:

- `@everyone` and `@here` are defused before sending (and `allowed_mentions` is
  set to none), so a suggestion cannot ping a whole server;
- one account may send 5 suggestions an hour.

Suggestions are **not anonymous**: the sender's username and class go with
them, and the page says so plainly.

### Turning the ordering off

A big switch on the dashboard controls whether the weekly job orders for you.
Turning it off stops the ordering only: the menu is still collected and your
scores are kept, and your stored password is left alone so the switch flips
back on without logging in again. **Forget my password** is the separate,
explicit action that erases the credentials.

---

## About your password

The weekly job has to log in as you while you are asleep, so your school
password is stored, encrypted with a key derived from `SECRET_KEY`. That is
reversible encryption rather than hashing. It has to be, because the plaintext
is needed to authenticate later. Honestly:

- anyone who can read **both** the database file and `SECRET_KEY` can recover
  the password, so keep `.env` out of git and off shared volumes;
- changing `SECRET_KEY` makes stored passwords unreadable and everyone has to
  log in again (the app handles this gracefully rather than crashing).

If you would rather not store it, use **Forget my password** on the dashboard.
It is erased and the weekly job stops acting for you; you can still use the
ranking and the day board. (The big on/off switch does *not* erase it, it
only pauses the ordering.)

The app talks only to your school's server. There is no telemetry and no third
party.

---

## Layout

```
app/
  identity.py    meal identity: parsing, fingerprints, the merge rule
  ranking.py     the 1-100 rating pass, the order, pinning and highlighting
  suggestions.py user suggestions and the outgoing chat webhook
  collector.py   Phase 1 -- read and archive the menu; queue identity questions
  picker.py      Phase 3 -- choose and order the week's meals
  malcomat.py    client for the school's API (one write endpoint, flag-guarded)
  auth.py        school-account login, sessions, credential encryption
  db.py          SQLite schema and helpers
  main.py        web app and the scheduler
  cli.py         command line entry points
tests/
  test_identity.py   identity rules, exercised against real menu text
  test_ranking.py    rating flow, ordering, pinning, and new-dish handling
  test_suggestions.py webhook delivery, failure handling, mention safety
```

Run the tests with `pytest`. `tests/fixtures/real_menus.json` holds 93 real
observations; the identity tests assert against actual dishes rather than
invented examples, because the failure being guarded against is a
domain failure, not a logic one.

## Adapting it to another school

MALCOMAT is used by several schools. If yours runs it, set `MALCOMAT_BASE_URL`
and it should work; the API is discoverable at `<base-url>/openapi.json`.

Two things are worth checking:

- **The filler list** in `app/identity.py`. It is frozen in code on purpose: if
  it were learned from the data it would drift as observations accumulated,
  silently changing old fingerprints and splitting dishes. If your school's
  menus use different boilerplate, edit `FILLER`, bump `FINGERPRINT_VERSION` and
  run `python -m app.cli refingerprint`, which reports what would collapse
  together instead of merging it for you.
- **The number of slots.** Nothing hard-codes seven; the picker uses whatever
  the API returns for each day.

## Licence

MIT, see [LICENSE](LICENSE). Contributions welcome.
