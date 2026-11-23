# ADR-0002: SQLite, in a folder the user chooses

## Status
Accepted

## Context
Resellers want their data on more than one computer, backed up, and easy to move to a server. Most have Google
Drive, OneDrive or Dropbox. Few would run a database server.

## Decision
All data lives in one SQLite file (`ebay_profit_tracker.db`) in a folder picked on the setup page. Writes go through
one process-wide lock and commit per item, so long jobs never hold the database while waiting on the network
(see the 1.4.2 fix in the changelog).

## Alternatives considered
- **PostgreSQL/MySQL:** another service to install and secure for a one-person tool.
- **JSON files:** no transactions, slow queries over years of orders.

## Consequences
- Backup is copying a file; Settings → Your data downloads or restores it.
- Two copies writing to the same file at once must be avoided (the app warns about it).
