"""Build docs/how-it-works.html: every feature, what it does for you, and how it's built."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from sitekit import page  # noqa: E402

SECTIONS = []


def section(sid, title, what, how, files, decisions=""):
    SECTIONS.append((sid, title, f"""
<h2 id="{sid}">{title}</h2>
<p>{what}</p>
<h3>How it's built</h3>
{how}
<p class="status">Code: {", ".join(f'<a href="https://github.com/JosephsStuff/Flip-For-Free/blob/main/{f}"><code>{f}</code></a>' for f in files)}</p>
{f'<div class="note"><b>Why it works this way.</b> {decisions}</div>' if decisions else ''}"""))


section("architecture", "The big picture",
        "Flip for Free is one small program you run yourself: on your computer, in Docker, or on a free cloud server. "
        "It talks to eBay (and, if you connect them, CardTrader and Shopify) with your own keys, and keeps everything "
        "in one database file that you choose the location of.",
        """<figure><img src="architecture.svg" alt="Diagram: your browser, Claude and scripts talk to Flip for Free,
which keeps a SQLite database and talks to eBay, CardTrader, Shopify, the ECB and GitHub."><figcaption>One Python
process serves the pages, the JSON API and the background jobs; one SQLite file holds your data.</figcaption></figure>
<ul>
<li><b>Web app:</b> Flask with server-rendered pages (Jinja templates, one stylesheet, a little plain JavaScript).
No build step, so the code you download is the code that runs.</li>
<li><b>Data:</b> SQLite in the folder you pick on the setup screen, so it can live in Dropbox or OneDrive. Writes go
through one lock (<code>db.write_lock</code>) and commit quickly, so long jobs never block the pages.</li>
<li><b>Background work:</b> a sync thread (every few hours, or <i>Sync now</i>) and an automations thread (every 30
minutes) run inside the same process.</li>
<li><b>Explore the code:</b> the <a href="code-map.html">code map</a> is an interactive graph of every module, class
and function, built with graphify from the source.</li>
</ul>""",
        ["app.py", "db.py", "common.py"],
        "A single process and a single file keep it free to run anywhere, easy to back up (copy one file), and simple "
        "enough for one person to understand. SQLite comfortably handles years of a reseller's orders.")

section("profit", "True profit on every sale",
        "For each sale you see what the buyer paid, every penny eBay took (fees, promoted listings, postage labels, "
        "refunds, regulatory fees) and what's left after your own costs: the true profit.",
        """<ol>
<li><b>Orders</b> come from eBay's Fulfillment API: what the buyer paid for items and postage.</li>
<li><b>Money movements</b> come from eBay's Finances API: every charge and credit eBay made, each tagged with its
order. eBay requires these calls to be signed, so Flip creates a signing key for your account the first time.</li>
<li><code>profit.compute_sales</code> nets them per order: <i>net from eBay</i> = the sum of every money movement for
that order, and <i>eBay deductions</i> = what the buyer paid minus that net, broken down by type. Anything eBay
doesn't label is shown as "Other eBay adjustments" so the totals always add up.</li>
<li>If eBay's money data hasn't arrived yet (it lags a day or two), the fee is estimated and marked <i>est.</i> until
the real figure replaces it.</li>
<li>Your own costs come next: postage you paid elsewhere, packaging and handling (defaults from Settings, or set per
item or sale), and the item's cost. <i>Money made</i> = net from eBay − your costs; <i>profit</i> = money made − item cost.</li>
</ol>
<p>Account-level charges that aren't tied to an order (shop subscription, ad fees not linked to a sale) are kept
separate and, if you choose, taken off the dashboard's true profit.</p>""",
        ["profit.py", "sync.py", "ebay_api.py"],
        "eBay's payout reports don't line up with individual sales, so Flip rebuilds each order from the raw money "
        "movements. Reconciling to the penny (with an explicit 'other adjustments' line) means you can trust the total.")

section("sync", "Syncing with eBay",
        "Press <i>Sync now</i> or let it run on its schedule: orders, fees, your active listings and dispatch dates "
        "come in without you exporting anything.",
        """<ul>
<li><b>First sync</b> looks back up to two years (eBay's limit); later syncs only fetch what changed, using each
order's last-modified time.</li>
<li><b>Listings</b> come from the Trading API (<code>GetMyeBaySelling</code>), including watchers and quantities.
Relisted items are followed back to the original listing so <i>time to sell</i> counts from the first day.</li>
<li><b>Saving as it goes:</b> each order is committed as soon as it's processed, and listings are downloaded before
the database is locked, so other pages stay responsive during a long first sync.</li>
<li><b>Tokens:</b> you sign in with eBay's own page (OAuth). The refresh token is kept in your database and
access tokens are renewed automatically.</li>
</ul>""",
        ["sync.py", "ebay_api.py"])

