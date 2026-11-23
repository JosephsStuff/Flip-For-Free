# ADR-0001: Each seller runs their own copy with their own eBay keys

## Status
Accepted

## Context
A profit tracker needs read access to a seller's orders and finances, and (for automations) the ability to change
listings. A hosted service would hold every user's eBay tokens and sales history, need an eBay app approved for
many users, and cost money to run.

## Decision
Flip for Free is software you run yourself: on your computer, in Docker or on a free server. Each seller registers
their own free eBay developer account and pastes their own keys on the setup page.

## Alternatives considered
- **Hosted multi-user service:** central breach risk, running costs, eBay app review for public access.
- **Desktop app with shared keys:** shipping one Cert ID to everyone would expose it.

## Consequences
- Setup needs a one-off eBay developer account (about a day for approval). The install guide puts this first.
- No data leaves the seller's machine except calls to eBay and the shops they connect.
