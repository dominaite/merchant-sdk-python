# dominaite-python

Server-side Python client for the Dominaite merchant API. One call from your backend opens a
hosted checkout session; a two-line script tag renders the payment widget on your page. Card
details go straight from your customer's browser into the payment widget - they never touch
your server, which keeps your PCI scope minimal (SAQ A).

Python 3.9+, standard library only. No `requests`, no framework, nothing to vendor.

## Install

The package name is `dominaite` on PyPI (verified free 2026-08-17; matches `import dominaite`,
the same pattern Stripe uses). It is **not published yet** - until it is, install from a checkout:

```bash
pip install /path/to/dominaite-python-sdk
# or, while you are working on the SDK itself:
pip install -e /path/to/dominaite-python-sdk
```

## Credentials

You get two values from the Dominaite dashboard, under **Online payments -> Website
integration**, when you create an API key. The secret is shown **once** - store both like
passwords:

- `dmk_...` - your API key id. Identifies you; not secret by itself.
- `dms_...` - your API secret. Server-side only: environment variable or a config file outside
  the web root. Never in a browser, never in git, never in logs.

Every request is signed with the secret (HMAC-SHA256) and timestamped. Keep your server clock
on NTP - signatures older than 5 minutes are rejected.

## Quickstart against dev

Everything you need to go from nothing to a live session on the dev environment.

**1. Set your credentials.** Both come from the dashboard's Website-integration tab (dev
dashboard, dev key - a prod key will not authenticate against dev):

```bash
export DOMINAITE_KEY_ID='dmk_...'          # the key id shown on the tab
export DOMINAITE_SECRET='dms_...'          # the secret shown once at key creation
export DOMINAITE_BASE_URL='https://func-dom-gw-payments-dev-gwc-01.azurewebsites.net/api'
```

That base URL is the dev payments service. Production is
`https://api.dominaite.com/payments`, which is the SDK's default when you pass no `base_url`.

**2. Check your signing before you call anything.** This runs offline against the published
test vector and authenticates nothing, so it can never fail for credential reasons:

```bash
python -m pytest tests/test_signing.py
```

**3. Ping before your first mint.** One signed GET that creates nothing, so a failure here
is your credentials, your signing or your clock and nothing else:

```python
import os

from dominaite import DominaiteClient

client = DominaiteClient(
    os.environ["DOMINAITE_KEY_ID"],
    os.environ["DOMINAITE_SECRET"],
    base_url=os.environ.get("DOMINAITE_BASE_URL", "https://api.dominaite.com/payments"),
)

print(client.ping())
# {'pong': True, 'merchantId': '...', 'serverTime': '...', 'clockSkewSeconds': 0}
```

Watch `clockSkewSeconds`: requests start failing once it passes 300, so a drifting number
is your warning to fix NTP before payments break.

**4. Mint a session** (`mint.py`):

```python
import os

from dominaite import CheckoutRefusedError, DominaiteClient, TransportError

client = DominaiteClient(
    os.environ["DOMINAITE_KEY_ID"],
    os.environ["DOMINAITE_SECRET"],
    base_url=os.environ.get("DOMINAITE_BASE_URL", "https://api.dominaite.com/payments"),
)

try:
    session = client.create_checkout_session(
        amount=2500,                    # minor units: 2500 = 25.00 EUR
        currency="EUR",
        order_reference="order-1042",   # your own order id, shows up in your dashboard
        customer={
            # Pass everything you already know - prefilled fields are hidden from the
            # payer, so the checkout form stays short.
            "firstName": "Ana",
            "lastName": "Kirova",
            "email": "ana@example.com",
        },
        language="bg",                  # widget UI language
        theme="dark",
    )
except CheckoutRefusedError as refusal:
    # Machine-readable: refusal.error_code - see the exception docstring for the codes.
    raise SystemExit("Payment unavailable: " + refusal.error_code)
except TransportError:
    # Network blip - safe to retry with the same idempotency_key.
    raise SystemExit("Payment temporarily unavailable")

print(session["transactionId"], session["cashierKey"], session["cashierToken"])
```

```bash
python mint.py
```

A transaction id, cashier key and cashier token on stdout means the whole chain works: your
credentials, your clock, your signing, and the dev gateway.

**If it fails**, the error tells you which one:

