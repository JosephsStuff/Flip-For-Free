# ADR-0009: Other eBay markets: tax summaries for the top five, profit tracking for the rest

## Status
Accepted (2026-11)

## Context
Flip was built for UK sellers: pounds everywhere, HMRC tax years and mileage rates. Sellers on other eBay sites want
the same profit tracking in their own currency. Building and maintaining full tax tools for every country isn't
realistic.

## Decision
- `markets.py` describes each eBay site: currency, miles or km, eBay domain and (for the five biggest: US, UK,
  Germany, Australia, Canada) a tax profile with the tax year, form lines, mileage rate and key threshold.
- Expenses keep one internal grouping (numbered like the UK SA103S boxes 11–19). Each tax profile maps those groups
  to its own form's lines, so one calculation (`taxcalc.compute`) serves every market and `taxcalc.summary` reshapes
  it. A test checks every summary reconciles with the full figures.
- Every other site gets profit and loss, expenses and mileage, no tax page, and a currency picker.
- Amounts are stored as eBay reports them (the account's currency); only the symbol and tax rules change.

## Consequences
- Tax rates and thresholds in `markets.TAX` need checking once a year.
- Summaries are a record-keeping aid, linked to each tax authority's guidance, not tax advice.