section("inventory", "Inventory, bulk buys and the item timeline",
        "Log what you buy (one item or a whole lot), where it's stored and what it cost. Each item keeps a timeline: "
        "bought, listed, relisted, price changes, sold.",
        """<ul>
<li>Items link to eBay listings automatically by SKU, or by hand from the item page. Sales then pick up the item's
cost, storage location and bulk buy.</li>
<li><b>Bulk buys</b> spread the lot's total cost (plus extras like fuel) over its items: evenly, or in proportion to
their prices. An item with a fixed cost (the one valuable card in a binder) keeps it, and the rest is split over the
others. The bulk buy page shows money made back, profit and return on investment.</li>
<li>Every change is written to <code>item_events</code>, which is what the timeline shows.</li>
</ul>""",
        ["features.py", "profit.py", "db.py"])

section("time-to-sell", "Time to sell",
        "How long things take to sell: an average and median for single items, and \"one sells every N days\" for "
        "multi-quantity listings, with a sell-out estimate for the stock left.",
        """<p><code>timetosell.chains</code> groups listings across relists (by <code>relisted_from</code>, or the
inventory item), so the clock starts at the first listing. Single-quantity items use first listed → sold.
Multi-quantity listings use days listed ÷ units sold over the period, and project the stock left at that rate.
Cancelled and refunded sales don't count.</p>""",
        ["timetosell.py"])

section("automations", "Automations: relist and promote",
        "Optional rules that change your live eBay listings: end and relist stale listings (optionally dropping the "
        "price each time), and add promoted-listing ads only after something has gone unsold for a while.",
        """<ul>
<li>Runs every 30 minutes while the app is open, and only on one device at a time (the device name is recorded).</li>
<li><b>End &amp; relist</b> uses <code>EndFixedPriceItem</code>/<code>RelistFixedPriceItem</code> just before eBay's
30-day auto-renew, with a per-relist price cut, a floor, "never below what I paid" and a daily cap. You can ask for
relists to wait for your approval instead.</li>
<li><b>Delayed promotion</b> adds cost-per-sale ads through the Marketing API once an item has been listed N days.</li>
<li>Everything is paused until you press <i>Resume</i>; every action is logged and added to the item's timeline.</li>
</ul>""",
        ["automation.py", "ebay_api.py"],
        "Anything that changes live listings is off by default and fully logged, because a mistake costs real money.")

section("other-shops", "Other shops: Vinted, CardTrader and Shopify",
        "Sell the same stock in more places without selling anything twice.",
        """<h3>Vinted (and Depop, Facebook)</h3>
<p>These don't allow outside tools to post for normal sellers, so <b>Copy to Vinted</b> prepares everything for you
to paste in: the title, the eBay description with formatting removed plus item specifics, the closest Vinted
condition, a price that keeps your eBay take-home, and the photos as a zip. Mark the item as listed on Vinted and an
eBay sale raises a reminder to take it down there.</p>
<h3>CardTrader</h3>
<p>Connect with your CardTrader API token. On a card, Flip reads the set code in the title (LOB-124, SDY-006) to find
the expansion in CardTrader's catalogue and ranks its cards by name similarity. It then shows the cheapest current
listings for that card and CardTrader's own fields (condition, language, first edition) for the card type. Orders come
back as sales; prices in euros are converted with the European Central Bank's daily rate (<code>fx.py</code>).</p>
<h3>Shopify</h3>
<p>Connect an app from Shopify's Dev Dashboard. Flip uses the client credentials grant for a 24-hour token and renews
it automatically. <i>List on Shopify</i> builds a product from the eBay listing with <code>productSet</code> and publishes
it to the Online Store; Shopify orders come in as sales with your payment-fee settings applied.</p>
<h3>Stock in step</h3>
<p>When anything sells, on eBay, CardTrader, Shopify or a sale you record, its quantity comes off the item's listings
on the other shops: <code>EndFixedPriceItem</code> or <code>ReviseInventoryStatus</code> on eBay (read back and
checked), <code>increment</code> on CardTrader, and a compare-and-set <code>inventoryAdjustQuantities</code> on Shopify.
<code>stock_sync_done</code> makes sure each sale is applied once, and only to listings that existed when it sold.</p>""",
        ["crosslist.py", "stores.py", "cardtrader_api.py", "shopify_api.py", "fx.py"],
        "Only official APIs are used, and only where the marketplace allows them, so your accounts aren't put at risk.")

