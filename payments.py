# payments.py
# Handles all Razorpay payment logic for Signalwatch Pro.
#
# Sits next to app.py — Python finds it automatically via "from payments import ..."
# One responsibility: create orders, verify payments, check Pro status.

import hmac        # Cryptographic message authentication — used to verify Razorpay signatures
import hashlib     # SHA256 hashing algorithm — used inside the signature check
import os          # Read environment variables from Render
import razorpay    # Official Razorpay Python SDK — pip install razorpay

# ── CREDENTIALS ───────────────────────────────────────────────────────────────
# Set these in Render → your service → Environment:
#   RAZORPAY_KEY_ID     = rzp_live_xxxxxxxxxx   (from Razorpay dashboard → Settings → API Keys)
#   RAZORPAY_KEY_SECRET = your_secret_here
#
# During testing use rzp_test_ keys — no real money moves.
RAZORPAY_KEY_ID     = os.getenv("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "")

# ── PLAN DETAILS ──────────────────────────────────────────────────────────────
# Razorpay always works in the smallest currency unit.
# For INR: 1 rupee = 100 paise. ₹1,900 = 190000 paise.
# We use INR as the base currency. International customers
# will be handled via a second provider (tomorrow's task).
PLAN_AMOUNT_PAISE = 190000   # ₹1,900/month — approximately $19 at current rates
PLAN_CURRENCY     = "INR"
PLAN_NAME         = "Signalwatch Pro"
PLAN_DESCRIPTION  = "Unlimited searches · All 4 agents · Lead intelligence · No daily cap"


def get_razorpay_client():
    """
    Creates and returns an authenticated Razorpay client.
    Called once per request — lightweight, no persistent connection needed.
    auth= takes a tuple of (key_id, key_secret) — Razorpay uses HTTP Basic Auth.
    """
    return razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))


def create_razorpay_order(token: str) -> dict:
    """
    Creates a Razorpay order server-side and returns the details the
    frontend needs to open the checkout popup.

    Why server-side:
    If the browser created orders, anyone could tamper with the amount.
    The server sets the price — the browser just pays it.

    Returns a dict with order_id, amount, currency, key_id.
    Returns {"error": "..."} if anything fails.
    """
    if not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET:
        return {"error": "Payments not configured. Contact support@signalwatch.in"}

    try:
        client = get_razorpay_client()

        order = client.order.create({
            "amount":   PLAN_AMOUNT_PAISE,      # Amount in paise
            "currency": PLAN_CURRENCY,          # "INR"
            "receipt":  f"sw_{token[:12]}",     # Your internal reference — short token prefix
            "notes": {
                # Store the full token in Razorpay's notes so you can look it up later
                # if needed for manual support queries
                "token": token,
                "plan":  PLAN_NAME
            }
        })

        return {
            "order_id":    order["id"],          # e.g. "order_ABCxyz123"
            "amount":      PLAN_AMOUNT_PAISE,
            "currency":    PLAN_CURRENCY,
            "key_id":      RAZORPAY_KEY_ID,      # Frontend needs this to init checkout
            "description": PLAN_DESCRIPTION
        }

    except Exception as e:
        print(f"create_razorpay_order error: {e}")
        return {"error": "Could not create payment order. Please try again."}


def verify_razorpay_signature(order_id: str, payment_id: str, signature: str) -> bool:
    """
    Verifies the payment signature Razorpay sends after a successful payment.

    Why this matters:
    Without this, anyone could send a fake POST to /verify-payment and get Pro for free.
    The signature is a cryptographic proof that Razorpay generated — only someone
    with your secret key can produce it.

    How it works:
    Razorpay signs "order_id|payment_id" using your secret key via HMAC-SHA256.
    We compute the same signature ourselves and compare.
    If they match, the payment is genuine.
    """
    if not RAZORPAY_KEY_SECRET:
        return False

    # Build the exact string Razorpay signed
    message = f"{order_id}|{payment_id}"

    # Compute our own HMAC-SHA256 signature
    # hmac.new(key, message, algorithm) — all arguments must be bytes, not strings
    expected_signature = hmac.new(
        RAZORPAY_KEY_SECRET.encode("utf-8"),  # Secret key as bytes
        message.encode("utf-8"),              # Message as bytes
        hashlib.sha256                        # The hashing algorithm
    ).hexdigest()   # Convert binary hash to lowercase hex string

    # hmac.compare_digest is timing-safe — prevents timing attacks
    # (a normal == comparison leaks information via response time differences)
    return hmac.compare_digest(expected_signature, signature)


def mark_token_as_pro(token: str) -> bool:
    """
    Writes the token into the pro_users table after payment is verified.
    From this point on, is_pro(token) returns True and limits are skipped.

    Uses INSERT ... ON CONFLICT DO UPDATE so it is safe to call multiple times
    (e.g. if Razorpay sends a webhook twice — idempotent means no duplicate rows).
    """
    # Import here to avoid circular imports — database.py does not exist yet
    # so we reuse the get_db pattern from app.py directly
    try:
        import psycopg2
        database_url = os.getenv("DATABASE_URL", "")
        conn = psycopg2.connect(database_url, connect_timeout=5)
        cur = conn.cursor()

        # Create the table if it does not exist yet
        # This means you do not need a separate migration step
        cur.execute("""
            CREATE TABLE IF NOT EXISTS pro_users (
                token        TEXT PRIMARY KEY,
                activated_at TIMESTAMPTZ DEFAULT NOW(),
                plan         TEXT DEFAULT 'pro_monthly'
            )
        """)

        # Insert the token — if already exists, update the timestamp
        cur.execute("""
            INSERT INTO pro_users (token, activated_at, plan)
            VALUES (%s, NOW(), 'pro_monthly')
            ON CONFLICT (token) DO UPDATE SET activated_at = NOW()
        """, (token,))

        conn.commit()
        cur.close()
        conn.close()
        print(f"Pro activated: {token[:12]}...")
        return True

    except Exception as e:
        print(f"mark_token_as_pro error: {e}")
        return False


def is_pro(token: str) -> bool:
    """
    Fast lookup: is this token in the pro_users table?
    Called on every search request before the rate limit check.
    Returns True = unlimited searches. Returns False = apply daily limit.
    """
    try:
        import psycopg2
        database_url = os.getenv("DATABASE_URL", "")
        conn = psycopg2.connect(database_url, connect_timeout=5)
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM pro_users WHERE token = %s", (token,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        return row is not None   # True if a row exists, False if not

    except Exception as e:
        print(f"is_pro check error: {e}")
        return False  # Fail closed — if DB is down, treat as free user