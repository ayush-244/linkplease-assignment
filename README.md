# LinkPlease — Part A

Keyword-triggered DM automation via PseudoGram.

When someone comments on a PseudoGram post, this service automatically
sends them a DM if their comment contains a configured keyword.

---

## Architecture

```
PseudoGram
    ↓
POST /webhook
    ↓
HMAC-SHA256 verification (raw bytes, before JSON parsing)
    ↓
PostgreSQL (events table — UNIQUE event_id for idempotency)
    ↓
Return HTTP 200 immediately
    ↓
Background Worker (asyncio.Task)
    ↓
Phase 1: Recover stuck deliveries (crash safety)
    ↓
Phase 2: Match events → create Delivery rows
         (UNIQUE(rule_id, user_id) prevents duplicate DMs)
    ↓
Phase 3: Rate-limited sender (≤10 req / 60s sliding window)
         → POST /v1/dm/send → save dm_id → status = "sending"
    ↓
Phase 4: Reconciliation loop (non-blocking)
         → GET /v1/dm/{dm_id} → "delivered" or "failed"
    ↓
/stats (all counts from DB — no in-memory counters)
```

---

## Database Schema

### `rules`
| Column | Type | Notes |
|--------|------|-------|
| `id` | UUID | Primary key |
| `keyword` | VARCHAR | Trigger phrase |
| `dm_message` | TEXT | Message to send |
| `created_at` | TIMESTAMPTZ | |

### `events`
| Column | Type | Notes |
|--------|------|-------|
| `id` | UUID | Primary key |
| `event_id` | VARCHAR | **UNIQUE** — PseudoGram's ID, prevents double-processing |
| `event_type` | VARCHAR | `comment.created` or `comment.deleted` |
| `comment_id` | VARCHAR | |
| `user_id` | VARCHAR | Recipient identity (never username) |
| `text` | TEXT | Comment content |
| `processed` | BOOLEAN | False = worker hasn't handled it yet |
| `deleted` | BOOLEAN | True if a comment.deleted event arrived |

### `deliveries`
| Column | Type | Notes |
|--------|------|-------|
| `id` | UUID | Primary key |
| `rule_id` | UUID FK | Which rule matched |
| `user_id` | VARCHAR | Recipient |
| `comment_id` | VARCHAR | Which comment triggered it |
| `dm_id` | VARCHAR | PseudoGram's DM ID (null until sent) |
| `status` | ENUM | `queued` / `sending` / `delivered` / `failed` / `cancelled` |
| `attempts` | INT | Send attempt count |
| `next_retry_at` | TIMESTAMPTZ | When to retry (null = ready now) |
| `next_reconcile_at` | TIMESTAMPTZ | When to check DM status (null = not sent yet) |
| **UNIQUE** | | `(rule_id, user_id)` — DB-level duplicate prevention |

### `duplicate_blocks`
| Column | Type | Notes |
|--------|------|-------|
| `id` | UUID | |
| `rule_id` | VARCHAR | |
| `user_id` | VARCHAR | |
| `created_at` | TIMESTAMPTZ | |

One row = one blocked duplicate attempt. Counted by `GET /stats`.

---

## API Endpoints

### `POST /rules`
Create a keyword trigger rule.

**Request**
```json
{ "keyword": "PRICE", "dm_message": "Here's the price list..." }
```
**Response (201)**
```json
{ "rule_id": "...", "keyword": "PRICE", "dm_message": "..." }
```

---

### `POST /webhook`
Receive a PseudoGram webhook event.

**Required header**
```
X-PseudoGram-Signature: sha256=<hex>
```

Supports `event_type`:
- `comment.created` — triggers rule matching
- `comment.deleted` — cancels pending DMs for that comment

**Response (200)**
```json
{ "status": "received" }
```

Returns `401` if the signature is missing or invalid.

---

### `GET /stats`
```json
{
  "sent": 142,
  "failed": 3,
  "queued": 8,
  "duplicates_blocked": 57
}
```

All numbers come from database queries — accurate across restarts.

---

### `GET /health`
```json
{ "status": "ok" }
```

---

## Local Setup (Python)

```bash
# 1. Create and activate a virtual environment
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS/Linux

# 2. Install dependencies
pip install -r requirements.txt

# 3. Copy and configure environment variables
cp .env.example .env
# Edit .env: set DATABASE_URL and PSEUDOGRAM_API_KEY

# 4. Start the server
uvicorn app.main:app --host 0.0.0.0 --port 10000 --reload
```

