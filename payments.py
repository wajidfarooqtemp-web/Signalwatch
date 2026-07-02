# payments.py
# All payment logic for Signalwatch Pro.
# Handles Razorpay orders, PayPal subscription verification,
# promo code generation and redemption, and Pro status checks.
#
# Imported by app.py using: from payments import ...
# Sits in the same folder as app.py — Python finds it automatically.

import os
import hmac
import hashlib
import secrets     # Python built-in — generates cryptographically secure random strings
import string      # Python built-in — gives us character sets for code generation
import razorpay    # pip install razorpay — add to requirements.txt
import requests    # Already in your requirements.txt — used for PayPal API calls
from datetime import datetime, timedelta
import json

# ── CREDENTIALS ───────────────────────────────────────────────────────────────
# Set all of these in Render → your service → Environment.
# Never hardcode them here.

RAZORPAY_KEY_ID       = os.getenv("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET   = os.getenv("RAZORPAY_KEY_SECRET", "")

# ── WEBHOOK VERIFICATION ──────────────────────────────────────────────────
# Razorpay calls your server automatically after a successful payment.
# This function checks the request really came from Razorpay and was not
# faked by someone else hitting your URL directly.

RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")

def verify_webhook_signature(raw_body: bytes, signature: str) -> bool:
    """
    Recomputes the expected signature using your webhook secret and
    compares it to the one Razorpay sent. If they match, it's genuine.
    """
    if not RAZORPAY_WEBHOOK_SECRET:
        return False
    expected = hmac.new(
        RAZORPAY_WEBHOOK_SECRET.encode("utf-8"),
        raw_body,
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)

# PayPal credentials — get from PayPal Developer Dashboard → My Apps
# App Client ID and Secret are different from what you put in the frontend.
# The frontend uses Client ID only (public).
# The backend uses Client ID + Secret to verify payments server-side.
PAYPAL_CLIENT_ID      = os.getenv("PAYPAL_CLIENT_ID", "")
PAYPAL_CLIENT_SECRET  = os.getenv("PAYPAL_CLIENT_SECRET", "")

# PayPal runs two environments — sandbox for testing, live for real payments.
# Set PAYPAL_ENV=sandbox in Render while testing, then change to live.
PAYPAL_ENV            = os.getenv("PAYPAL_ENV", "live")
PAYPAL_BASE           = (
    "https://api-m.sandbox.paypal.com"
    if PAYPAL_ENV == "sandbox"
    else "https://api-m.paypal.com"
)

# Secret salt for promo code generation — set this in Render environment.
# Any random string works. Example: openssl rand -hex 32
# This makes codes impossible to guess without knowing this value.
PROMO_SECRET          = os.getenv("PROMO_SECRET", "change-this-to-a-random-string")

# Admin password — protects your /admin/generate-code endpoint.
# Set ADMIN_SECRET in Render environment. Pick something strong.
ADMIN_SECRET          = os.getenv("ADMIN_SECRET", "")

# Pro plan limits
PRO_MONTHLY_LIMIT     = 1000   # Searches per calendar month
PRO_MONTHLY_LEAD_LIMIT = 250   # Lead scans per calendar month for Pro users
PLAN_AMOUNT_PAISE     = 190000  # ₹1,900 — approximately $19
PLAN_CURRENCY         = "INR"
PLAN_DESCRIPTION      = "Know what to do next · All 4 specialised agents · Competitor intelligence · Lead intelligence"


# ── DATABASE HELPER ───────────────────────────────────────────────────────────
# Reuses your existing DATABASE_URL — no new connection setup needed.

def _get_conn():
    """Opens a database connection. Returns None if unavailable."""
    try:
        import psycopg2
        return psycopg2.connect(os.getenv("DATABASE_URL", ""), connect_timeout=5)
    except Exception as e:
        print(f"DB connection error: {e}")
        return None


def setup_payment_tables():
    """
    Creates the three tables needed for payments.
    Called once when app.py starts — safe to call repeatedly
    because of IF NOT EXISTS.

    Tables:
    - pro_users:     tokens that have active Pro access
    - pro_searches:  monthly search counter per Pro token
    - promo_codes:   generated codes and their redemption status
    """
    conn = _get_conn()
    if not conn:
        print("Payments: DB unavailable — tables not created")
        return
    try:
        cur = conn.cursor()

        # Stores every token that has paid or redeemed a promo code.
        # expires_at is NULL for real subscriptions (managed by PayPal/Razorpay)
        # and set to 30 days from now for promo code redemptions.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS pro_users (
                token        TEXT PRIMARY KEY,
                activated_at TIMESTAMPTZ DEFAULT NOW(),
                expires_at   TIMESTAMPTZ,
                plan         TEXT DEFAULT 'pro_monthly',
                payment_ref  TEXT
            )
        """)

        # Add missing columns if they do not exist yet.
        # This handles the case where pro_users was created by an older version
        # of the code that did not have these columns.
        # ALTER TABLE ... ADD COLUMN IF NOT EXISTS is safe to run repeatedly.
        for column_sql in [
            "ALTER TABLE pro_users ADD COLUMN IF NOT EXISTS expires_at  TIMESTAMPTZ",
            "ALTER TABLE pro_users ADD COLUMN IF NOT EXISTS plan        TEXT DEFAULT 'pro_monthly'",
            "ALTER TABLE pro_users ADD COLUMN IF NOT EXISTS payment_ref TEXT",
        ]:
            try:
                cur.execute(column_sql)
            except Exception:
                pass  # Column already exists — safe to ignore

        # Monthly search counter for Pro users.
        # month_key is a string like "2026-06" — resets automatically
        # because we insert a new row each month.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS pro_searches (
                token      TEXT NOT NULL,
                month_key  TEXT NOT NULL,
                count      INTEGER DEFAULT 0,
                PRIMARY KEY (token, month_key)
            )
        """)

        # Promo codes you generate and share with customers.
        # Each code is single-use and gives one month of Pro access.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS promo_codes (
                code         TEXT PRIMARY KEY,
                created_at   TIMESTAMPTZ DEFAULT NOW(),
                redeemed_at  TIMESTAMPTZ,
                redeemed_by  TEXT,
                is_used      BOOLEAN DEFAULT FALSE,
                note         TEXT
            )
        """)

        conn.commit()
        cur.close()
        conn.close()
        print("Payment tables ready")
    except Exception as e:
        print(f"setup_payment_tables error: {e}")


# ── PRO STATUS ────────────────────────────────────────────────────────────────

def is_pro(token: str) -> bool:
    """
    Checks if a token has active Pro access.

    For subscription users: checks the pro_users table.
    For promo code users: also checks that expires_at has not passed.

    Returns True = Pro access granted.
    Returns False = treat as free user.
    """
    conn = _get_conn()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        # Check for active Pro — either no expiry (real subscription)
        # or expiry in the future (promo code)
        cur.execute("""
            SELECT 1 FROM pro_users
            WHERE token = %s
              AND (expires_at IS NULL OR expires_at > NOW())
        """, (token,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        return row is not None
    except Exception as e:
        print(f"is_pro error: {e}")
        return False


def mark_token_as_pro(token: str, payment_ref: str = "", expires_at=None) -> bool:
    """
    Writes a token into pro_users after a verified payment.

    payment_ref: Razorpay payment ID or PayPal subscription ID — stored for
                 your records so you can look up any token's payment history.
    expires_at:  None for real subscriptions (PayPal/Razorpay manage renewal).
                 Set to 30 days from now for promo code redemptions.
    """
    conn = _get_conn()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO pro_users (token, activated_at, expires_at, payment_ref)
            VALUES (%s, NOW(), %s, %s)
            ON CONFLICT (token) DO UPDATE
                SET activated_at = NOW(),
                    expires_at   = EXCLUDED.expires_at,
                    payment_ref  = EXCLUDED.payment_ref
        """, (token, expires_at, payment_ref))
        conn.commit()
        cur.close()
        conn.close()
        print(f"Pro activated: {token[:12]}... ref={payment_ref}")
        return True
    except Exception as e:
        print(f"mark_token_as_pro error: {e}")
        return False


