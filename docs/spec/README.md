# BBMS rewrite — specification

This folder describes **what the new Build Brighton Member System (BBMS) must do**, based on a
line-by-line review of the legacy system (Laravel, release `20230807074222`) and its
production logs.

It is written for volunteers who will build and maintain the new system, so it explains *why* rules
exist as well as *what* they are. Where the legacy system behaves in a way we should **not** copy, it
is called out explicitly.

| File | Contents |
|---|---|
| [01-overview.md](01-overview.md) | Purpose, users, roles, scope, proposed technology, open decisions |
| [02-data-model.md](02-data-model.md) | Entities, fields and relationships (legacy → new) |
| [03-membership-lifecycle.md](03-membership-lifecycle.md) | Joining, statuses, subscriptions, the nightly jobs, leaving |
| [04-payments-and-credit.md](04-payments-and-credit.md) | Payment sources & reasons, GoCardless, Stripe, PayPal, cash, statement import, member credit |
| [05-access-control-and-equipment.md](05-access-control-and-equipment.md) | Key fobs, door entry, the ACS device API, equipment, inductions, usage billing |
| [06-other-features.md](06-other-features.md) | Storage boxes, proposals & voting, expenses, notifications, emails, directory, stats, policies |
| [08-migration-and-cutover.md](08-migration-and-cutover.md) | Data migration, running in parallel, switching hardware over |
| [09-device-api-v2-build.md](09-device-api-v2-build.md) | Build brief for the v2 device API: exact endpoints, models, tests (a work order, ready to pick up) |

## Status

- **Draft 1 — 2026-11-04.** Derived from the code.
- **Draft 2 — 2026-11-05.** Decisions recorded: Django + HTMX on DigitalOcean App Platform with Spaces; dropped features
  (01 §1.4); credit calculated from the ledger; prices as settings; emergency PIN removed; activity visible to all members;
  device API v2 for the planned new firmware. Remaining questions are listed in 01 §1.7 for the trustees.
- Items marked **❓ Decision** need an answer from the makerspace before build starts.
- Items marked **⚠️ Verify** are inferences from code that should be checked with someone who runs the space.

## Reference environment

A sanitized, working clone of the legacy system is available for checking behaviour
(see the project notes for access). It contains only `@example.com` fixture data.
