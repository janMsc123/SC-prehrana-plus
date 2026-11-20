"""Public pages for the Claude connector listing: privacy policy and setup guide.

Plain static HTML, no login, so the directory reviewers and users can read them.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from .config import settings

router = APIRouter()

CONTACT = "https://github.com/janMsc123/SC-prehrana-plus/issues"

_STYLE = """<style>
body{font:16px/1.55 system-ui,sans-serif;max-width:42rem;margin:6vh auto;padding:0 1rem;
background:#fafaf7;color:#1c1c1a}
@media(prefers-color-scheme:dark){body{background:#161614;color:#eee}a{color:#8cc}}
h1{font-size:1.6rem}h2{font-size:1.15rem;margin-top:2rem}code{background:#8882;padding:.1em .3em;border-radius:.2em}
</style>"""


def _page(title, body):
    return HTMLResponse(
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>{}</title>{}<body>{}</body></html>".format(title, _STYLE, body)
    )


@router.get("/privacy")
def privacy():
    return _page("Prehrana Plus privacy policy", """
<h1>Prehrana Plus: privacy policy</h1>
<p><i>Last updated 2026-10-13.</i> Prehrana Plus is a small, independently run tool that picks
school lunches in MALCOMAT (prehrana.sc-celje.si) for students of Šolski center Celje. It is not
run by the school or by Anthropic.</p>

<h2>What we collect</h2>
<ul>
<li><b>Your school account details</b>, as returned by MALCOMAT when you sign in: username, name,
class and meal location.</li>
<li><b>Your school password, only if you leave "remember me" on</b> (it is ticked by default in the app and on the Claude sign-in page, and you can untick it). It is stored encrypted
(Fernet, key held only on the server) so the app can place your orders when you are not online.
You can erase it at any time with "forget my password" in the app.</li>
<li><b>Your choices:</b> dish scores, rejected dishes, hand-picked meals per date, menu plan,
whether automatic ordering is on.</li>
<li><b>The school menu</b> and the orders MALCOMAT reports for you.</li>
<li><b>A short activity log</b> (sign-ins, ratings, picks, changes made through Claude).</li>
</ul>

<h2>How it is used</h2>
<p>Only to rank dishes, choose meals and order them for you through MALCOMAT, and to show you
your own data. Nothing is used for advertising or profiling, and nothing is sold.</p>

<h2>Using it from Claude</h2>
<p>When you connect Prehrana Plus to Claude you sign in on our page with your school account and
approve access. Claude receives a token, never your password. The tools it calls return your menus,
ranking and orders, and that text becomes part of your Claude conversation, which Anthropic handles
under its own privacy policy. Claude can only reach your account's data. Access tokens last one
hour and refresh tokens 60 days. You can disconnect in Claude's connector settings.</p>

<h2>Who else sees it</h2>
<p>Only the school's MALCOMAT system, which we talk to on your behalf. We do not share data with
anyone else. The person running the server can technically read the database; stored passwords are
encrypted, but whoever holds both the database and the server key could decrypt them. Suggestions
you submit in the app may be forwarded to the maintainer's chat.</p>

<h2>How long we keep it</h2>
<p>Until you ask us to delete it. "Forget my password" removes the stored password immediately.
To delete your whole account and history, contact us below.</p>

<h2>Security</h2>
<p>Everything is served over HTTPS. Sign-in uses OAuth 2.1 with PKCE, and connector tokens are
signed and expire.</p>

<h2>Contact</h2>
<p>Questions, deletion requests and security reports: <a href="{c}">{c}</a></p>
""".format(c=CONTACT))


@router.get("/claude")
def claude_guide():
    return _page("Prehrana Plus for Claude", """
<h1>Prehrana Plus for Claude</h1>
<p>Lets Claude see and manage <b>your own</b> school lunches (Šolski center Celje, MALCOMAT):
look at the menu, rate dishes, choose or skip a day, and turn automatic ordering on or off.</p>

<h2>You need</h2>
<ul><li>A paid Claude plan that supports connectors.</li>
<li>A school MALCOMAT account (the login you use at prehrana.sc-celje.si).</li></ul>

<h2>Set up</h2>
<ol>
<li>In Claude open Settings → Connectors → Add custom connector (or find Prehrana Plus in the
directory).</li>
<li>Use the URL <code>{url}/mcp</code> and add it.</li>
<li>Press Connect. On the page that opens, sign in with your school account and press Allow.</li>
</ol>
<p>Use the app at <a href="{url}">{url}</a> once first to rate dishes, or just ask Claude to
walk you through unrated dishes.</p>

<h2>Things to ask</h2>
<ul>
<li>"What's for lunch next week, and what will be ordered?"</li>
<li>"Which dishes haven't I rated? Let's go through five."</li>
<li>"Rate meal X 85." / "Never order that one."</li>
<li>"Pick MENI 3 on Thursday." / "Skip lunch on Friday." / "Back to automatic for Thursday."</li>
<li>"Turn automatic ordering off while I'm away."</li>
</ul>

<h2>Tools</h2>
<ul>
<li><code>get_status</code>, <code>list_days</code>, <code>get_ranking</code>,
<code>list_unrated</code>: read only.</li>
<li><code>rate_meal</code>, <code>reject_meal</code>, <code>set_pick</code>,
<code>clear_pick</code>, <code>set_autopilot</code>: change your settings. <code>set_pick</code> and
<code>clear_pick</code> place or cancel the real order at school straight away.</li>
</ul>

<h2>Limits</h2>
<p>Changes follow the app's rules: a day is locked once the school's cutoff passes (14:00 the day
before). There are no admin or password actions, and Claude only ever sees your account.</p>

<h2>Troubleshooting and support</h2>
<p>Wrong password: sign in with the same login as prehrana.sc-celje.si. "School system
unreachable": MALCOMAT is down, try later. Anything else: <a href="{c}">{c}</a>.
See also the <a href="/privacy">privacy policy</a>.</p>
""".format(url=settings.public_url, c=CONTACT))
