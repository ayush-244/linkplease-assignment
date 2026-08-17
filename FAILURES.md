# FAILURES.md — Known Limitations and Honest Edge Cases

This document is intentionally honest. Every item below describes a real
failure mode that is possible with the current architecture.



## Failure 2 — Database outage while receiving a webhook

**Exact condition:**
PseudoGram sends a webhook event. Our server receives it (signature verified,
JSON parsed), but the database is unreachable at the moment `db.commit()` is called.

**What happens:**
SQLAlchemy raises an exception. FastAPI returns HTTP 500 to PseudoGram.
The event is NOT persisted. If PseudoGram does not retry, the event is lost.
If PseudoGram does retry (with the same `event_id`), it will succeed once the
DB recovers — the `UNIQUE` constraint on `event_id` prevents double-processing.

**Why it happens:**
We have no local buffer (e.g., disk queue) between the webhook endpoint and the DB.
A direct DB write is simpler but less resilient.

**How to improve:**
Introduce a durable local queue (e.g., a file on disk, Redis, or a message broker)
that buffers events before writing to PostgreSQL. The webhook returns 200 as soon
as the event hits the buffer, not when it hits the DB.

---

## Failure 3 — DM sent for a comment that was deleted before the worker ran

**Exact condition:**
1. `comment.created` webhook arrives and is persisted.
2. `comment.deleted` webhook arrives and is persisted for the same comment.
3. Both events are processed in the wrong order (deleted event processed **after**
   the worker already created a Delivery row for the created event and set it to "sending").

**What happens:**
The DM is delivered. The `comment.deleted` handler only cancels deliveries in
`status="queued"`. A delivery in `status="sending"` is already in-flight.

**Why it happens:**
The worker processes events in insertion order, and there is a race between the
send phase and the deletion phase. If the worker starts sending before it sees
the deletion event, the DM goes through.

**How to improve:**
Before calling `POST /v1/dm/send`, the worker should re-check whether the
underlying comment has been deleted. This requires an additional DB query per
delivery but eliminates the race for most cases.

---

## Failure 4 — PseudoGram stays unavailable longer than the retry window

**Exact condition:**
PseudoGram is down for more than 16 + 8 + 4 + 2 + 1 = 31 seconds of total
backoff time (max 5 attempts). All in-flight deliveries exhaust their retries
and are permanently marked `status="failed"`.

**What happens:**
Stats show `failed = N`. The DMs are never sent, even after PseudoGram recovers.
There is no automatic "re-queue after outage" mechanism.

**Why it happens:**
Once `status="failed"`, the worker ignores those rows. The retry design assumes
transient failures, not extended outages.

**How to improve:**
Add an admin endpoint (`POST /deliveries/{id}/retry`) to manually re-queue failed
deliveries. Or implement a dead-letter queue with a longer-term retry policy
(e.g., try again every hour for 24 hours).

---

## Failure 5 — PseudoGram Official 500-Event Simulator Generates Invalid HMAC Signatures

**Exact condition:**
The official PseudoGram 500-event simulator sends webhook requests, but the `X-Pseudogram-Signature` it provides does not match the true `HMAC-SHA256` of the raw HTTP request body (using the API key supplied to the simulator). This was independently verified by capturing the simulator's raw request and mathematically recomputing the HMAC.

**What happens:**
Our application correctly rejects those requests with HTTP 401 `Invalid webhook signature`, because it strictly enforces security via cryptographic verification.

**Why it happens:**
The mock simulator itself (or its test-harness) has a bug in its serialization or signing logic, resulting in forged/invalid signatures.

**How to improve:**
Because this is an external bug in the official test harness, our application mitigates it perfectly by rejecting the invalid traffic at the edge. We intentionally do not bypass or weaken `verify_signature()` to accommodate the simulator's bug. A local correctly-signing mock was therefore used to validate the application's Part C queue/rate-limit behavior.

---

## What Was Intentionally Not Built

- Frontend (per requirements)
- Authentication on management endpoints (`/rules`, `/stats`)
- Alembic migration files (using `create_all` instead)
- Admin endpoint to manually re-queue failed deliveries
- Webhook delivery retry from PseudoGram's side (we trust their retry policy)