| What you see | What is wrong |
|---|---|
| `AuthenticationError` + `INVALID_API_KEY` | Wrong or revoked key id, or a prod key against dev. |
| `AuthenticationError` + `INVALID_SIGNATURE` | Secret does not match the key id. |
| `AuthenticationError` + `TIMESTAMP_OUT_OF_RANGE` | Your machine's clock is more than 5 minutes off. |
| `AuthenticationError` + `IP_NOT_ALLOWED` | The key has an IP allowlist that does not include you. |
| `CheckoutRefusedError` | You authenticated fine; the gateway declined to open a session. |
| `TransportError` | Wrong base URL, or the service is down. Retry with the same key. |

**5. Render the widget.** Store `session["transactionId"]` against your order, then hand the
two cashier values to the page:

```html
<div id="checkout"></div>
<script src="https://bp-checkout.dominaite.com/v2/launcher"
        data-cashier-key="{{ cashier_key }}"
        data-cashier-token="{{ cashier_token }}"></script>
```

HTML-escape both when templating (Jinja's autoescape does it for you). They are per-payment
session values, not your credentials.

That's the whole integration: the session call, the script tag, and your domain bound to your
checkout by Dominaite during onboarding.

## Amounts are minor units

`amount` is always an integer in the currency's minor unit: `2500` is 25.00 EUR. A float or a
string raises `ValueError` before anything is sent. The amount is locked server-side - what you
pass here is what gets charged; nothing in the browser can change it.

## Retries and double-charges

Every `create_checkout_session` call carries an idempotency key (auto-generated, or pass your
own as `idempotency_key`). Retrying with the same key never opens a second payment - on a
timeout, retry with the same key rather than generating a new one.

There is a helper that does exactly that:

```python
session = client.create_checkout_session_with_retry(
    amount=2500,
    currency="EUR",
    order_reference="order-1042",
    max_attempts=3,
)
```

It retries only `TransportError` (network failures, 5xx, `MERCHANT_API_UNAVAILABLE`), reuses the
one key across all attempts, and backs off between them. Refusals and authentication failures
are raised immediately.

## Sessions expire

A session is valid for 2 hours. If the payer comes back later, create a new session.

## Status polling

```python
status = client.get_status(session["transactionId"])
# {"transactionId": ..., "orderReference": "order-1042", "status": "succeeded",
#  "amount": 2500, "currency": "EUR", ...}
```

`status` is one of: `pending`, `processing`, `succeeded`, `failed`, `refunded`,
`partially_refunded`, `cancelled`, `disputed`, `requires_capture`, `abandoned`. While the
session is still payable the response also carries `expiresAt`; after that instant a `pending`
session can only become `abandoned`. An unknown transaction id raises `ApiError` with
`http_status == 404`.

`succeeded` is the only value that means the payment is complete. Keep polling on `pending`,
`processing` and `requires_capture` - none of them is terminal.

`requires_capture` is **not** "unpaid": the payer has already paid and the funds are held
awaiting capture. Never treat it as an abandoned order.

Treat any status you do not recognise as still-open as well: a value the API adds later should
make you keep polling, never silently close an order that is still live.

Poll after the payer returns to you, or on your order timeout - not in a tight loop; the
endpoint is rate limited per key.

### Recovering from a replay refusal

When your idempotency key collides with an earlier attempt, the refusal names the transaction
it collided with, so you can reconcile instead of minting a second payment:

```python
try:
    session = client.create_checkout_session(...)
except CheckoutRefusedError as refusal:
    if refusal.transaction_id:
        status = client.get_status(refusal.transaction_id)
        # Now you know what the earlier attempt actually did.
```

`refusal.transaction_id` is `None` when the API did not name one (a concurrent-race
`DUPLICATE_REQUEST` knows the key is taken but not yet by which row), so check it before use.
The full refusal payload is on `refusal.result`.

## Errors

| Exception | Means | Retry? |
|---|---|---|
| `AuthenticationError` | Bad credentials, bad signature, clock skew, IP not allowlisted | No - fix config |
| `CheckoutRefusedError` | The gateway refused to open the session (`error_code`) | Depends on the code |
| `ApiError` | Unexpected response, or a 4xx like an unknown transaction id (`http_status`) | No |
| `TransportError` | Network failure or 5xx; you don't know if it landed | Yes, same idempotency key |

All four inherit from `DominaiteError` if you only care that the call failed.

## Running the tests

```bash
python -m venv .venv && .venv/bin/pip install pytest
.venv/bin/python -m pytest
```

`tests/test_signing.py` reproduces the signing test vector published on the dashboard's
Website-integration tab. If it ever fails, the SDK cannot authenticate - fix the signing, never
the expected value.
