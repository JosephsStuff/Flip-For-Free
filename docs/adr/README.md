# Architecture decision records

Short notes on the decisions that shape Flip for Free: what was decided, why, and what was turned down.
Add a new numbered file when you make a decision that someone would otherwise undo by accident.
Don't delete old ones; mark them *Superseded by ADR-00N* instead.

| ADR | Decision |
|---|---|
| [0001](0001-self-hosted-with-your-own-ebay-keys.md) | Each seller runs their own copy with their own eBay keys |
| [0002](0002-sqlite-in-a-folder-you-choose.md) | SQLite, in a folder the user chooses |
| [0003](0003-true-profit-from-money-movements.md) | True profit is rebuilt from eBay's money movements, reconciled to the penny |
| [0004](0004-official-apis-only-for-other-marketplaces.md) | Official APIs only; copy-and-paste where a marketplace forbids tools |
| [0005](0005-stock-sync-once-per-sale.md) | Stock is kept in step once per sale, only for listings that existed when it sold |
| [0006](0006-tailscale-for-servers.md) | Servers are reached through Tailscale, not open ports |
| [0007](0007-server-rendered-no-build-step.md) | Server-rendered pages with no front-end build step |
| [0008](0008-main-and-beta-channels.md) | Main and beta release channels from two branches |
| [0009](0009-markets-and-tax-summaries.md) | Other eBay markets: tax summaries for the top five, profit tracking for the rest |
