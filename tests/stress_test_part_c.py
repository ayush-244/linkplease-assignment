import asyncio
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timezone

import httpx
import uvicorn
from fastapi import FastAPI, Request
from pydantic_settings import BaseSettings

# Simple local mock for PseudoGram
mock_app = FastAPI(title="Mock PseudoGram")

class Settings(BaseSettings):
    PSEUDOGRAM_API_KEY: str = "YXl1c2hrdTI0NEBnbWFpbC5jb20.69a7faea7455b7ab596a"
    LINKPLEASE_URL: str = "http://localhost:10000"

settings = Settings()

@mock_app.post("/v1/dm/send")
async def mock_send_dm(request: Request):
    # Simulate PseudoGram accepting the DM
    body = await request.json()
    return {"dm_id": f"mock_dm_{body['comment_id']}"}

@mock_app.get("/v1/dm/{dm_id}")
async def mock_dm_status(dm_id: str):
    # Simulate delivery success after a short delay
    return {"status": "delivered"}


def sign_payload(payload: dict, secret: str) -> str:
    raw = json.dumps(payload).encode("utf-8")
    mac = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256)
    return f"sha256={mac.hexdigest()}"


async def fire_event(client: httpx.AsyncClient, i: int):
    payload = {
        "event_id": f"evt_stress_{i}",
        "event_type": "comment.created",
        "sent_at": datetime.now(timezone.utc).isoformat(),
        "data": {
            "comment_id": f"cmt_stress_{i}",
            "post_id": "post_1",
            "text": "Link please!",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "from": {
                "user_id": f"usr_{i}",
                "username": f"user_{i}",
            },
        },
    }
    signature = sign_payload(payload, settings.PSEUDOGRAM_API_KEY)
    headers = {"X-PseudoGram-Signature": signature}
    
    try:
        r = await client.post(f"{settings.LINKPLEASE_URL}/webhook", json=payload, headers=headers)
        return r.status_code
    except Exception as e:
        print(f"Error firing event {i}: {e}")
        return 500


async def run_stress_test():
    print(f"Starting stress test: 500 events to {settings.LINKPLEASE_URL}")
    print(f"Using API Key: {settings.PSEUDOGRAM_API_KEY}")
    
    # 1. Create the rule
    async with httpx.AsyncClient() as client:
        try:
            r = await client.post(
                f"{settings.LINKPLEASE_URL}/rules",
                json={"keyword": "link", "dm_message": "Here is your link!"}
            )
            print(f"Rule created: {r.status_code}")
        except Exception as e:
            print(f"Could not connect to {settings.LINKPLEASE_URL}. Is Docker running?")
            return

    # 2. Fire 500 events concurrently
    start_time = time.time()
    async with httpx.AsyncClient(timeout=30.0) as client:
        tasks = [fire_event(client, i) for i in range(500)]
        results = await asyncio.gather(*tasks)
    
    elapsed = time.time() - start_time
    success_count = sum(1 for r in results if r == 200)
    print(f"Fired 500 events in {elapsed:.2f} seconds.")
    print(f"Successful 200 OK responses: {success_count}/500")
    
    # 3. Monitor /stats until all are sent
    print("Monitoring /stats...")
    async with httpx.AsyncClient() as client:
        while True:
            r = await client.get(f"{settings.LINKPLEASE_URL}/stats")
            stats = r.json()
            print(f"Stats: {stats}")
            if stats["queued"] == 0 and stats["sent"] >= 500:
                print("Test completed successfully! All 500 DMs processed.")
                break
            await asyncio.sleep(2)


if __name__ == "__main__":
    import threading
    import sys
    
    # Start the mock server in a background thread
    def start_mock():
        uvicorn.run(mock_app, host="0.0.0.0", port=8001, log_level="error")
        
    t = threading.Thread(target=start_mock, daemon=True)
    t.start()
    
    # Give the server a moment to start
    time.sleep(2)
    
    # Run the stress test
    try:
        asyncio.run(run_stress_test())
    except KeyboardInterrupt:
        sys.exit(0)