---

## Docker Setup

```bash
# 1. Set your API key in the environment (or a .env file)
export PSEUDOGRAM_API_KEY=your_real_key

# 2. Build and start everything (PostgreSQL + API)
docker-compose up --build

# The API is available at http://localhost:10000
```

PostgreSQL persists data in a named Docker volume (`postgres_data`).

---

## Running Tests

Tests use an **in-memory SQLite database** — no PostgreSQL required.

```bash
# Run all tests
pytest tests/ -v

# Run a specific file
pytest tests/test_webhook.py -v
```

---

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `DATABASE_URL` | ✅ | — | `postgresql+asyncpg://user:pass@host:port/db` |
| `PSEUDOGRAM_API_KEY` | ✅ | — | API key + HMAC webhook secret |
| `PSEUDOGRAM_BASE_URL` | ✗ | `https://pseudogram-api.onrender.com` | PseudoGram API base URL |
| `WORKER_POLL_INTERVAL` | ✗ | `1.0` | Seconds between worker iterations |
| `DM_RECONCILE_INTERVAL` | ✗ | `2.0` | Seconds between DM status polls |

---

## Render Deployment

1. Create a **Web Service** pointing at this repository.
2. Set the start command:
   ```
   uvicorn app.main:app --host 0.0.0.0 --port 10000
   ```
3. Set environment variables in Render's dashboard:
   - `DATABASE_URL` — from your Render PostgreSQL instance
   - `PSEUDOGRAM_API_KEY` — your PseudoGram key
4. Create a **PostgreSQL** database on Render and copy the internal URL.
5. Set your webhook URL in PseudoGram to:
   ```
   https://YOUR-APP.onrender.com/webhook
   ```

---

## Retry Strategy

