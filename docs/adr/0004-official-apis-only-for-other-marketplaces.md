# ADR-0004: Official APIs only; copy-and-paste where a marketplace forbids tools

## Status
Accepted (2026-11)

## Context
Sellers asked to cross-list to Vinted, CardTrader and Shopify. In 2026 Vinted's terms forbid unauthorised third-party
tools (enforced with temporary bans); its Pro API is invite-only. Depop and Facebook Marketplace APIs are
partner-only. Cardmarket isn't accepting API applications. CardTrader and Shopify offer self-service APIs.

## Decision
- CardTrader and Shopify: full integration through their official APIs, with the seller's own token/app.
- Vinted, Depop, Facebook: Flip never signs in or posts. "Copy to Vinted" prepares text, condition, price and photos
  for the seller to paste, and tracks that the item is listed there.

## Alternatives considered
- **Browser automation / form filling:** breaks those marketplaces' terms and puts the seller's account at risk.

## Consequences
- No account risk; manual steps remain for Vinted. If a seller gets Vinted Pro API access, an integration can be
  added alongside CardTrader's in `stores.py`.
