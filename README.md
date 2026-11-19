
# Sentinel Guard Engine

Sentinel checks every agent action before it runs. Each transaction is assigned an ID, checked against the rules, locked to prevent duplicate execution, logged, and released. If an action fails, it is flagged for manual review and is not retried automatically.

## The 7-Step Flow
1. **INGEST:** Assign a transaction ID (`TX-timestamp-seq`). Status: `INCOMING`.
2. **STATE QUERY:** Block if the same idempotency key is locked or already succeeded (`BLOCKED`).
3. **RULES:** Reject malformed requests before anything executes.
4. **LOCK:** Claim the idempotency key atomically (enforced by a partial unique index).
5. **EXECUTE:** Run the registered action. Errors go to manual review.
6. **RELEASE:** Mark `SUCCESS` or `ERROR` and release the lock.
7. **LOG:** Write every step to the audit log with a UTC timestamp.

## Demo Actions
`send_email` and `crm_update` are simulated stubs. They don't contact an email server or CRM. Replace them with real integrations before production use.

## Status
Prototype. Python standard library only.

## Live Demo
https://sentinel-1-ramon.netlify.app/: scripted scenarios in the browser, no live API calls.
