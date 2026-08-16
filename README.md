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

Implementation: `app/rate_limiter.py` uses a sliding-window deque.  
Before every outgoing API call, the worker calls `rate_limiter.acquire()`.  
If the window is full, it sleeps until a slot opens.

This means 500 webhook events arriving in 10 seconds are all accepted and queued,
but DMs are sent gradually at ≤10 per 60 seconds.

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
- If `"failed"`: marks delivery as `"failed"` → counts in `failed`.
- If still `"queued"`: pushes `next_reconcile_at` forward and checks again later.

**The sender never blocks waiting for confirmation.** It records the `dm_id`,
sets the next check timestamp, and immediately moves on to the next delivery.
This lets the worker maintain throughput while respecting the rate limit.

---

## Deleted Comments

When a `comment.deleted` event arrives:

1. Any pending deliveries (`status="queued"`) for that `comment_id` are cancelled.
2. Deliveries in `"sending"` state are **not** cancelled — the HTTP call already went out.
3. Deliveries with `status="delivered"` are left unchanged — a DM cannot be recalled.

---

## Current Status (Completed Parts: A + B)

This project successfully implements all requirements for Part A (Keyword Automation) and Part B (Webhook Signature Verification & Live Stats).

The application architecture includes:
- **Webhook Security:** Strict `HMAC-SHA256` verification (raw request bytes compared securely against the API key).
- **Background Processing:** An asynchronous background worker decouples immediate webhook receipt (HTTP 200 response) from rate-limited API calls.
- **Duplicate Protection:** Two-layer duplicate prevention via database constraints on `event_id` and `UNIQUE(rule_id, user_id)`.
- **Rate Limiting:** A sliding window `RateLimiter` ensures ≤10 DM requests per 60 seconds.
- **Delivery Reconciliation:** Asynchronous polling loop checks final DM status without blocking the sender worker.

## Load Testing (Simulator Signature Bug)

Part C (500-Event Stress Test) is currently blocked by an upstream bug in the official PseudoGram simulator (`/v1/simulate/start`).

**The Issue:** The simulator generates an `X-Pseudogram-Signature` that does not mathematically match the true `HMAC-SHA256` of its own raw HTTP request body (using the provided API key).

Because our application strictly enforces security requirements, it correctly rejects these invalid/forged webhook requests with `HTTP 401 Unauthorized`. This prevents the 500-event simulation from exercising the background worker. This issue has been forensically verified (by capturing raw simulator traffic) and documented as a known external limitation in [FAILURES.md](FAILURES.md). We intentionally do not bypass HMAC verification to accommodate the simulator's bug.

---

## Known Limitations

See [FAILURES.md](FAILURES.md) for the full list. Key items:

1. **Double-send on crash** — Crash between `POST /v1/dm/send` and saving `dm_id` can cause a retry that sends the DM a second time.
2. **No horizontal scaling** — Multiple workers can race on the same delivery. Use `SELECT FOR UPDATE SKIP LOCKED` to fix.
3. **In-memory rate limiter** — Not shared across instances.
4. **No manual retry endpoint** — Failed deliveries require a DB intervention to re-queue.