# ── MONTHLY SEARCH COUNTER FOR PRO USERS ─────────────────────────────────────

def get_pro_search_count(token: str) -> int:
    """
    Returns how many searches this Pro token has done in the current calendar month.
    month_key example: "2026-06"
    """
    month_key = datetime.now().strftime("%Y-%m")
    conn = _get_conn()
    if not conn:
        return 0
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT count FROM pro_searches
            WHERE token = %s AND month_key = %s
        """, (token, month_key))
        row = cur.fetchone()
        cur.close()
        conn.close()
        return row[0] if row else 0
    except Exception as e:
        print(f"get_pro_search_count error: {e}")
        return 0


def increment_pro_search_count(token: str) -> int:
    """
    Adds 1 to this token's monthly search count and returns the new total.
    Creates a new row automatically at the start of each month.
    """
    month_key = datetime.now().strftime("%Y-%m")
    conn = _get_conn()
    if not conn:
        return 1
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO pro_searches (token, month_key, count)
            VALUES (%s, %s, 1)
            ON CONFLICT (token, month_key)
            DO UPDATE SET count = pro_searches.count + 1
            RETURNING count
        """, (token, month_key))
        count = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()
        return count
    except Exception as e:
        print(f"increment_pro_search_count error: {e}")
        return 1
    

