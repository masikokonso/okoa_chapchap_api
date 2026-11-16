
import os
import re
import time
import uuid
import math
import requests

from flask import Flask, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
CORS(app)

# =========================================================
# CONFIGURATION
# =========================================================

PAYHERO_BASE_URL = "https://backend.payhero.co.ke/api/v2"

PAYHERO_AUTH_TOKEN = os.getenv("PAYHERO_AUTH_TOKEN", "")
CHANNEL_ID = int(os.getenv("PAYHERO_CHANNEL_ID", "5471"))

# Your public Render URL, without a trailing slash.
# Example: https://your-app.onrender.com
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")

# Your application's API key
API_KEY = os.getenv("BACKEND_API_KEY", "")

# Firebase Realtime Database
FIREBASE_DB_URL = os.getenv("FIREBASE_DB_URL", "").rstrip("/")
FIREBASE_SECRET = os.getenv("FIREBASE_SECRET", "")

USERS = "aviator_users"
PAYMENTS = "aviator_pay"

# Deposit settings
MIN_DEPOSIT = int(os.getenv("MIN_DEPOSIT", "10"))
BONUS_MIN_DEPOSIT = int(os.getenv("BONUS_MIN_DEPOSIT", "200"))

# Deposit charges
DEP_FEE_FLAT = float(os.getenv("DEP_FEE_FLAT", "0"))
DEP_TAX_PCT = float(os.getenv("DEP_TAX_PCT", "0"))

PAYHERO_HEADERS = {
    "Authorization": PAYHERO_AUTH_TOKEN,
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Accept-Encoding": "identity"
}


# =========================================================
# GENERAL HELPERS
# =========================================================

def fmt_phone(phone):
    """Convert Kenyan phone numbers to 254XXXXXXXXX."""

    phone = str(phone or "").strip()
    phone = phone.replace("+", "").replace(" ", "")
    phone = phone.replace("-", "")

    if phone.startswith("0"):
        phone = "254" + phone[1:]
    elif phone.startswith("7") or phone.startswith("1"):
        phone = "254" + phone

    return phone


def valid_phone(phone):
    return bool(re.fullmatch(r"254[17]\d{8}", phone))


def authorized():
    return (
        bool(API_KEY)
        and request.headers.get("X-API-Key") == API_KEY
    )


def firebase_ready():
    return bool(FIREBASE_DB_URL and FIREBASE_SECRET)


def firebase_url(path):
    return f"{FIREBASE_DB_URL}/{path}.json"


def fb_get(path):
    response = requests.get(
        firebase_url(path),
        params={"auth": FIREBASE_SECRET},
        timeout=15
    )

    response.raise_for_status()
    return response.json()


def fb_put(path, data):
    response = requests.put(
        firebase_url(path),
        params={"auth": FIREBASE_SECRET},
        json=data,
        timeout=15
    )

    response.raise_for_status()
    return response.json()


def fb_txn(path, update_function, retries=8):
    """
    Firebase atomic read-modify-write using ETags.

    update_function(current) returns:
        New value: update Firebase.
        None: abort the update.

    Returns (value, changed).
    """

    for _ in range(retries):

        response = requests.get(
            firebase_url(path),
            params={"auth": FIREBASE_SECRET},
            headers={"X-Firebase-ETag": "true"},
            timeout=15
        )

        response.raise_for_status()

        etag = response.headers.get("ETag")
        current = response.json()

        new_value = update_function(current)

        if new_value is None:
            return current, False

        update_response = requests.put(
            firebase_url(path),
            params={"auth": FIREBASE_SECRET},
            json=new_value,
            headers={"if-match": etag},
            timeout=15
        )

        if update_response.status_code == 200:
            return new_value, True

        if update_response.status_code != 412:
            update_response.raise_for_status()

    raise RuntimeError("Firebase database busy. Please retry.")


def deposit_charge(amount):
    return int(math.ceil(
        DEP_FEE_FLAT + amount * DEP_TAX_PCT / 100
    ))


def update_payment_record(reference, update_function):
    return fb_txn(
        f"{PAYMENTS}/{reference}",
        update_function
    )


# =========================================================
# BALANCE CREDITING
# =========================================================