section("tax", "UK tax, expenses and mileage",
        "Self Assessment figures for the self-employment short pages (SA103S), on a cash or accrual basis, with checks "
        "for missing data before you file.",
        """<ul>
<li><code>taxcalc</code> maps sales, eBay fees, expenses and mileage to the SA103S boxes for the tax year that starts
on 6 April.</li>
<li>Mileage uses HMRC's approved rates per vehicle; post office trips can be logged automatically from your dispatch
days and saved drop-off points.</li>
<li>Mark a year as filed and any change that would alter it asks you to confirm first, in the app and the API.</li>
</ul>""",
        ["taxcalc.py", "dispatch.py", "features.py"],
        "It's a record-keeping helper, not tax advice: the figures are laid out so you (or your accountant) can check them.")

section("api", "API and Claude connector",
        "Everything in the app is also available as a JSON API, so Claude, a spreadsheet or a script can do the typing for you.",
        """<ul>
<li>Keys are random <code>flip_…</code> strings shown once; only a SHA-256 hash is stored.</li>
<li><code>mcp_server.py</code> is a Model Context Protocol server: each Claude tool is a thin call to the API, so both
always behave the same.</li>
<li>Full reference with real examples: <a href="api.html">API documentation</a>.</li>
</ul>""",
        ["api.py", "mcp_server.py"])

section("access", "Who can get in",
        "By default only the computer running it can open Flip for Free. Other devices and servers are opt-in.",
        """<ul>
<li><b>On your computer:</b> requests are only accepted from the same machine.</li>
<li><b>On your Wi-Fi:</b> with <code>--lan</code>, other devices can connect once they've been paired with a code
from Settings → Use on your phone, and they still sign in with eBay.</li>
<li><b>On a server:</b> with <code>--public-url</code> it only listens on the server itself, behind Tailscale (or
Cloudflare), which decides who can reach it; you sign in with eBay.</li>
<li><b>Docker:</b> traffic from Docker's own network counts as "this computer", because that's where your browser's
requests come from.</li>
</ul>""",
        ["app.py"])

section("reliability", "Errors, backups and updates",
        "When something goes wrong you get a plain explanation and a one-click way to report it; your data is backed "
        "up before every update.",
        """<ul>
<li><b>Errors:</b> <code>errors.py</code> keeps a rotating log and shows a friendly page with "Report this on GitHub".
Emails, tokens, keys, postcodes and your computer's user name are scrubbed from reports before you see them.</li>
<li><b>Updates:</b> Settings → Updates pulls the latest version from GitHub (git, or the zip if it isn't a git copy),
backs up the database first, then restarts itself with exit code 3, which the launch scripts, Docker and systemd all
treat as "start again".</li>
<li><b>Backups:</b> on a server a nightly job keeps 30 days of database copies.</li>
</ul>""",
        ["errors.py", "updater.py", "deploy/backup.sh"])

body = """<h1>How Flip for Free works</h1>
<p class="lede">What each feature does for you, and how it's built. Useful if you want to trust the numbers, change the
code, or run it somewhere new.</p>""" + "".join(s[2] for s in SECTIONS) + """
<h2 id="decisions">Design decisions</h2>
<table>
<tr><th>Decision</th><th>Why</th></tr>
<tr><td>Run it yourself, with your own eBay keys</td><td>No company holds your sales data or your eBay access, and there's nothing to pay for.</td></tr>
<tr><td>SQLite in a folder you choose</td><td>One file to back up or move; works offline; no database server.</td></tr>
<tr><td>Tailscale for servers</td><td>Private HTTPS from your own devices with no open ports and no domain to buy.</td></tr>
<tr><td>Copy-and-paste for Vinted, Depop and Facebook</td><td>Their terms don't allow outside tools to post for you, so nothing touches those accounts.</td></tr>
<tr><td>Official APIs only for CardTrader, Shopify and eBay</td><td>Changes are supported and auditable, and accounts aren't put at risk.</td></tr>
<tr><td>Server-rendered pages, no build step</td><td>Fast on old phones, simple to change, and the downloaded code is what runs.</td></tr>
</table>
<p>Want to change something? The <a href="code-map.html">code map</a> shows how the pieces connect, and the
<a href="https://github.com/JosephsStuff/Flip-For-Free/blob/main/CHANGELOG.md">changelog</a> explains every release.</p>
"""
toc = [(s[0], s[1]) for s in SECTIONS] + [("decisions", "Design decisions")]
(ROOT / "docs" / "how-it-works.html").write_text(page(
    file="how-it-works.html", title="How Flip for Free works: features and implementation",
    description="How Flip for Free works out true eBay profit, syncs orders and fees, tracks stock and bulk buys, "
                "lists on CardTrader and Shopify, keeps stock in step, and prepares UK tax figures.",
    body=body, toc=toc))
print("wrote docs/how-it-works.html")