def get_pro_lead_count(token: str) -> int:
    """
    Returns how many lead scans this Pro token has used this calendar month.
    Reuses the pro_searches table with a different month_key prefix so we
    don't need a new table.
    """
    month_key = "leads-" + datetime.now().strftime("%Y-%m")
    conn = _get_conn()
    if not conn:
        return 0
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT count FROM pro_searches
            WHERE token = %s AND month_key = %s
        """, (token, month_key))
        row = cur.fetchone()
        cur.close()
        conn.close()
        return row[0] if row else 0
    except Exception as e:
        print(f"get_pro_lead_count error: {e}")
        return 0


def increment_pro_lead_count(token: str) -> int:
    """
    Adds 1 to this Pro token's monthly lead scan count.
    """
    month_key = "leads-" + datetime.now().strftime("%Y-%m")
    conn = _get_conn()
    if not conn:
        return 1
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO pro_searches (token, month_key, count)
            VALUES (%s, %s, 1)
            ON CONFLICT (token, month_key)
            DO UPDATE SET count = pro_searches.count + 1
            RETURNING count
        """, (token, month_key))
        count = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()
        return count
    except Exception as e:
        print(f"increment_pro_lead_count error: {e}")
        return 1    


# ── RAZORPAY ──────────────────────────────────────────────────────────────────

def create_razorpay_order(token: str) -> dict:
    """
    Creates a one-time Razorpay order server-side.
    The frontend uses the returned order_id to open the Checkout.js popup.
    The token is attached as a note — the webhook reads it later to know
    who to activate. Price is set here — the browser cannot modify it.
    """
    if not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET:
        return {"error": "Razorpay not configured"}
    try:
        client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))
        order = client.order.create({
            "amount":   PLAN_AMOUNT_PAISE,
            "currency": PLAN_CURRENCY,
            "receipt":  f"sw_{token[:12]}",
            "notes":    {"token": token}
        })
        return {
            "order_id":    order["id"],
            "amount":      PLAN_AMOUNT_PAISE,
            "currency":    PLAN_CURRENCY,
            "key_id":      RAZORPAY_KEY_ID,
            "description": PLAN_DESCRIPTION
        }
    except Exception as e:
        print(f"create_razorpay_order error: {e}")
        return {"error": "Could not create order"}


# ── PAYPAL ────────────────────────────────────────────────────────────────────

def get_paypal_access_token() -> str:
    """
    Gets a short-lived OAuth access token from PayPal.
    Required for all PayPal API calls.
    PayPal uses Client ID + Secret to issue this token — server-side only.
    Token expires in ~9 hours but we fetch a fresh one per verification.
    """
    res = requests.post(
        f"{PAYPAL_BASE}/v1/oauth2/token",
        auth=(PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET),
        data={"grant_type": "client_credentials"},
        timeout=10
    )
    res.raise_for_status()
    return res.json()["access_token"]

def create_paypal_order(token: str) -> dict:
    """
    Creates a one-time PayPal order for $19 USD.
    PayPal calls this "intent: CAPTURE" — meaning we take payment
    immediately once the buyer approves, not a recurring subscription.

    custom_id stores our token so we know whose account to upgrade
    when the webhook fires later.
    """
    try:
        access_token = get_paypal_access_token()
        res = requests.post(
            f"{PAYPAL_BASE}/v2/checkout/orders",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type":  "application/json"
            },
            json={
                "intent": "CAPTURE",
                "purchase_units": [{
                    "amount": {
                        "currency_code": "USD",
                        "value": "19.00"
                    },
                    "custom_id": token,
                    "description": PLAN_DESCRIPTION
                }]
            },
            timeout=10
        )
        if res.status_code not in (200, 201):
            print(f"PayPal create order failed: {res.status_code} {res.text}")
            return {"error": "Could not create PayPal order"}

        data = res.json()
        return {"order_id": data.get("id", "")}

    except Exception as e:
        print(f"create_paypal_order error: {e}")
        return {"error": "Could not create PayPal order"}


def capture_paypal_order(order_id: str) -> bool:
    """
    Step two of PayPal's flow. Creating an order does NOT take the money.
    After the buyer approves in the popup, we must explicitly "capture"
    the order — this is the moment the actual charge happens.

    Returns True if capture succeeded.
    """
    try:
        access_token = get_paypal_access_token()
        res = requests.post(
            f"{PAYPAL_BASE}/v2/checkout/orders/{order_id}/capture",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type":  "application/json"
            },
            timeout=10
        )
        if res.status_code not in (200, 201):
            print(f"PayPal capture failed: {res.status_code} {res.text}")
            return False
        return True

    except Exception as e:
        print(f"capture_paypal_order error: {e}")
        return False


PAYPAL_WEBHOOK_ID = os.getenv("PAYPAL_WEBHOOK_ID", "")