| PseudoGram Response | Action |
|--------------------|--------|
| 202 Accepted | Save `dm_id`, poll for final status |
| 500 Server Error | Retry with backoff: 1s → 2s → 4s → 8s → 16s (max 5 attempts) |
| 429 Rate Limited | Wait `Retry-After` seconds, then retry (doesn't count as an attempt) |
| 400 Bad Request | Permanent failure — no retry |

All retry timestamps (`next_retry_at`) are stored in PostgreSQL.  
**A restart never loses queued retry jobs.**

---

## Rate Limiting

PseudoGram allows **10 requests per rolling 60 seconds**.

Implementation: `app/rate_limiter.py` uses a **PostgreSQL-backed sliding window**.
It maintains exactly 10 token rows in the database. Before every outgoing API call,
the worker acquires the oldest token using `SELECT FOR UPDATE SKIP LOCKED` (or a regular lock in SQLite tests).
If all tokens were used in the last 60 seconds, the rate limit is reached.
Instead of blocking the worker or holding a transaction open, the worker calculates when the token will be available, sets `next_retry_at`, and cleanly exits the send phase.

This means 500 webhook events arriving in 10 seconds are all accepted and queued,
but DMs are sent gradually at ≤10 per 60 seconds. The rate limiter handles horizontal scaling perfectly.

**Note:** The reconciliation calls (`GET /v1/dm/{dm_id}`) do **not** count against
the rate limit, per the assignment specification.

---

## Duplicate Prevention

**Two layers:**

1. **DB-level UNIQUE constraint** — `UNIQUE(rule_id, user_id)` on the `deliveries` table.  
   PostgreSQL rejects a second insert for the same (rule, user) pair with an `IntegrityError`.  
   This works even if two requests arrive simultaneously.

2. **`event_id` deduplication** — `UNIQUE(event_id)` on the `events` table.  
   PseudoGram can resend the same event; we silently ignore duplicates.

**Idempotency Key:**
Retries safely use an `Idempotency-Key` header (`rule_id-user_id`) to ensure that PseudoGram never creates duplicate DMs even if a crash occurs between the API call and our database commit.

---

## Webhook Security

All incoming webhooks are verified before any processing:

1. The **raw request bytes** are read first (before JSON parsing).
2. The `X-PseudoGram-Signature: sha256=<hex>` header is checked.
3. We compute `HMAC-SHA256(raw_body, PSEUDOGRAM_API_KEY)` and compare
   using `hmac.compare_digest()` (constant-time, prevents timing attacks).
4. A missing, malformed, or invalid signature → HTTP 401.
5. JSON parsing only happens after the signature is verified.

---

## Delivery Reconciliation

After a successful `POST /v1/dm/send` (202 response):

1. The `dm_id` is saved to the database.
2. The delivery status becomes `"sending"`.
3. `next_reconcile_at` is set to `now + 2s`.

The reconciliation loop (Phase 4 of the worker) runs every iteration:
- Fetches all deliveries with `status="sending"` and `next_reconcile_at <= now`.
- Calls `GET /v1/dm/{dm_id}` for each.
- If `"delivered"`: marks delivery as `"delivered"` → counts in `sent`.
- If `"failed"`: schedules a retry with exponential backoff + jitter.
- If still `"queued"`: pushes `next_reconcile_at` forward and checks again later.

**The sender never blocks waiting for confirmation.** It records the `dm_id`,
sets the next check timestamp, and immediately moves on to the next delivery.
This lets the worker maintain throughput while respecting the rate limit.

---

## Deleted Comments

When a `comment.deleted` event arrives:

1. If it arrives **before** the worker creates the delivery, the creation is ignored.
2. Any pending deliveries (`status="queued"`) for that `comment_id` are cancelled.
3. Deliveries in `"sending"` state are **not** cancelled — the HTTP call already went out.
4. Deliveries with `status="delivered"` are left unchanged — a DM cannot be recalled.

---

## Current Status (Completed Parts: A+B+C)

This project successfully implements all requirements for Part A (Keyword Automation), Part B (Webhook Signature Verification & Live Stats), and Part C (Delivery Reconciliation, Rate Limiting & Stress Testing).

### Part C Capabilities
The application architecture is fully equipped for Part C with the following features:
- **Durable delivery reconciliation:** Polling the `/v1/dm/{dm_id}` endpoint ensures deliveries are accurately tracked.
- **Failed DM retries with exponential backoff and jitter:** Handles transient API failures gracefully.
- **Configurable `MAX_RETRIES`:** Prevents infinite retry loops.
- **Deterministic `Idempotency-Key`:** Ensures that worker crashes during the send phase do not result in duplicate DMs upon recovery.
- **PostgreSQL-backed durable rate limiting:** Coordinates across multiple worker instances safely.
- **Maximum 10 `/v1/dm/send` requests per rolling 60 seconds:** Strictly adheres to PseudoGram's rate limits.
- **Atomic delivery claiming:** Uses atomic SQL `UPDATE`s to prevent concurrent workers from processing the same queued delivery.
- **Out-of-order `comment.deleted` handling:** Safely drops deliveries even if the deletion event arrives before the creation event.
- **Durable queued deliveries:** All states are tracked in PostgreSQL, ensuring no tasks are kept only in memory.
- **Crash recovery:** If the app restarts, it safely resumes processing queued deliveries and retrying failed ones.
- **500-event local stress test:** A custom stress test validates the system against a correctly signing local PseudoGram mock.

### Important Tradeoff
The system prioritizes **durable correctness and rate-limit safety** over completing all 500 outbound DMs immediately. Incoming webhooks are accepted quickly and persisted, while outbound processing is intentionally throttled to the platform's 10 requests/60 seconds limit.

### Part C Validation
Because the official PseudoGram 500-event simulator currently generates invalid HMAC signatures (see [FAILURES.md](FAILURES.md)), we validated Part C using a custom local load test that correctly signs payloads. This test demonstrated:
- **49 pytest tests passing** covering concurrency, retries, idempotency, and state recovery.
- **Local 500-event stress test:** 500 valid signed webhook requests were fired concurrently. All were accepted with HTTP 200, durably queued, and outbound DMs were correctly rate-limited without any webhook loss.
- **Docker build/start successful:** The application runs cleanly via `docker compose up -d` in an isolated network environment.

---

## Known Limitations

See [FAILURES.md](FAILURES.md) for the full list. Key items:

1. **Database outage while receiving a webhook** — We don't use an external broker (e.g. Redis/RabbitMQ) for initial event queuing.
2. **No manual retry endpoint** — Failed deliveries require a DB intervention to re-queue.
3. **Official Simulator Bug** — The official 500-event test sends invalid signatures, so it cannot currently exercise the system.
