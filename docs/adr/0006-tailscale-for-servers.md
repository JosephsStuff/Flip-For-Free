# ADR-0006: Servers are reached through Tailscale, not open ports

## Status
Accepted

## Context
Running 24/7 on a free Oracle server needs HTTPS (eBay requires it for sign-in) and must not expose the app to the
internet.

## Decision
On a server Flip listens on 127.0.0.1 only, and `tailscale serve` publishes it to the owner's tailnet with a real
certificate (`https://flip.<tailnet>.ts.net`). Access control is Tailscale's; Flip trusts the proxy in server mode
(see the 1.4.1 "Forbidden" fix) and the owner still signs in with eBay.

## Consequences
- No open ports, no domain to buy, valid HTTPS for eBay's redirect.
- Each device that uses Flip needs Tailscale installed and signed in.