def verify_paypal_webhook(headers: dict, body: bytes) -> bool:
    """
    PayPal does NOT use a simple secret + HMAC like Razorpay.
    Instead, you send PayPal back the exact headers it sent you,
    plus your Webhook ID, and PayPal's own API tells you whether
    the webhook was genuinely sent by them.

    This is PayPal's official verification method — no shortcuts.
    """
    if not PAYPAL_WEBHOOK_ID:
        return False
    try:
        access_token = get_paypal_access_token()
        verification_payload = {
            "transmission_id":   headers.get("paypal-transmission-id", ""),
            "transmission_time": headers.get("paypal-transmission-time", ""),
            "cert_url":          headers.get("paypal-cert-url", ""),
            "auth_algo":         headers.get("paypal-auth-algo", ""),
            "transmission_sig":  headers.get("paypal-transmission-sig", ""),
            "webhook_id":        PAYPAL_WEBHOOK_ID,
            "webhook_event":     json.loads(body)
        }
        res = requests.post(
            f"{PAYPAL_BASE}/v1/notifications/verify-webhook-signature",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type":  "application/json"
            },
            json=verification_payload,
            timeout=10
        )
        if res.status_code != 200:
            print(f"PayPal webhook verify request failed: {res.status_code}")
            return False

        result = res.json()
        return result.get("verification_status") == "SUCCESS"

    except Exception as e:
        print(f"verify_paypal_webhook error: {e}")
        return False


# ── PROMO CODES ───────────────────────────────────────────────────────────────

def generate_promo_code(note: str = "") -> str:
    """
    Generates a new promo code and stores it in the database.
    Called only from your private /admin/generate-code endpoint.

    Format: SW-XXXX-XXXX where X is an uppercase letter or digit.
    Example: SW-K7M2-P9QR

    note: optional internal note — e.g. "for customer John who lost access"
          Stored in DB for your records, never shown to customers.

    Returns the code string, or empty string if DB write fails.
    """
    # Generate 8 random uppercase alphanumeric characters
    # secrets.choice is cryptographically secure — cannot be predicted
    alphabet = string.ascii_uppercase + string.digits
    part1    = "".join(secrets.choice(alphabet) for _ in range(4))
    part2    = "".join(secrets.choice(alphabet) for _ in range(4))
    code     = f"SW-{part1}-{part2}"

    conn = _get_conn()
    if not conn:
        return ""
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO promo_codes (code, note)
            VALUES (%s, %s)
        """, (code, note))
        conn.commit()
        cur.close()
        conn.close()
        print(f"Promo code generated: {code} note={note}")
        return code
    except Exception as e:
        print(f"generate_promo_code error: {e}")
        return ""


def redeem_promo_code(code: str, token: str) -> dict:
    """
    Validates and redeems a promo code for a user token.

    Checks:
    1. Code exists in the database
    2. Code has not already been used
    3. Marks it as used atomically — prevents two people redeeming simultaneously

    On success: grants one month of Pro access to the token.
    Returns {"success": True} or {"success": False, "error": "..."}
    """
    # Normalise — uppercase, strip whitespace
    # So "sw-k7m2-p9qr" and "SW-K7M2-P9QR" both work
    code = code.strip().upper()

    conn = _get_conn()
    if not conn:
        return {"success": False, "error": "Service temporarily unavailable"}
    try:
        cur = conn.cursor()

        # Fetch the code record
        cur.execute("""
            SELECT is_used FROM promo_codes WHERE code = %s
        """, (code,))
        row = cur.fetchone()

        if not row:
            # Code does not exist — do not reveal this specifically
            # Just say invalid so people cannot probe for valid codes
            cur.close()
            conn.close()
            return {"success": False, "error": "Invalid code"}

        if row[0]:
            # Code already used
            cur.close()
            conn.close()
            return {"success": False, "error": "This code has already been used"}

        # Mark as used atomically
        cur.execute("""
            UPDATE promo_codes
            SET is_used     = TRUE,
                redeemed_at = NOW(),
                redeemed_by = %s
            WHERE code = %s AND is_used = FALSE
        """, (token, code))

        if cur.rowcount == 0:
            # Race condition — someone else redeemed it in the same instant
            cur.close()
            conn.close()
            return {"success": False, "error": "This code has already been used"}

        conn.commit()
        cur.close()
        conn.close()

        # Grant one month of Pro access
        # expires_at = exactly 30 days from now
        expires_at = datetime.now() + timedelta(days=30)
        activated  = mark_token_as_pro(token, payment_ref=f"promo:{code}", expires_at=expires_at)

        if activated:
            return {"success": True, "message": "Pro access activated for 30 days"}
        else:
            return {"success": False, "error": "Could not activate — contact support@signalwatch.in"}

    except Exception as e:
        print(f"redeem_promo_code error: {e}")
        return {"success": False, "error": "Service error — please try again"}