# Flip for Free

A local web app that signs in with your eBay account, pulls in everything you've sold, takes off every eBay deduction, and shows what you actually made. It also tracks profit per item and per bulk buy.

## What it does
**Sync with eBay** (sign in with your own eBay developer keys; nothing goes through anyone else)
- Pulls in sold orders, active listings and every fee, refund, postage label and ad charge. The first sync goes back 2 years.
- New sales, relists, returns and cancellations update automatically (every 6 hours while the app is open, or press **Sync now**).
- You only enter what eBay doesn't know: what you paid, where you bought it and where it's stored.

**Automations** (run on their own every 30 minutes while the app is open; pause any time)
- **End & relist**: ends fixed-price Good 'Til Cancelled listings and relists them with a new item ID and fresh start date. Choose to do it just before eBay's 30-day auto-renew or after a listing is N days old. Set the hour it runs, a daily limit, and whether you approve each batch first. Exclude items, or give them their own floor price, from the item page. A daily digest says what relisted, what failed and why.
- **Repricing**: cuts the price by a % on each relist for up to N cycles, never more than a maximum total drop, never below a minimum price and never below what you paid.
- **Delayed promotion**: adds a Promoted Listings (cost per sale) ad only after an item has gone unsold for N days.
- **Error detection**: flags duplicate postage label charges, label refunds and adjustments, fee refunds, failed relists, cancellations and returns.
- **Sale alerts**: a notification for each sale showing where the item is stored, with optional desktop alerts.
- Every action is written to an activity log and to the item's timeline. There's a preview of what's coming up in the next 7 days.

**Packing costs**
- **Packaging** and **Handling (labour)** come off every posted sale's money made.
- Set defaults in **Settings → Packing costs** (with a minutes × hourly rate helper), a different amount for any listing on its inventory page, or override a single sale. Bulk-set them from the Inventory or Sales lists.
- Defaults only apply to sales from a date you choose, so your history doesn't change unless you want it to.
- If you pack yourself, labour is shown so you can see what each sale would make with paid help, but it's left out of your tax figures (your own time isn't an allowable expense). Tick "I pay someone to pack" and it goes in SA103S box 13. Packaging always counts (box 18).

**Date ranges**
- Every page has quick periods (today, this week, month, financial year…) plus **Custom**. Pick the dates, give the range a name (e.g. "Christmas rush") and click **Save range**. It then appears as a ★ button on every page and in the P&L period list. Exports use its name in the file name and heading. Remove a saved range from the Custom panel.

**Inventory**
- Every item you've bought: cost, where you bought it, where it's stored, and whether it's listed, not listed or sold.
- Log stock as soon as you buy it and link it to its eBay listing later. Listings found during sync are added automatically.
- **Bulk buys**: enter a lot's total once and its cost is split across its items, either evenly or weighted by each item's price. Give a valuable item its own fixed cost and the rest is re-split. Each bulk buy shows money made back, profit, ROI, break-even and the value still listed.
- **Product timeline**: price changes, title changes, relists (across new item IDs), promotions and sales.
- **Restock links**: save where you buy each item (supplier page, wholesaler, Amazon…). A ↻ restock button opens it, and "Add restock to inventory" records the new batch with its own cost and date.
- **Link listings to a bulk buy** from the bulk buy page: tick existing eBay listings (active, ended or sold) and their past sales, future sales and relists all count towards that lot.
- Import inventory or past sales from a CSV (Excel, Google Sheets or another reseller tool).

**Insights**
- Net profit, margin, ROI, average days from buying to listing and from listing to selling.
- Weekly charts of new listings vs items sold, and of sales, profit and margin.
- Cost breakdown: inventory bought, cost of goods sold, fees, ads, postage, packaging and refunds.
- **Sources**: which places you buy from actually make money (spend, sell-through, profit, ROI, days to sell).
- **Buyer map**: where your buyers are. Only the postcode district is stored (e.g. "SW1A"), never the full address.
- **Other marketplaces**: record Vinted, Facebook, Depop, car boot or cash sales alongside eBay.
- Sold price history for everything you've sold.

**Performance**
- **Store health**: rolling 90-day sell-through rate, average sale price, days to sell and stale listings, each green or amber against targets you set.
- **Stale stock**: listings and unlisted items older than your stale threshold, and the cost tied up in them.
- **Growth Lab**: which price bands sell best (margin, profit per item, days to sell) and how healthy your margins are.
- **Goal planner**: turns a monthly or yearly profit, revenue or items-sold goal into daily and weekly targets, with progress rings for today, this week and this month. A recap arrives in your notifications on the 1st of each month.
- **Calculators**: "Should I buy it?" profit calculator (pre-filled with your own fee rates), break-even buy price, and sell-through rate.

