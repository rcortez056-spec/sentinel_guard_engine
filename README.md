# Sentinel Guard Engine

**Sentinel doesn't just execute. It guards.**

Every transaction is locked, logged, and checked before execution. If something fails, it stops and flags it for you. No spam, no duplicates, no silent failures. You're in control.

## The 7-Step Flow

1. **Incoming Request:** Generate a unique transaction ID (`TX-timestamp-seq`). Status: `INCOMING`.
2. **Guard Clause: Query State:** Check whether the transaction is already in progress. If yes, block it (`BLOCKED`). If no, continue.
3. **Guard Clause: Evaluate Rules:** Continue only if the request is valid and the state lock is free. All checks must pass, or the transaction is rejected.
4. **Lock State:** Lock the transaction to prevent concurrent execution of the same request. This is the guard against spam.
5. **Execute Action:** Run only if all guards pass. If an error occurs, flag it for manual review. No auto-retry spam.
6. **Release Lock:** Unlock the transaction and mark it complete.
7. **Log Everything:** Timestamp every step. Record the final status: `SUCCESS`, `BLOCKED`, or `ERROR`. Ready for audit trail or debugging.

## The Three Core Panels

- **Guard Clause Engine:** Shows what blocks or allows requests.
- **State Database:** Shows transaction locks and queries.
- **Rule Evaluator:** Shows the conditional logic that gates execution.
