# ADR-0003: True profit is rebuilt from eBay's money movements, reconciled to the penny

## Status
Accepted

## Context
eBay's order data says what the buyer paid; fees, promoted-listing charges, postage labels, refunds and regulatory
fees arrive separately in the Finances API, sometimes days later, and not always labelled.

## Decision
For each order, net received = the sum of every money movement linked to it; eBay deductions = buyer paid − net.
Known types are labelled; anything left over is shown as "Other eBay adjustments", so totals always reconcile.
Until the money data arrives, fees are estimated and marked *est.*

## Consequences
- The figure matches what actually reached the seller's account.
- `tests/test_profit.py` asserts the reconciliation for every demo sale.