**Accounts**
- **Profit & loss statement** for a financial year, quarter, month or any dates: revenue, cost of goods sold, each eBay fee, postage and packaging, promoted listings, expenses, mileage and write-offs, then net profit. Unsold stock is shown separately as an asset. Download as CSV, print to PDF, or email it to your accountant. Set your own financial year start in Settings.
- **Write-offs**: mark an item lost, damaged or donated, and its cost shows on your P&L.
- **Receipts**: attach a photo or PDF to any expense. Receipts are saved in a "Profit Tracker receipts" folder next to your database, so they sync with it.
- **Import wizard**: upload a CSV of sold items, inventory, expenses or bulk purchases, check which column is which, then import.

**UK tax (Self Assessment)**
- Live SA103S figures for each tax year (6 April – 5 April), boxes 9–22, on the cash or accruals basis.
- Checks whether the £1,000 trading allowance beats claiming expenses.
- **Guided data checks**: sales with no cost, estimated fees and stock with no purchase date, each with a link to fix it.
- **Expense ledger** with HMRC categories mapped to SA103S boxes.
- **Mileage log** at HMRC simplified rates: 55p a mile for the first 10,000 miles from 2026/27 (45p before), then 25p.
- **Post office trips from dispatches**: dispatch dates come from eBay (plus other-marketplace sales with postage). Save your home postcode and your drop-off points (post office, parcel locker, Collect+ shop); the driving distance is worked out once using postcodes.io and OpenStreetMap routing, or you can type it. Each dispatch day becomes one return trip: tick the days and log them, or switch on automatic logging to your default drop-off point.
- **Closed-year protection**: mark a year as filed, and any edit that would change it asks before saving.
- CSV export for your accountant.

This is a record-keeping aid, not tax advice.

## Install (Windows / Mac / Linux)
1. Install Python 3.10+ from python.org. On Windows, tick "Add to PATH".
2. Get Flip for Free: on **github.com/JosephsStuff/flip-for-free** click **Code → Download ZIP** and unzip it,
   or `git clone https://github.com/JosephsStuff/flip-for-free.git`.
3. Double-click `run.bat` (Windows) or `run.command` (Mac), or run `./run.sh` (Linux).
   (`python app.py` works too, but the run files also restart Flip for Free after an update.)
4. Your browser opens at **https://localhost:8765**. The first time, the browser warns about the certificate. That's expected: the app makes its own certificate because eBay needs an https sign-in address. Choose *Advanced → Proceed to localhost*.

