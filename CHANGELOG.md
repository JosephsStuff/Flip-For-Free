# Changelog

## 1.8.0-beta.9
- Check for updates sees a new release within about a minute: Flip now reads the version from GitHub's API instead
  of a file copy that GitHub can cache for up to 5 minutes (it falls back to that copy if the API is busy).

## 1.8.0-beta.8
- Phones: the date-range box is now full width, lining up with the True profit, Buyers paid and other boxes.

## 1.8.0-beta.7
- Urgent update alerts: if a release fixes a security problem (or anything else that can't wait), Flip shows a banner
  on every page with an Update now button, plus a notification, until you're on the fixed version. Flip checks for
  alerts every 3 hours. The alert text comes from the project's own GitHub (ALERTS.json) and is shown as plain text.

## 1.8.0-beta.6
- Notifications on phones: one line each (the start of the title); tap one to read it all. Swipe a notification left
  or right to dismiss it, or use the × (on any screen). Dismissed notifications stay gone and count as read.

## 1.8.0-beta.5
- Phones: the date ranges (Today, This week, 31 days…) sit in a tidy three-column grid instead of uneven rows, and
  the Custom range panel lines up From and To side by side with a full-width Apply button.

## 1.8.0-beta.4
- Sales and inventory never need side-scrolling: long bulk-buy names wrap onto more lines, column headings and dates
  wrap on laptop screens, and tablets get the card layout phones use (four figures per row). Checked from 360px to
  1366px wide.

## 1.8.0-beta.3
- Showcase mode shows a small "Showcase" tag next to the logo (like the Beta tag) instead of a banner, and hides the
  Sync button while it's on (sync is off for fake data). Turn it off in Settings → Showcase mode.

## 1.8.0-beta.2
- Showcase mode (Settings → Showcase mode): show someone around Flip with a year of fake sales, stock, bulk buys and
  automations instead of your own figures. Only that browser changes; your data, syncs and automations carry on as
  normal, other devices still see your figures, and nothing is sent to eBay. A banner shows while it's on, with a
  Turn off button; signing out also turns it off. The fake data is a separate copy you can click around and change,
  refreshed daily or with "Reset the fake data".

## 1.8.0-beta.1
- Sell on eBay outside the UK. Settings → Market and currency sets your eBay site and currency; money shows as $, €,
  A$, C$ and so on everywhere (pages, notifications, CSVs), eBay links go to your site, and distances use miles or km.
- Tax summaries for the five biggest eBay markets, laid out like each country's form: US Schedule C, German EÜR,
  Australian business schedule and Canadian T2125 (the UK keeps its full Self Assessment page). Each uses that
  country's tax year, its mileage rate (IRS 72.5¢/mile for 2026, Germany €0.30/km, Australia 88¢/km up to 5,000 km;
  Canada uses actual car costs instead), and shows how close you are to the key threshold (1099-K, Kleinunternehmer,
  GST, GST/HST).
- Every other eBay site gets profit and loss, expenses and mileage with no tax page, and a free choice of currency.
- CardTrader and Shopify sales convert into your currency, not always pounds. Money boxes accept 12,50 as well as 12.50.
- Try another market in the demo: `python app.py --demo --market EBAY_US`.
- Record-keeping aid, not tax advice.

## 1.7.7
- Beta channel: Settings → Updates → Release channel lets you choose the main release (the default) or the beta,
  which gets new features to try before they go into the main release. A "Beta" badge shows in the top bar while
  you're on it, and you can switch back at any time (your data is backed up first).

## 1.7.6
- Phones: the "Flip for Free" name is back next to the logo; it fits on the top row with the icons and Sync.

## 1.7.5
- Phones: the top bar is two rows instead of three. Just the logo (no name), with notifications, settings and Sync
  on the same row and the menu underneath. Your account name and sign-out are hidden on phones; Sign out is now
  also in Settings → eBay connection. On very narrow screens Sync shows just its icon.

## 1.7.4
- Docs for contributors: CONTRIBUTING.md (run, test, where things are, how the docs site is generated) and seven
  architecture decision records in docs/adr explaining why Flip works the way it does.

## 1.7.3
- Accessibility: every page now passes an automated WCAG 2.1 AA check (axe-core) in light and dark mode, down from
  over 600 issues. Every checkbox, dropdown and text box has a name a screen reader can read ("Select Blue-Eyes White
  Dragon…", "Tax year", "Drop-off point"), links inside sentences are underlined, and low-contrast text (refunded rows,
  green figures and links in light mode, warning text) was darkened or lightened to at least 4.5:1.
- Website: code examples can be scrolled with the keyboard; the API and install pages pass the same check.

## 1.7.2
- On phones, the Sales and Inventory lists show one card per row with buyer paid, eBay took, money made, price paid
  and profit (or status, location and cost) visible, instead of a table you had to scroll sideways.
- The full menu fits on a 1280px laptop again ("Accounting" was cut off); the last sync time moves to the Sync
  button's tooltip on narrower screens.
- Item page: "Record a sale" is the main action, with "List on CardTrader", "List on Shopify" and "Copy to Vinted"
  grouped separately; the timeline no longer shows eBay's internal line numbers.

## 1.7.1
- Fix: a sale on CardTrader or Shopify from before an item was listed on another shop could still take stock off that
  shop when the item was also live on eBay. Each listing is now only adjusted for sales made after it existed.
- Error reports now also remove the exact values of every stored secret (eBay, CardTrader and Shopify keys and tokens),
  plus JWT, Shopify and Tailscale token formats, before you see or send them.
