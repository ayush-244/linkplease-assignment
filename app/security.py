"""
security.py — Webhook signature verification.

PseudoGram signs every webhook payload with HMAC-SHA256.
The signature is sent in the header:
    X-PseudoGram-Signature: sha256=<hex_digest>

We must verify this BEFORE parsing the JSON body, using the RAW bytes of
the request body so the signature matches exactly what PseudoGram signed.

Why hmac.compare_digest?
  A normal == comparison short-circuits as soon as it finds a mismatch.
  This timing difference can leak information to an attacker (timing attack).
  compare_digest always takes the same time regardless of where the mismatch
  is, making it safe for security comparisons.
"""

import hashlib
import hmac


def verify_signature(raw_body: bytes, signature_header: str, secret: str) -> bool:
    """
    Return True if the HMAC-SHA256 signature in the header is valid.

    Args:
        raw_body:         The unmodified request body as bytes.
        signature_header: Value of the X-PseudoGram-Signature header.
                          Expected format: "sha256=<hex_digest>"
        secret:           The PSEUDOGRAM_API_KEY used as the HMAC secret.

    Returns:
        True if valid, False otherwise.
    """
    # The header value looks like: "sha256=abcdef1234..."
    # We split on "=" to get the algorithm and the hex digest separately.
    parts = signature_header.split("=", 1)
    if len(parts) != 2 or parts[0] != "sha256":
        # Header is malformed — not in "sha256=..." format.
        return False

    received_hex = parts[1]

    # Compute our own HMAC using the shared secret.
    # The secret must be bytes, so we encode the string.
    mac = hmac.new(
        key=secret.encode("utf-8"),
        msg=raw_body,
        digestmod=hashlib.sha256,
    )
    expected_hex = mac.hexdigest()

    # Constant-time comparison to prevent timing attacks.
    return hmac.compare_digest(expected_hex, received_hex)