def credit_user_once(uid, reference, paid_amount):
    """
    Atomically credit the user's balance exactly once.

    The payment reference is also stored in the user's
    payment_credits ledger to prevent duplicate crediting.
    """

    net_amount = max(
        0,
        paid_amount - deposit_charge(paid_amount)
    )

    result = {}

    def update_user(current):

        if not isinstance(current, dict):
            current = {}

        credits = current.get("payment_credits") or {}

        # Payment was already credited.
        if reference in credits:
            result["duplicate"] = True
            result["bonus"] = credits[reference].get("bonus", 0)
            result["balance"] = float(current.get("balance") or 0)
            return None

        first_deposit = not bool(current.get("firstdep"))

        bonus = 0

        if first_deposit and paid_amount >= BONUS_MIN_DEPOSIT:
            bonus = net_amount

        old_balance = float(current.get("balance") or 0)

        new_balance = round(
            old_balance + net_amount + bonus,
            2
        )

        current["balance"] = new_balance
        current["firstdep"] = True

        credits[reference] = {
            "amount": paid_amount,
            "net_amount": net_amount,
            "bonus": bonus,
            "credited_at": int(time.time())
        }

        current["payment_credits"] = credits

        result["duplicate"] = False
        result["bonus"] = bonus
        result["balance"] = new_balance

        return current

    updated_user, changed = fb_txn(
        f"{USERS}/{uid}",
        update_user
    )

    if not changed and not result.get("duplicate"):
        raise RuntimeError("Unable to confirm payment credit.")

    return {
        "credited": changed,
        "bonus": result.get("bonus", 0),
        "balance": result.get(
            "balance",
            float((updated_user or {}).get("balance") or 0)
        )
    }


# =========================================================
# PAYHERO TRANSACTION VERIFICATION
# =========================================================

def verify_and_credit(reference):
    """
    Verify payment directly with PayHero.

    The callback itself is not treated as sufficient proof
    of payment. PayHero transaction status must confirm SUCCESS.
    """

    record = fb_get(f"{PAYMENTS}/{reference}")

    if not record:
        return {
            "state": "FAILED",
            "message": "Unknown payment reference"
        }

    if record.get("credited"):
        return {
            "state": "COMPLETED",
            "credited": False,
            "balance": get_user_balance(record["uid"])
        }

    provider_reference = record.get("payhero_reference")

    if not provider_reference:
        return {
            "state": "PENDING",
            "message": "Waiting for PayHero transaction reference"
        }

    response = requests.get(
        f"{PAYHERO_BASE_URL}/transaction-status",
        headers=PAYHERO_HEADERS,
        params={"reference": provider_reference},
        timeout=20
    )

    response.raise_for_status()

    result = response.json()

    print("PAYHERO VERIFICATION:", result)

    transaction_status = str(
        result.get("status", "")
    ).upper()

    if transaction_status == "FAILED":

        def mark_failed(current):
            if not current or current.get("credited"):
                return None

            current["state"] = "FAILED"
            return current

        update_payment_record(reference, mark_failed)

        return {"state": "FAILED"}

    if transaction_status != "SUCCESS":
        return {"state": "PENDING"}

    # Get the amount confirmed by PayHero.
    try:
        paid_amount = float(
            result.get("amount")
            or record.get("callback_amount")
            or 0
        )
    except (TypeError, ValueError):
        paid_amount = 0

    expected_amount = float(record.get("amount") or 0)

    # Never credit an amount that does not match the request.
    if paid_amount <= 0 or paid_amount != expected_amount:
        print(
            "AMOUNT MISMATCH:",
            "Expected:", expected_amount,
            "Received:", paid_amount
        )

        return {
            "state": "PENDING",
            "message": "Payment amount verification failed"
        }

    uid = record["uid"]

    # Credit the user's Firebase balance exactly once.
    credit_result = credit_user_once(
        uid,
        reference,
        paid_amount
    )

    # Mark payment as credited.
    def mark_credited(current):

        if not current:
            return None

        current["credited"] = True
        current["state"] = "COMPLETED"
        current["paid"] = paid_amount
        current["bonus"] = credit_result["bonus"]
        current["receipt"] = (
            result.get("provider_reference")
            or current.get("receipt")
        )
        current["completed_at"] = int(time.time())

        return current

    update_payment_record(reference, mark_credited)

    return {
        "state": "COMPLETED",
        "amount": paid_amount,
        "charge": deposit_charge(paid_amount),
        "bonus": credit_result["bonus"],
        "credited": credit_result["credited"],
        "balance": credit_result["balance"]
    }


def get_user_balance(uid):
    user = fb_get(f"{USERS}/{uid}")

    if not isinstance(user, dict):
        return 0

    return float(user.get("balance") or 0)


# =========================================================
# HOME / HEALTH CHECK
# =========================================================

@app.route("/", methods=["GET"])
def home():

    return jsonify({
        "status": "success",
        "message": "PayHero STK Push API is running",
        "channel_id": CHANNEL_ID,
        "firebase_configured": firebase_ready(),
        "payhero_configured": bool(PAYHERO_AUTH_TOKEN),
        "callback_configured": bool(PUBLIC_BASE_URL),
        "endpoints": [
            "/api/payment/initiate",
            "/api/payment/status/<reference>",
            "/payhero/callback",
            "/api/balance"
        ]
    })


# =========================================================
# INITIATE PAYMENT
# =========================================================

