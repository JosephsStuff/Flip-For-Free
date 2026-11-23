# Changelog

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