- A proper test suite (56 tests on demo data: profit reconciles to the penny, every page and API endpoint, access
  rules, the other-shops flows, error scrubbing, and the sync not locking the database), run on GitHub for Python
  3.10 and 3.12 on every push.
- Removed unused imports.

## 1.7.0
- **CardTrader**: connect with your API token (Settings → Other shops). On an item, Flip guesses the set from the
  card code in the title, ranks the matching cards in CardTrader's catalogue, shows CardTrader's cheapest listings for
  that card and its condition/language/edition fields, and lists it. Change the price or quantity, or remove it, from
  the item. CardTrader orders are imported as sales (fees and postage included, converted to GBP with ECB rates).
- **Shopify**: connect a Dev Dashboard app (client credentials; 24-hour tokens renewed automatically). Create a product
  from an item's eBay listing (title, cleaned description, price, stock, photos) and publish it to the Online Store.
  Shopify orders are imported as sales, with your Shopify payment fee settings applied.
- **Stock in step across shops**: a sale on any shop lowers the item's stock on the others, once per sale, and only
  for sales made after the item was listed there. Problems raise a notification.

## 1.6.0
- **Copy to Vinted** (on each item): the eBay title, the description with the formatting removed plus item specifics,
  the closest Vinted condition, a suggested price that keeps your eBay take-home, and the listing's photos as a zip,
  each with a copy button. You paste them into Vinted yourself: Vinted's terms don't allow other tools to post for you.
- Mark an item as listed on Vinted (with its link). It's tagged in Inventory, and if it sells on eBay you get a
  notification to take it down on Vinted.
- Recording a sale from another marketplace can now update eBay: it lowers the item's eBay listing quantity, or ends it
  if none are left, then reads the listing back to check. Also available in the API and the Claude connector
  (`update_ebay`).
- "Sold on Vinted" shortcut on items that are on Vinted.

## 1.5.0
- A design pass across the whole app, following the tasteskill design rules:
  - one type scale (13px labels, 14px secondary text, 16px reading text) instead of about 20 sizes; no small text
    below 13px
  - sentence case everywhere: table headings, status pills and section headings are no longer spaced-out capitals
  - one spacing scale (4, 8, 12, 16, 24, 32, 48, 64) and three corner radii
  - buttons and fields are 44px tall (36px for compact buttons in tables and toolbars), and buttons beside fields
    match their height
  - a visible focus ring on every button and link when you use the keyboard, plus a "Skip to content" link
  - a proper icon set (bell, settings, warnings, phone, lock, pause and so on) replaces the emoji
  - dashboard cards line up: equal widths in each row, and on a phone one card per row
- Fix: on a phone, the dashboard, Settings, Automations, Insights, Mileage and Expenses pages could scroll sideways.

## 1.4.3
- The "Accounts" menu is now called "Accounting".

## 1.4.2
- Fix: while a sync was running (especially the first, long one) other pages could hang and then show
  "database is locked", e.g. Automations. The sync no longer keeps the database locked while it waits for eBay.

## 1.4.1
- Fix: on a server (Oracle, behind Tailscale Serve or Cloudflare) every page said "Forbidden". The
  check that keeps other Wi-Fi devices out was also turning away visitors coming through the proxy.

## 1.4.0
- When Flip for Free starts from a terminal (run.bat, run.command, run.sh), it says when it's ready, gives the
  address for this computer and the address for other devices on the same Wi-Fi (and that it only works on that
  network), and says what to do next to finish setting up.
- The run files now let paired devices on your network use it (each one is paired once from this computer).
  Use `--local-only` to keep it to this computer.

## 1.3.1
- Docker: `docker compose exec flip flip-address` prints the address to open in your browser (the same command
  on Windows, Mac and Linux). The address is also shown at the top of the container's log.

## 1.3.0
- Time to sell (Insights): for single-quantity listings, the average, median and spread of days from first
  listed (across relists) to sold, plus what's been listed longest. For multi-quantity listings, how often one
  sells ("one every N days"), last sale, stock left and roughly when each will sell out.
- Each item's page shows its own time to sell; also in the API (`/api/v1/time-to-sell`) and for Claude.

## 1.2.0
- Error reporting: a friendly error page with a "Report this on GitHub" button, notifications when a sync or
  automation fails, Settings → Report a problem (bug report, feature idea, recent errors, error log). Reports
  open pre-filled on GitHub for you to check and submit; personal details are removed first.
- Run Flip for Free in Docker: `docker compose up -d --build`. Data stays in `flip-data`; in-app updates
  work inside the container.

## 1.1.1
- New licence: the Functional Source License 1.1 (MIT Future License). Free to use, including for your own
  reselling business; selling, rebranding or hosting Flip for Free commercially needs a commercial licence.
  Each version becomes MIT two years after release. Versions up to 1.1.0 stay MIT.

## 1.1.0
- New Flip for Free logo: three Fs mid-flip, each one bigger and stronger than the last.
- New look: mint on grey, in light and dark mode.
- New icons for the browser tab, home screen and installed app.

## 1.0.0
- First public release of Flip for Free: eBay sync with every fee and refund, true profit per sale, inventory,
  bulk buys, automations (relist, repricing, delayed promotion), insights, UK Self Assessment, mileage,
  P&L, goals, the Flip for Free API and Claude connector.
- Update from GitHub inside the app (Settings → Updates) without losing data.
- Choose how often Flip for Free syncs with eBay (every 5 minutes to daily).
- Download a copy of your database, or restore one, from Settings.
