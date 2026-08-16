import os
import json
import hmac
import hashlib
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

def load_env():
    """Manually parse .env to get the API key safely without exposing it."""
    if os.path.exists(".env"):
        with open(".env", "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    parts = line.split("=", 1)
                    if len(parts) == 2:
                        os.environ.setdefault(parts[0].strip(), parts[1].strip())

def main():
    load_env()
    api_key = os.getenv("PSEUDOGRAM_API_KEY")
    if not api_key:
        print("FAIL: PSEUDOGRAM_API_KEY is not loaded.")
        return

    print("API Key loaded successfully from environment/file.")
    
    event_id = f"evt_final_{int(time.time())}"
    user_id = f"usr_final_{int(time.time())}"
    
    payload = {
        "event_id": event_id,
        "event_type": "comment.created",
        "sent_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "data": {
            "comment_id": f"cmt_final_{int(time.time())}",
            "post_id": "post_123",
            "text": "PRICE please!",
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "from": {
                "user_id": user_id,
                "username": "final.user"
            }
        }
    }
    
    payload_bytes = json.dumps(payload).encode("utf-8")
    mac = hmac.new(api_key.encode("utf-8"), payload_bytes, hashlib.sha256)
    signature = f"sha256={mac.hexdigest()}"
    
    url = "http://localhost:10000/webhook"
    
    def send_webhook(desc):
        req = urllib.request.Request(url, data=payload_bytes, headers={
            "Content-Type": "application/json",
            "X-PseudoGram-Signature": signature
        })
        print(f"[{desc}] Sending webhook...")
        try:
            with urllib.request.urlopen(req) as response:
                print(f"  -> Response status: {response.status}")
        except urllib.error.HTTPError as e:
            print(f"  -> HTTP Error: {e.code} - {e.read().decode('utf-8')}")

    def check_stats():
        stats_req = urllib.request.Request("http://localhost:10000/stats")
        try:
            with urllib.request.urlopen(stats_req) as response:
                stats = json.loads(response.read().decode('utf-8'))
                print(f"  -> Stats: {stats}")
                return stats
        except urllib.error.HTTPError as e:
            print(f"  -> Stats Error: {e.code}")
            return None

    print("\n--- STEP 1: First Event ---")
    send_webhook("First Event")
    
    print("Waiting 4 seconds for worker to pick up and mock API to respond...")
    time.sleep(4)
    check_stats()
    
    print("\n--- STEP 2: Duplicate Event ID ---")
    send_webhook("Duplicate Event")
    time.sleep(2)
    check_stats()

    print("\n--- STEP 3: Same User, Different Event ID ---")
    payload2 = dict(payload)
    payload2["event_id"] = f"evt_diff_{int(time.time())}"
    payload2_bytes = json.dumps(payload2).encode("utf-8")
    mac2 = hmac.new(api_key.encode("utf-8"), payload2_bytes, hashlib.sha256)
    signature2 = f"sha256={mac2.hexdigest()}"
    
    req2 = urllib.request.Request(url, data=payload2_bytes, headers={
        "Content-Type": "application/json",
        "X-PseudoGram-Signature": signature2
    })
    try:
        with urllib.request.urlopen(req2) as response:
            print(f"  -> Response status (diff event): {response.status}")
    except urllib.error.HTTPError as e:
        print(f"  -> HTTP Error: {e.code}")

    time.sleep(2)
    check_stats()
    
    print("\n--- STEP 4: Wait for Reconciliation ---")
    print("Waiting 15 seconds to allow the Mock API to simulate delivery...")
    for i in range(15):
        time.sleep(1)
        stats = check_stats()
        if stats and stats.get("sent", 0) > 0:
            print("  -> Delivery confirmed!")
            break

if __name__ == "__main__":
    main()
