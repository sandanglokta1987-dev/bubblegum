"""
BubbleGum — Activation Key Generator
KEEP THIS FILE PRIVATE. Never distribute to clients.

Usage:
    python keygen.py A1B2-C3D4-E5F6-G7H8
"""

import hashlib
import hmac
import sys

# ── Shared secret — must match bubblegum_app.py ──────────────────────────────
SECRET = "BubbleGum-S3cr3t-K3y-X9q7ZmP2vL8"


def generate_key(machine_id):
    """HMAC-SHA256 of machine_id → 20-char hex → groups of 5 dashes."""
    h = hmac.new(SECRET.encode(), machine_id.encode(), hashlib.sha256).hexdigest()[:20].upper()
    return f"{h[:5]}-{h[5:10]}-{h[10:15]}-{h[15:20]}"


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python keygen.py <MACHINE-ID>")
        print("Example: python keygen.py A1B2-C3D4-E5F6-G7H8")
        sys.exit(1)

    mid = sys.argv[1].strip().upper()
    key = generate_key(mid)

    print()
    print(f"  Machine ID:      {mid}")
    print(f"  Activation Key:  {key}")
    print()
