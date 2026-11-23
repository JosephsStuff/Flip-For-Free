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
- Money is GBP in the database; convert on the way in (`fx.py`).

## Releasing

Bump `VERSION`, add a `CHANGELOG.md` entry, push to `main`. Users get it from Settings → Updates.