@app.route("/api/payment/initiate", methods=["POST"])
def initiate_payment():

    if not authorized():
        return jsonify({
            "status": "error",
            "message": "Unauthorized"
        }), 401

    if not firebase_ready():
        return jsonify({
            "status": "error",
            "message": "Firebase is not configured"
        }), 500

    if not PAYHERO_AUTH_TOKEN:
        return jsonify({
            "status": "error",
            "message": "PayHero credentials are missing"
        }), 500

    if not PUBLIC_BASE_URL:
        return jsonify({
            "status": "error",
            "message": "PUBLIC_BASE_URL is not configured"
        }), 500

    data = request.get_json(force=True, silent=True) or {}

    phone = fmt_phone(data.get("phone"))
    uid = fmt_phone(data.get("uid"))

    description = str(
        data.get("description", "Payment")
    ).strip()

    try:
        amount = int(float(data.get("amount", 0)))
    except (TypeError, ValueError):
        amount = 0

    if not valid_phone(phone):
        return jsonify({
            "status": "error",
            "message": "Invalid M-Pesa phone number"
        }), 400

    if not valid_phone(uid):
        return jsonify({
            "status": "error",
            "message": "A valid account uid is required"
        }), 400

    if amount < MIN_DEPOSIT:
        return jsonify({
            "status": "error",
            "message": f"Minimum deposit is KES {MIN_DEPOSIT}"
        }), 400

    # Unique reference for every payment.
    reference = "AV-" + uuid.uuid4().hex[:24].upper()

    callback_url = PUBLIC_BASE_URL + "/payhero/callback"

    # Save the pending payment BEFORE sending the STK request.
    # This prevents losing a callback that arrives very quickly.
    payment_record = {
        "uid": uid,
        "phone": phone,
        "amount": amount,
        "description": description,
        "credited": False,
        "state": "PENDING",
        "external_reference": reference,
        "ts": int(time.time())
    }

    try:
        fb_put(
            f"{PAYMENTS}/{reference}",
            payment_record
        )

    except Exception as e:
        print("FIREBASE SAVE ERROR:", str(e))

        return jsonify({
            "status": "error",
            "message": "Could not save payment record"
        }), 502

    payload = {
        "amount": amount,
        "phone_number": phone,
        "channel_id": CHANNEL_ID,
        "provider": "m-pesa",
        "external_reference": reference,
        "callback_url": callback_url
    }

    try:

        response = requests.post(
            f"{PAYHERO_BASE_URL}/payments",
            headers=PAYHERO_HEADERS,
            json=payload,
            timeout=30
        )

        print("PAYHERO RESPONSE:", response.status_code, response.text)

        try:
            result = response.json()
        except ValueError:
            result = {}

    except requests.RequestException as e:

        print("PAYHERO REQUEST ERROR:", str(e))

        return jsonify({
            "status": "error",
            "message": "Could not contact PayHero",
            "reference": reference
        }), 502

    provider_reference = result.get("reference")

    if response.status_code not in (200, 201) or not provider_reference:

        def mark_initiation_failed(current):
            if not current:
                return None

            current["state"] = "INITIATION_FAILED"
            current["error"] = result.get("message", "STK request failed")

            return current

        update_payment_record(
            reference,
            mark_initiation_failed
        )

        return jsonify({
            "status": "error",
            "message": (
                result.get("error_message")
                or result.get("message")
                or "STK Push failed"
            )
        }), 400

    # Save the PayHero reference and checkout ID.
    def save_provider_details(current):

        if not current:
            return None

        current["payhero_reference"] = provider_reference
        current["checkout_request_id"] = (
            result.get("CheckoutRequestID")
            or result.get("checkout_request_id")
        )
        current["state"] = "PENDING"

        return current

    update_payment_record(
        reference,
        save_provider_details
    )

    # If the callback arrived before PayHero returned its reference,
    # it was saved in Firebase. Process it now.
    try:
        latest = fb_get(f"{PAYMENTS}/{reference}")

        if latest and latest.get("callback_received"):
            verify_and_credit(reference)

    except Exception as e:
        print("EARLY CALLBACK PROCESSING:", str(e))

    return jsonify({
        "status": "success",
        "message": "STK Push sent successfully",
        "reference": reference,
        "payhero_reference": provider_reference,
        "checkout_request_id": (
            result.get("CheckoutRequestID")
            or result.get("checkout_request_id")
        ),
        "amount": amount
    }), 200


# =========================================================
# PAYHERO AUTOMATIC CALLBACK
# =========================================================

