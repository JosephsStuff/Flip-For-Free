# ADR-0007: Server-rendered pages with no front-end build step

## Status
Accepted

## Context
The app must run on old laptops, phones over Wi-Fi, and tiny free servers, and be easy for one person to change.

## Decision
Flask + Jinja templates, one stylesheet built on design tokens (type, spacing, radius scales), small plain-JavaScript
enhancements, an inline SVG icon set (`icons.py`). No bundler, no framework.

## Consequences
- The code on GitHub is exactly what runs; updates are a `git pull`.
- Accessibility is checked with axe-core (zero WCAG 2.1 AA violations as of 1.7.3).