**On a server instead?** One command on Ubuntu (e.g. Oracle Cloud's free tier) installs everything:
```
curl -fsSL https://raw.githubusercontent.com/JosephsStuff/flip-for-free/main/deploy/install.sh | bash
```
Follow the step-by-step guide at **https://josephsstuff.github.io/Flip-For-Free/** (also in `deploy/DEPLOY_ORACLE.md`).

## Updating
**Settings → Updates → Update now** downloads the latest version from GitHub, backs up your database first, and restarts.
Or tick *Install new versions automatically*. Your data is never in the app folder (the database and receipts are in the
folder you chose on the setup page, and settings are in your user config folder), so updating can't lose your progress.
The previous version is kept in your config folder under `updates/` in case you need to go back.

**Your data:** **Settings → Your data** lets you download a complete copy of your database at any time, or restore one.
That's also how you move Flip for Free between your computer and a server.

Want to look around first? Run `python app.py --demo` to use fake data without an eBay account.

## First-time setup
1. **Database location**: pick a folder inside Google Drive (e.g. `G:\My Drive\eBay` on Windows, or `~/Library/CloudStorage/GoogleDrive-…/My Drive/eBay` on Mac). The app creates `ebay_profit_tracker.db` there.
2. **eBay developer keys** (free, one-off). The field names in the app match eBay's labels:
   - **First you need an eBay developer account.** Register at https://developer.ebay.com (click *Register*). eBay reviews new accounts, which takes about 1 business day, and you can't create keys until it's approved. If it's rejected, use eBay's developer support form to request a manual review.
   - Go to https://developer.ebay.com/my/keys (*Application Keysets*) and create a **Production** keyset. If it says the keyset is disabled, open the *Marketplace account deletion* notice and choose the exemption.
   - Copy **App ID (Client ID)**, **Dev ID** and **Cert ID (Client Secret)** from the **Production** box into the fields with the same names.
   - Next to the keyset: *User Tokens → Get a Token from eBay via Your Application → Add eBay Redirect URL*. Set *Your auth accepted URL* and *Your auth declined URL* to `https://localhost:8765/auth/callback`, tick *OAuth Enabled* and save. If eBay won't accept a localhost URL, see **If eBay won't accept localhost** below.
   - Copy the value in the **RuName (eBay Redirect URL name)** column (a short name, not a URL).
3. Click **Sign in with eBay**, sign in and click *Agree*. The app asks for read access to orders, finances and your identity, plus Promoted Listings (for delayed promotion). If you signed in before automations existed, sign out and in again once. eBay sends you straight back to the app, signed in. The first sync starts automatically. By default it looks back 365 days; eBay keeps about 2 years of orders.

## If eBay won't accept localhost
Some eBay accounts won't save `https://localhost/...` as the auth accepted URL. The fix is the tiny relay page in the `relay` folder. It's one static HTML file that runs only in your browser: eBay sends you to it after sign-in, and it instantly forwards you to `https://localhost:8765/auth/callback`. Sign-in stays fully automatic.

1. Host `relay/index.html` on any free https static host. For example, create a public GitHub repo, upload the file, and turn on *Settings → Pages*. You get an address like `https://yourname.github.io/ebay-relay/`.
2. Use that address as *Your auth accepted URL* and *Your auth declined URL* on eBay.

The sign-in code that passes through the page only works once, expires after 5 minutes, and is useless without your Cert ID, so the relay never sees anything that could access your account.

## Let Claude run it for you
Flip for Free has its own API, so Claude can do the inputs for you. You can say things like *"I bought a binder of 40 Yu-Gi-Oh cards for £60 at the car boot, log it"*, *"which sales are missing a cost? They were all about £2 each"*, *"I sold a Zara coat on Vinted for £28 yesterday"*, *"log 6 miles return to the post office"* or *"how much profit did I make this month and what do I need to fix before filing?"*.

1. In Flip for Free, go to **Settings → API keys**, type a name (e.g. "Claude desktop") and click **Create API key**. Copy the key; it's only shown once.
2. Install the connector's library once: `pip install -r requirements.txt`
3. In the Claude desktop app open **Settings → Developer → Edit Config** and add this (use your own folder path and key):
   ```json
   {
     "mcpServers": {
       "flip-for-free": {
         "command": "python",
         "args": ["C:\\Users\\you\\flip-for-free\\mcp_server.py"],
         "env": { "FLIP_URL": "https://localhost:8765", "FLIP_API_KEY": "flip_your_key_here" }
       }
     }
   }
   ```
4. Restart Claude. Flip for Free's tools then appear in Claude, as long as the app is running.

Check the connection any time with `python mcp_server.py --check`.

**What Claude can do:** check profit and tax figures; add stock and bulk buys (with their items); fill in costs, packaging and handling; record sales from Vinted, Facebook and car boots; log expenses and mileage; link listings to bulk buys; write items off; read alerts; and start an eBay sync. Changes to a tax year you've marked as filed are refused unless you explicitly tell Claude to change it.

**The API directly:** send `Authorization: Bearer <key>` to `https://localhost:8765/api/v1`. `GET /api/v1` lists every endpoint. Revoke a key in Settings and it stops working straight away.

## Run it 24/7 on a free server
See the [Oracle deployment guide](https://josephsstuff.github.io/Flip-For-Free/) (or **deploy/DEPLOY_ORACLE.md**): Oracle Cloud's Always Free server plus Tailscale gives you Flip for Free running all the time,
private to your own devices, at a real https address (which also fixes eBay sign-in). `deploy/install.sh` does the
setup, including automatic restarts and nightly backups. Server mode is `python app.py --public-url https://…`.

## Using it on your phone
Start the app with `python app.py --lan`, then open **Settings → Use on your phone** on your computer and scan the one-time pairing link (it expires after 10 minutes). Your phone must be on the same Wi-Fi. Phones can add stock and costs, record sales, and check the dashboard and relists. Setup and eBay sign-in only work on the computer. Without `--lan` the app can only be reached from the computer it runs on.

## Using it on another device
Install the app on the other device, choose the **same Google Drive folder** on its setup page, and sign in with eBay. The setup page sees the existing database and opens it instead of making a new one. Your eBay keys, sign-in token, costs and bulk buys are all stored in that one file.

⚠ Only use it on **one device at a time**. If two devices write to the file before Google Drive syncs, Drive creates a conflicting copy. The app shows a warning if another device used the database in the last 10 minutes.

## Privacy
Everything stays on your computer and in your Drive folder. The app binds to `127.0.0.1` only. The database contains your eBay app secret and sign-in token, so keep that Drive folder private (don't share it).

## Notes
- UK/EU sellers must digitally sign Finances API calls. The app creates the signing key through eBay's Key Management API and signs each call automatically.
- If the Finances API fails, sales still show, but with fees estimated from the order (marked **est.**) until the next sync succeeds.
- Bulk buy costs count against profit in the period you **bought** the lot. Use the Bulk buys page to see each lot's lifetime profit.
- Options: `--port 9000`, `--no-https` (only if your RuName accepts http), `--no-browser`.

## Files
`app.py` core pages · `features.py` inventory, insights, automations, tax · `automation.py` relist/promote engine · `taxcalc.py` SA103S figures · `ebay_api.py` OAuth, signing and eBay APIs · `sync.py` downloads data · `profit.py` deduction and cost maths · `db.py` storage · `api.py` JSON API · `mcp_server.py` Claude connector · `demo.py` fake data.