@app.route("/payhero/callback", methods=["POST"])
def payhero_callback():

    data = request.get_json(force=True, silent=True) or {}

    print("PAYHERO CALLBACK RECEIVED:", data)

    callback = data.get("response") or {}

    # PayHero sends the merchant's external reference here.
    reference = str(
        callback.get("ExternalReference", "")
    ).strip()

    if not reference:
        return jsonify({
            "success": False,
            "message": "Missing external reference"
        }), 400

    if not firebase_ready():
        return jsonify({
            "success": False,
            "message": "Firebase unavailable"
        }), 500

    try:
        record = fb_get(f"{PAYMENTS}/{reference}")

    except Exception as e:
        print("CALLBACK FIREBASE ERROR:", str(e))

        return jsonify({
            "success": False,
            "message": "Could not retrieve payment"
        }), 500

    if not record:
        return jsonify({
            "success": False,
            "message": "Unknown payment reference"
        }), 404

    # A failed M-Pesa request must not be credited.
    result_code = callback.get("ResultCode")

    if result_code is not None and str(result_code) != "0":

        def mark_callback_failed(current):

            if not current or current.get("credited"):
                return None

            current["state"] = "FAILED"
            current["callback_received"] = True
            current["callback_result"] = callback

            return current

        update_payment_record(
            reference,
            mark_callback_failed
        )

        return jsonify({
            "success": True,
            "message": "Failed payment recorded"
        }), 200

    # Save callback information.
    try:
        callback_amount = float(callback.get("Amount") or 0)
    except (TypeError, ValueError):
        callback_amount = 0

    def save_callback(current):

        if not current:
            return None

        current["callback_received"] = True
        current["callback_amount"] = callback_amount
        current["receipt"] = callback.get("MpesaReceiptNumber")
        current["callback_phone"] = callback.get("Phone")
        current["callback_status"] = callback.get("Status")

        return current

    try:
        update_payment_record(
            reference,
            save_callback
        )

    except Exception as e:
        print("CALLBACK SAVE ERROR:", str(e))

        return jsonify({
            "success": False,
            "message": "Could not save callback"
        }), 500

    # The callback may arrive before the STK request response
    # has been saved. In that case, process it after /pay saves it.
    if not record.get("payhero_reference"):

        return jsonify({
            "success": True,
            "message": "Callback received; waiting for payment reference"
        }), 200

    try:

        verification = verify_and_credit(reference)

        print("CALLBACK VERIFICATION RESULT:", verification)

        return jsonify({
            "success": True,
            "message": "Callback processed",
            "payment_state": verification.get("state")
        }), 200

    except Exception as e:

        print("CALLBACK PROCESSING ERROR:", str(e))

        # Return a server error so the callback can be retried.
        return jsonify({
            "success": False,
            "message": "Payment verification could not be completed"
        }), 500


# =========================================================
# CHECK PAYMENT STATUS
# =========================================================

@app.route("/api/payment/status/<reference>", methods=["GET"])
def payment_status(reference):

    if not authorized():
        return jsonify({
            "state": "PENDING",
            "message": "Unauthorized"
        }), 401

    if not firebase_ready():
        return jsonify({
            "state": "PENDING",
            "message": "Firebase is not configured"
        }), 500

    try:

        result = verify_and_credit(reference)

        return jsonify(result), 200

    except requests.RequestException as e:

        print("STATUS NETWORK ERROR:", str(e))

        return jsonify({
            "state": "PENDING",
            "message": "Could not verify payment with PayHero"
        }), 502

    except Exception as e:

        print("STATUS ERROR:", str(e))

        return jsonify({
            "state": "PENDING",
            "message": "Payment verification error"
        }), 500


# =========================================================
# GET USER BALANCE
# =========================================================

@app.route("/api/balance", methods=["POST"])
def get_balance():

    if not authorized():
        return jsonify({
            "success": False,
            "message": "Unauthorized"
        }), 401

    if not firebase_ready():
        return jsonify({
            "success": False,
            "message": "Firebase is not configured"
        }), 500

    data = request.get_json(force=True, silent=True) or {}

    uid = fmt_phone(data.get("uid"))

    if not valid_phone(uid):
        return jsonify({
            "success": False,
            "message": "Invalid account uid"
        }), 400

    try:

        balance = get_user_balance(uid)

        return jsonify({
            "success": True,
            "balance": balance
        })

    except Exception as e:

        print("BALANCE ERROR:", str(e))

        return jsonify({
            "success": False,
            "message": "Could not retrieve balance"
        }), 502


# =========================================================
# RUN SERVER
# =========================================================

if __name__ == "__main__":

    print("=" * 50)
    print("PAYHERO PAYMENT SERVER")
    print("=" * 50)

    print("Channel ID:", CHANNEL_ID)
    print("PayHero configured:", bool(PAYHERO_AUTH_TOKEN))
    print("Firebase configured:", firebase_ready())
    print("API key configured:", bool(API_KEY))
    print("Callback configured:", bool(PUBLIC_BASE_URL))

    print("=" * 50)

    port = int(os.environ.get("PORT", "5000"))

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False
    )
