# Contributing to Flip for Free

Thanks for helping. Flip for Free is a plain Flask app with no build step: what's on GitHub is what runs.

## Run it

```bash
pip install -r requirements.txt
python app.py --demo          # a year of fake data, no eBay account needed
```

Open https://localhost:8765 (accept the certificate warning) or add `--no-https` and use http.

## Test it

```bash
pip install pytest ruff
ruff check --select F,E9 *.py tools/*.py tests/*.py
python -m pytest -q tests
```

The tests run on a fresh demo database each time and never touch eBay, CardTrader or Shopify (those are replaced
with fakes). GitHub runs the same commands on Python 3.10 and 3.12 for every push and pull request.

What's covered: profit reconciles to the penny for every sale, every page renders, every API endpoint answers, LAN
and server access rules, eBay quantity updates (both ways eBay may count), CardTrader and Shopify order import and
once-only stock sync, error-report scrubbing, and the sync not locking the database.

## Where things are

| File | What it does |
|---|---|
| `app.py` | Flask app, setup, eBay sign-in, access rules, start-up |
| `sync.py`, `ebay_api.py` | eBay orders, money movements and listings |
| `profit.py` | True profit per sale, bulk-buy cost splitting |
| `markets.py`, `taxcalc.py` | eBay sites, currency, tax years, mileage rates; UK Self Assessment and other markets' tax summaries |
| `features.py`, `features2.py` | Inventory, bulk buys, insights, tax, P&L, tools |
| `automation.py` | Relist and delayed promotion |
| `crosslist.py` | Copy to Vinted; eBay stock updates after sales elsewhere |
| `stores.py`, `cardtrader_api.py`, `shopify_api.py`, `fx.py` | CardTrader and Shopify; stock in step; currency |
| `api.py`, `mcp_server.py` | JSON API and Claude connector |
| `errors.py`, `updater.py` | Error reports (scrubbed) and self-update |
| `templates/`, `static/style.css`, `icons.py` | Pages, design tokens, icon set |

The [code map](https://josephsstuff.github.io/Flip-For-Free/code-map.html) shows how they connect, and
[docs/adr](docs/adr/README.md) explains the big decisions.

## Docs site

The website is `docs/` (GitHub Pages). These pages are generated, so edit the scripts, not the HTML:

```bash
python tools/gen_api_docs.py      # calls every API endpoint on demo data -> docs/openapi.json, docs/api-examples.json
python tools/gen_api_page.py      # -> docs/api.html
python tools/gen_how_page.py      # -> docs/how-it-works.html
python tools/gen_install_page.py  # -> docs/install.html
```

If you add an API endpoint, add it to `api.ENDPOINTS` and an example call to `tools/gen_api_docs.py`.

## Style

- Plain English in the interface: say what happened and what to do next.
- Use the tokens in `static/style.css` (`--s-*` spacing, `--t-*` type, `--r-*` radius) rather than new values.
- Every control needs a visible label or an `aria-label`; check new pages with axe-core.
- Never automate a marketplace whose terms forbid it (see ADR-0004).
- Money is stored in your own currency (Settings → Market and currency); convert other shops' money on the way in
  (`fx.to_home`). Show money with the `money` filter or `markets.money()`, never a hard-coded symbol.

## Releasing: main and beta

There are two release channels. Users pick one in Settings → Updates → Release channel.

| Channel | Branch | Versions | Who it's for |
|---|---|---|---|
| Main release | `main` | `1.8.0` | Everyone (the default) |
| Beta | `beta` | `1.8.0-beta.1`, `1.8.0-beta.2` … | People who want to try new features first |

1. **New feature:** commit it to `beta`, set `VERSION` to the next version with a beta number (`1.8.0-beta.1`, then
   `-beta.2` for each fix) and add a `CHANGELOG.md` entry with that exact version as its heading. Beta users get it
   from Settings → Updates (or automatically, if they've turned that on).
2. **Happy with it:** merge `beta` into `main`, set `VERSION` to `1.8.0` (no beta number), tidy the changelog entry
   and push. Main users get it; beta users get it too, because `1.8.0` counts as newer than `1.8.0-beta.N`.
3. **Fixes for main** that can't wait go straight to `main` as `1.7.x`; then merge `main` into `beta` so beta has them.

Switching back from beta to main installs the latest main release even if its version number is lower. The
database is backed up first; new tables or columns a beta added are simply ignored by older code (see ADR-0008).

## Urgent update alerts (security fixes)

If a release fixes something users must install quickly, add an entry to `ALERTS.json` **on the `main` branch**.
Every copy of Flip checks that file every 3 hours (and whenever someone clicks Check for updates). Anyone running an
affected version sees a banner on every page with an **Update now** button, plus a notification. The banner
disappears by itself once they're on the fixed version.

```json
{
  "alerts": [
    {
      "id": "sec-2026-01",
      "level": "critical",
      "title": "Security fix: please update today.",
      "body": "A bug could let someone on your network see your eBay sign-in.",
      "fixed_in": {"main": "1.7.8", "beta": "1.8.0-beta.7"},
      "since": "1.5.0",
      "expires": "2027-06-30"
    }
  ]
}
```

- `id`: letters, numbers, `.`, `_`, `-`; it's also used so each user gets one notification per alert.
- `level`: `critical` (red banner) or `warning` (amber).
- `fixed_in`: the first fixed version, either one version for both channels or one per channel.
- `since` (optional): the first affected version; older versions don't see the alert.
- `expires` (optional): stop showing it after this date.
- Title and body are shown as plain text (no HTML or links); keep them short and say what to do.

Release the fix first, then publish the alert, so the Update now button installs the fixed version.
