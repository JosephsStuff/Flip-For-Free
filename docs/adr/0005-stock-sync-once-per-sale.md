# ADR-0005: Stock is kept in step once per sale, only for listings that existed when it sold

## Status
Accepted (2026-11)

## Context
With the same item on eBay, CardTrader and Shopify, a sale on one must lower stock on the others, without ever
applying the same sale twice (syncs repeat) or applying an old sale to a listing created later.

## Decision
- `stores.propagate_sale` records every handled sale in `stock_sync_done`; a sale is applied at most once.
- Listings are skipped when they were created after the sale (`created_at > sold_at`).
- eBay: lower the quantity (`ReviseInventoryStatus`, read back and corrected if eBay counts differently) or end the
  listing at zero. CardTrader: `increment` (deletes at zero). Shopify: compare-and-set `inventoryAdjustQuantities`
  with an idempotency key.
- Failures raise a notification rather than retrying blindly.

## Consequences
- Covered by `tests/test_shops.py`, which found the "created after the sale" bug before release.
