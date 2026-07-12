# RC1 Load Test Profile

Status: RC1 load and stress interpretation guide.

RC1 load tests use two profiles. Do not mix their results.

## Normal Anti-Abuse Profile

Purpose: production-like anti-abuse validation.

- Same-IP mass login may return HTTP 429.
- HTTP 429 under same-IP mass account creation/login is expected behavior.
- Do not disable production anti-abuse just to make a throughput number look
  cleaner.
- Use this profile to verify rate limits, auth guards, and abuse boundaries.

## Functional Load-Test Profile

Purpose: chain, wallet, transaction, and governance consistency.

- Run only in development/staging/isolated environments.
- May raise rate limits or distribute client identities.
- Must not use production secrets.
- Must not disable production rate limits in production.

## Stress Invariants

Every RC1 stress run must check:

- no negative balance
- no duplicate wallet
- no duplicate request
- no double spend
- no duplicate active dispute for the same tx
- no duplicate treasury execute
- no multisig execute before threshold
- no user multisig outbound transfer
- chain verify pass after stress
- derived cache/replay consistency pass after stress

If a run hits same-IP HTTP 429 before completing wallet/chain invariants, record
it as anti-abuse behavior and rerun the functional chain portion with an
isolated load-test profile.
