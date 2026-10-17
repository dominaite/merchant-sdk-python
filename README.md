# dominaite-python

Server-side Python client for the Dominaite merchant API. One call from your backend opens a
hosted checkout session; a two-line script tag renders the payment widget on your page. Card
details go straight from your customer's browser into the payment widget - they never touch
your server, which keeps your PCI scope minimal (SAQ A).

Python 3.9+, standard library only. No `requests`, no framework, nothing to vendor.

## Install

```bash
pip install dominaite
```

The package name on PyPI is `dominaite`, the same as `import dominaite`. To work on the SDK
itself, install it from a checkout:

```bash
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

The scopes of a key decide which methods it can call:

| Scope | Methods |
|---|---|
| `payments` | Sessions, `get_status`, charges, revokes, refunds, plans and agreements |
| `transactions:read` | `list_transactions` |
| `webhooks:manage` | `create_webhook_endpoint`, `list_webhook_endpoints`, `delete_webhook_endpoint` |

A key with `payments` holds no other scope, so the other two scopes need a second key: the
reporting key. If your dashboard has no reporting key section, ask Dominaite support for the key.
The reporting key is for your whole account. We cannot bind it to one storefront.
A key issued before scopes existed holds `payments`. Every key can call `ping()`. A method that
the key's scopes do not cover raises a 403 `AuthenticationError` with `error_code`
`INSUFFICIENT_SCOPE`. A payments key always gets HTTP 403 `INSUFFICIENT_SCOPE` on
`list_transactions()` and on the webhook endpoint methods.

## What we switch on for your account

Some features work only after we switch them on for your account, so ask Dominaite support.
The dashboard has no switch for the first four.

- **Stored-card charges.** Without the switch, `charge_payment_method()` raises a 503
  `ChargeError` with `PAYMENT_METHOD_CHARGES_DISABLED`.
- **Subscription sessions.** Without the switch, `create_checkout_session()` with
  `agreement_id` raises `CheckoutRefusedError` with `SUBSCRIPTIONS_NOT_ENABLED`.
- **Scheduled charges.** Without the switch, the platform runs no scheduled charge and no
  retry, even when the subscription session works.
- **Verify-only sessions.** Without the switch, a session with `verify_only=True` raises a 403
  `AuthenticationError` with `VERIFY_ONLY_NOT_ENABLED`.
- **Card fields.** We switch them on per merchant on request. See [Card fields](#card-fields).
- **The reporting key.** This is a second key, not a switch. A payments key always gets HTTP
  403 `INSUFFICIENT_SCOPE` on `list_transactions()` and on the webhook endpoint methods.

## Quickstart against dev

Everything you need to go from nothing to a live session on the dev environment.

**1. Set your credentials.** Both come from the dashboard's Website-integration tab (dev
dashboard, dev key - a prod key will not authenticate against dev):

```bash
export DOMINAITE_KEY_ID='dmk_...'          # the key id shown on the tab
export DOMINAITE_SECRET='dms_...'          # the secret shown once at key creation
export DOMINAITE_BASE_URL='https://api.staging.dominaite.com/payments'   # the sandbox
```

`base_url` is required. The SDK has no default, so a missing, empty or whitespace-only value
raises `ValueError` at construction instead of calling production with the wrong key. Use
`https://api.staging.dominaite.com/payments` for the sandbox and
`https://api.dominaite.com/payments` for production. The package exports both as
`SANDBOX_BASE_URL` and `PRODUCTION_BASE_URL`. `DEFAULT_BASE_URL` is removed in 0.6.0. Use
`PRODUCTION_BASE_URL`.

`base_url` has to be `https://`. Every request carries your key id and a signature, and over
plain http anyone on the path can read them and replay the request inside the server's 5 minute
window, so the constructor raises `ValueError` rather than let that happen. The one exception is
loopback - `localhost`, `127.0.0.1` and `::1` may use `http://`, so a local mock server works.

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
    base_url=os.environ.get("DOMINAITE_BASE_URL"),   # required, see step 1
)

print(client.ping())
# {'pong': True, 'merchantId': '...', 'serverTime': '...', 'clockSkewSeconds': 0}
```

Watch `clockSkewSeconds`: requests start failing once it passes 300, so a drifting number
is your warning to fix NTP before payments break.

**4. Mint a session** (`mint.py`):

```python
import os

from dominaite import (
    CheckoutRefusedError,
    DominaiteClient,
    TransportError,
    order_idempotency_key,
)

client = DominaiteClient(
    os.environ["DOMINAITE_KEY_ID"],
    os.environ["DOMINAITE_SECRET"],
    base_url=os.environ.get("DOMINAITE_BASE_URL"),   # required, see step 1
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
            # Billing address, all optional. The checkout asks for these only when the
            # payment processor needs them, and never for one you send.
            "address": "12 Vitosha Blvd",  # one line, up to 200 characters
            "city": "Sofia",               # up to 100 characters
            "postalCode": "1000",          # up to 20 characters
            # "state": "CA",               # up to 50; only for the US, Canada and Australia
        },
        language="bg",                  # widget UI language
        theme="dark",
        # Required. Same order + same amount = same key, so a reload or a retry gets
        # the session the customer already has instead of a second one.
        idempotency_key=order_idempotency_key("checkout", "1042", 2500, "EUR"),
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
| `AuthenticationError` + `INSUFFICIENT_SCOPE` (`http_status` 403) | The key and the signature are good, but the key does not hold the `payments` scope. Use a payments key. |
| `CheckoutRefusedError` | You authenticated fine; the gateway declined to open a session. |
| `StorefrontError` + `STOREFRONT_NOT_WHITELISTED` | Your website's domain is not approved with the payment provider yet. See [Storefront errors](#storefront-errors). |
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

**6. Learn the outcome from a webhook.** The payer finishing on the widget is not your
signal that you got paid - your backend does not see that at all. Point an endpoint at your
server, verify the signature, and act on `payment.succeeded`. See [Webhooks](#webhooks) below;
that is the step that closes the loop, not `get_status` in a loop.

That's the whole integration: the session call, the script tag, the webhook, and your domain
bound to your checkout by Dominaite during onboarding.

## Amounts are minor units

`amount` is always an integer in the currency's minor unit: `2500` is 25.00 EUR. A float or a
string raises `ValueError` before anything is sent. The amount is locked server-side - what you
pass here is what gets charged; nothing in the browser can change it.

Not every currency has two decimals. The minor unit is set by the gateway's currency registry,
which mostly matches ISO 4217 but not always:

| Decimals | Currencies | `2500` means |
|---|---|---|
| 2 | EUR, USD, GBP, CAD, AUD, CHF, BGN, RON, PLN, CZK, SEK, DKK, NOK | 25.00 |
| 0 | JPY, HUF | 2500 |
| 3 | BHD, KWD | 2.500 |

**HUF is whole forints.** ISO 4217 gives HUF two decimals, the gateway uses none: `2500` HUF is
2500 Ft. Sending `250000` for 2500 Ft would charge 100 times too much.

ISK, KRW, OMR, JOD and TND are not supported: ISO and the gateway disagree on their decimals, so
`to_minor_units` raises for them rather than produce an amount that is off by 10x or 100x.

`to_minor_units` converts a price for you, without float math:

```python
from decimal import Decimal

from dominaite import to_minor_units

to_minor_units("25.00", "EUR")            # 2500
to_minor_units("2500", "JPY")             # 2500
to_minor_units("2500", "HUF")             # 2500, whole forints
to_minor_units("2.5", "BHD")              # 2500
to_minor_units(Decimal("0.30"), "EUR")    # 30, where 0.1 + 0.2 as floats is 0.30000000000000004
```

It takes a decimal string or a `Decimal`, never a float. It rounds nothing: more decimal places
than the currency has (`"25.001"` EUR, `"100.5"` JPY, and also `"25.000"` EUR) raises
`ValueError`, so quantize values from a wider column first. A currency it does not know raises
too, instead of assuming two decimals; the known ones are in `CURRENCY_EXPONENTS` and the
refused ones in `UNSUPPORTED_CURRENCIES`.

## Retries and double-charges

Every `create_checkout_session` call needs an `idempotency_key`. There is no random default:
leaving it out raises `ValueError` before anything is sent. Retrying with the same key never
charges twice - on a timeout, retry with the same key rather than generating a new one.

Build the key from the order with `order_idempotency_key`:

```python
from dominaite import order_idempotency_key

key = order_idempotency_key("checkout", order.id, order.total_minor, order.currency)
# "checkout-1042-2500-EUR"
```

The key is `{scope}-{order_id}-{amount_minor}-{CURRENCY}`. That shape is the point:

- **Same order, same amount, same key.** A reload, a back button or a retry after a timeout
  rebuilds the identical key, so the gateway hands back the session the customer already has
  (or tells you the order is paid) instead of opening a second one.
- **Changed amount or currency, new key.** A coupon or an edited basket produces a different
  key and a fresh session. Reusing the old key with a new amount would be refused with
  `IDEMPOTENCY_KEY_REUSED`.
- **Scope** keeps different flows on the same order apart (`"checkout"`, `"deposit"`, a
  billing period for recurring charges).

The helper applies the same rules as any key (see [Field lengths](#field-lengths)) and raises
`ValueError` on a bad part rather than building a key the API would refuse.

`charge_payment_method` needs a key the same way; derive it from the billing period.

If the first attempt did land and its session is still open, the retry returns that same
session: same transaction id, same cashier values. Otherwise it comes back as a
`CheckoutRefusedError` with a replay code (`ALREADY_PROCESSED` once it is paid,
`PRIOR_ATTEMPT_FAILED`, `DUPLICATE_REQUEST`, or `IDEMPOTENCY_KEY_REUSED` for a different
amount). Use `refusal.transaction_id` with `get_status()` to find out what the first attempt did
(see [Recovering from a replay refusal](#recovering-from-a-replay-refusal)).

### The client retries for you

The client retries a call that may not have landed. It never retries a call that got an answer.

- **What it retries:** every GET, and every POST that carries an idempotency key (create a
  checkout session, charge, refund, and create a plan or an agreement when you pass a key). A PATCH, a DELETE and a
  POST without a key are sent once.
- **When it retries:** a network failure, a 5xx, and `PAYMENT_PROCESSING_UNAVAILABLE` (card
  payments briefly off, nothing charged), as a 503 or as a refusal.
- **When it does not:** a 4xx, a 429, every other refusal, and the two charge answers that are
  final: `CHARGE_OUTCOME_UNKNOWN` and `CHARGE_FAILED`. Those carry a charge row; see
  [Stored payment methods](#stored-payment-methods-recurring).
- **Same key, same body.** Every attempt sends your one idempotency key and the same body. Only
  the timestamp and the signature over it change.
- **How long it waits:** full jitter. Before retry `n` (from 0) it waits a random time between 0
  and `min(8, 0.5 * 2**n)` seconds.

`max_retries` sets the number of retries after the first attempt. The default is 2, so a call
is sent at most three times. `max_retries=0` sends every call once:

```python
client = DominaiteClient(key_id, secret, base_url, max_retries=0)
```

Each retry goes to `on_warning` (see [Logging and warnings](#logging-and-warnings)), for example
`dominaite-python: retrying POST /merchant-api/checkout/sessions after HTTP 503 (attempt 2 of 3)`.

If processing stays unavailable past the last attempt you get the `CheckoutRefusedError` or the
`TransportError`. Retry later with the same key, and give up after about fifteen minutes.

`create_checkout_session_with_retry` is deprecated. It still works: it runs on the same retry
loop with its own `max_attempts` and `backoff_seconds`, and warns once per call. Call
`create_checkout_session` instead.

## Sessions expire

A session is valid for 2 hours. If the payer comes back later, re-POST with the same
idempotency key: once the session is a few minutes past expiry, that returns a fresh session
for the same order (see
[Recovering from a replay refusal](#recovering-from-a-replay-refusal)).

## Billing address

`customer` takes four optional billing fields next to the name, email and phone:

| Field | Limit | Notes |
|---|---|---|
| `address` | 200 characters | Street address, one line. |
| `city` | 100 characters | |
| `postalCode` | 20 characters | |
| `state` | 50 characters | ISO 3166-2 subdivision code, for example `CA` or `NSW`. Only used for the United States, Canada and Australia. |

The checkout asks the payer for address, city and postal code only when the payment processor
needs them. It does not ask for a field that the session already carries, so send what your own
checkout already collected. The SDK sends the fields as given and does not check the limits. The
API refuses a value over its limit with a 400, which arrives as `ApiError` with `error_code`
`VALIDATION_ERROR`. `error.result["error"]["validationErrors"]` names the field, for example
`{"field": "customer.postalCode", "code": "MAX_LENGTH"}`. Do not retry: fix the value first.

## Card fields

Instead of the hosted widget, you can render card fields inside your own checkout page. Card
fields are enabled per merchant on request: ask Dominaite support to switch them on. Until then a
session with `integration="fields"` is rejected with a 400 (`INVALID_SELECTION` on
`integration`).

Pass `integration="fields"` (or `CheckoutIntegration.FIELDS`) when you create the session. Leave
it out (or pass `"widget"`) for the hosted widget. It is part of the idempotency identity, so a
replay of the same key with a different `integration` is refused with `IDEMPOTENCY_KEY_REUSED`.

```python
from dominaite import CheckoutIntegration

session = client.create_checkout_session(
    amount=8440,
    currency="EUR",
    order_reference="order-1042",
    integration=CheckoutIntegration.FIELDS,
    idempotency_key=order_idempotency_key("checkout", "1042", 8440, "EUR"),
)
# session["integration"] == "fields", session["clientSecret"] is set
```

Hand `transactionId`, `integration`, `cashierKey`, `cashierToken` and `clientSecret` to the
payer's page, load the drop-in and mount it:

```html
<div id="checkout"></div>
<script src="https://pay.dominaite.com/v1/checkout.js"></script>
<script>
  const checkout = Dominaite.checkout({
    transactionId: "{{ transaction_id }}",
    integration: "fields",
    cashierKey: "{{ cashier_key }}",
    cashierToken: "{{ cashier_token }}",
    clientSecret: "{{ client_secret }}",
  })
  checkout.on("success", () => { /* show a "thank you, confirming" state */ })
  checkout.mount("#checkout")
</script>
```

`clientSecret` is what lets the browser charge this one session: keep it out of your logs and
escape it for the context you template it into. A widget session has no `clientSecret` key, so
read it with `session.get("clientSecret")`.

The page saying "success" is not proof of payment. Mark the order paid only from the
`payment.succeeded` webhook or a `get_status()` read, exactly as with the widget.

## Stored payment methods (recurring)

Pass `save_card=True` when you create a session and, once that payment is approved, the gateway
keeps the card on file. You never see the card number or the provider token: `get_status()` returns
a `storedPaymentMethod` with an opaque `id` (`pm_` + 32 hex characters), the `brand`, the `last4`
and the expiry, and that `id` is what you charge and revoke with. Store it against your customer.
(`paymentMethod` on the same status is something else: the gateway's string category of how the
payer paid, `card`, `wallet` and so on.)

Saving a card needs no switch. We switch stored-card charges on for your account. Until then,
`charge_payment_method()` raises a 503 `ChargeError` with `PAYMENT_METHOD_CHARGES_DISABLED`. It
raises the same error when we pause stored-card charges for a short time. The code is the same
in both cases. If we never switched stored-card charges on for your account, a retry does not
help: ask Dominaite support. If they are on, retry later with the same idempotency key.

```python
from dominaite import ChargeError, ChargeStatus, DeclineClass, RevokeError, StoredPaymentMethodStatus

session = client.create_checkout_session(
    amount=2500,
    currency="EUR",
    order_reference="sub-8817-first",
    save_card=True,
    idempotency_key=order_idempotency_key("sub-first", "8817", 2500, "EUR"),
)
# ... the payer completes the hosted checkout ...
status = client.get_status(session["transactionId"])
stored = status.get("storedPaymentMethod")
if status["status"] == "succeeded" and stored and stored["status"] == StoredPaymentMethodStatus.ACTIVE:
    db.save_card(customer_id, stored["id"])  # pm_...

# Later, off-session, no payer present:
try:
    charge = client.charge_payment_method(
        payment_method_id,
        amount=2500,
        currency="EUR",
        order_reference="sub-8817-2026-10",
        description="Monthly plan, October",
        idempotency_key="sub-8817-2026-10",  # derive it from the billing period, never random per attempt
    )
except ChargeError as error:
    if error.error_code == "CHARGE_OUTCOME_UNKNOWN":
        # 502: the provider gave no verdict, the charge MAY have happened. Never retry
        # under a new key: poll the transaction the gateway attached instead.
        poll_until_settled(error.transaction_id)
    elif error.error_code == "PAYMENT_METHOD_CHARGES_DISABLED":
        # 503, nothing was charged. Never switched on for your account: a retry does not
        # help, ask Dominaite support. Switched on: retry later with the SAME idempotency key.
        ...
    elif error.error_code in ("DUPLICATE_REQUEST", "PAYMENT_PROCESSING_UNAVAILABLE"):
        ...  # nothing was charged; retry later with the SAME idempotency key
    elif error.error_code == "PAYMENT_METHOD_NOT_ACTIVE":
        ...  # revoked, expired or retired: bring the customer back for a hosted session with save_card
    elif error.error_code == "CHARGE_FAILED":
        ...  # 502, nothing was charged; error.charge is set when a row exists
    elif error.error_code == "IDEMPOTENCY_KEY_REUSED":
        ...  # same key, different body or method: a bug on your side
    raise
else:
    if charge["status"] == ChargeStatus.SUCCEEDED:
        ...
    elif charge["status"] == ChargeStatus.PENDING:
        ...  # not terminal: poll get_status(charge["transactionId"]) or wait for the webhook
    elif charge["status"] == ChargeStatus.FAILED:
        # HTTP 402 from the gateway, but not an exception: branch on the class, log the code.
        # The class can be None, for example when nobody declined the payment.
        # DeclineClass.HARD              - give up on this card, ask the customer for another one
        # DeclineClass.SOFT_FUNDS        - insufficient funds, retry later (not in a loop)
        # DeclineClass.SOFT_SCA_REQUIRED - the issuer wants the customer present: send them
        #                                  through a hosted session with save_card and charge the new method
        # DeclineClass.SOFT_OTHER        - transient, one retry later is reasonable
        handle_decline(charge["declineClass"], charge["declineCode"])
    elif charge["status"] == ChargeStatus.CANCELLED:
        ...  # an authorization voided before capture; no money moved

# When the customer removes the card:
try:
    client.revoke_payment_method(payment_method_id)  # 204, returns None; 204 again if already revoked
except RevokeError as error:
    if error.error_code == "MERCHANT_API_UNAVAILABLE":
        ...  # 503: nothing changed, retry later
    else:
        ...  # 502 UPSTREAM_CONTRACT_ERROR: the provider refused for good, nothing changed; contact support with the id
```

A charge is signed exactly like a session and carries an `Idempotency-Key`, so a retry after a
timeout with the **same** key never charges the card twice: the gateway replays its first answer,
HTTP status included. The HTTP status is the contract on this route: 201 (or 200 on a replay)
returns the charge, 402 returns the charge too (`status` `failed`), and 409,
422, 502 and 503 raise `ChargeError` with `error_code`, `http_status`, the gateway's message and,
when the gateway attached the charge row, `charge` and `transaction_id`. Only authentication
(401/403), an id that is not yours (404, `ApiError`), validation (400, `ApiError`), rate limiting
(429) and network failures keep their generic exceptions. `declineClass` and `declineCode` can be
`None`, for example when nobody declined the payment. The gateway omits a null key on the wire, and
the SDK reads absent as `None`.

To bill an agreement, pass `agreement_id`. The gateway then checks the charge against the
agreement: the card, the currency, the amount and the period. A charge that does not fit is
refused with a 422 `ChargeError`, for example `AGREEMENT_PERIOD_ALREADY_CHARGED` or
`CHARGE_AMOUNT_ABOVE_AGREEMENT`, and nothing is charged. The returned charge has `agreementId`
(`None` outside an agreement) and `sequence`, which orders it against the `charge.*` events of
the same object.

Revoking signs an empty key and an empty body, like `get_status()`. A revoke that fails with
`RevokeError` changed nothing: `MERCHANT_API_UNAVAILABLE` (503) is retryable,
`UPSTREAM_CONTRACT_ERROR` (502) is not. After a revoke the status read keeps the
`storedPaymentMethod` with `status` `revoked`, and a charge against it is refused with
`PAYMENT_METHOD_NOT_ACTIVE`.

### Save a card without a payment (verify-only)

A verify-only session saves the card and takes no payment. The checkout places a small
authorization to check the card. The platform releases it within about 15 minutes. Nobody is
charged. We switch it on for your account. Until then, the API refuses the session with a 403:
an `AuthenticationError` with `error_code` `VERIFY_ONLY_NOT_ENABLED`.

```python
session = client.create_checkout_session(
    amount=0,
    currency="EUR",
    order_reference="verify-cus-42",
    verify_only=True,
    idempotency_key="verify-cus-42",
)
# ... the customer completes the hosted checkout ...
status = client.get_status(session["transactionId"])
stored = status.get("storedPaymentMethod")
if stored and stored["status"] == StoredPaymentMethodStatus.ACTIVE:
    db.save_card(customer_id, stored["id"])  # pm_...
```

1. Send `amount=0`. The API refuses every other amount on a verify-only session.
2. Do not send `save_card=False`, `integration="fields"` or `agreement_id`. The API refuses
   them on a verify-only session.
3. Read the result from `storedPaymentMethod`, not from `status`. The status of a checked card
   is `requires_capture`, then `cancelled` when the authorization is released.
4. `verifyOnly` is `True` on the returned session and on the status read.
5. `order_idempotency_key()` refuses an amount of 0. Use your own key, unique per check.
6. The webhook events are `verification.succeeded` and `verification.failed`. A verify-only
   session fires no `payment.*` events.

## Subscriptions (plans and agreements)

A plan is a price that you bill every period. An agreement subscribes one of your customers to a
plan. We switch subscription sessions on for your account. Until then,
`create_checkout_session()` with `agreement_id` raises `CheckoutRefusedError` with
`SUBSCRIPTIONS_NOT_ENABLED`. You can still create plans and agreements, but an agreement stays
`pending` until its first payment. Scheduled charges are a separate switch. With scheduled
charges on, the platform charges the stored card each period. Without that switch, the platform
runs no scheduled charge and no retry. Ask Dominaite support for both switches.

```python
plan = client.create_plan(
    name="Pro monthly",
    amount=2500,
    currency="EUR",
    interval_unit="month",
    cancel_url="https://shop.example/account/cancel",
    idempotency_key="plan-pro-monthly-2500-eur",
)
db.save_plan(plan["id"])  # plan_...

agreement = client.create_agreement(
    plan_id=plan["id"],
    customer_reference="cus-42",  # your own stable id for the customer
    idempotency_key="agreement-cus-42-pro-monthly",
)

session = client.create_checkout_session(
    amount=agreement["amount"],
    currency=agreement["currency"],
    order_reference="sub-cus-42-first",
    agreement_id=agreement["id"],
    consent={
        "textVersion": "recurring-2026-09",  # one of recurring.consentTextVersions
        "acceptedAt": "2026-10-11T09:30:00Z",
        "ip": customer_ip,
    },
    idempotency_key="sub-cus-42-first",
)
# ... the customer completes the hosted checkout ...
agreement = client.get_agreement(agreement["id"])
# "active" after the first payment is approved, still "pending" after a decline
```

1. Create a plan once for each price. Amount, currency and interval cannot change after that.
   To change a price, create a new plan and archive the old one with `delete_plan()`. Existing
   agreements keep their terms.
2. Create one agreement for each customer and plan. It starts `pending` and charges nothing.
3. Take the first payment with a subscription session: `create_checkout_session()` with
   `agreement_id` and `consent`. Send the agreement's amount and currency. On a plan with a
   trial, the session charges the amount you send. With scheduled charges on, the first
   scheduled charge comes when the trial ends.
4. `consent` names the consent text that your page showed the customer. Use one of
   `recurring.consentTextVersions` from `get_integration_contract()`, and send all three fields.
5. A session that does not fit the agreement raises `CheckoutRefusedError`, and nothing is
   created. Read `error_code`, for example `AGREEMENT_AMOUNT_MISMATCH`, `AGREEMENT_SESSION_OPEN`
   or `AGREEMENT_NOT_PENDING`.
6. Follow the agreement with the `agreement.*` and `charge.*` webhooks, or read it with
   `get_agreement()`. See "Ordering agreement and charge events" below.
7. `cancel_agreement()` cancels the agreement. With scheduled charges on, it also stops the
   scheduled charges that are not placed yet. It does not reverse a charge. If the agreement is
   already cancelled, the call succeeds again. A retry is safe.

To bill a card that the customer saved before, pass `stored_payment_method_id` and `consent` to
`create_agreement()`. For a variable amount, pass `max_amount_per_period` and `consent`. A
subscription session refuses an agreement with a maximum (`AGREEMENT_VARIABLE_AMOUNT_UNSUPPORTED`).

`update_plan()` changes only `name`, `trial_days` and `cancel_url`. `trial_days=0` removes the
trial and `cancel_url=""` removes the cancel page. A trial change applies to agreements created
after it. The update sends no idempotency key. `list_plans()` returns at most 200 plans, active
plans first, and has no paging.

Plan and agreement ids are case-sensitive. Store them and send them exactly as returned. A
refusal raises `ApiError` with `error_code`, for example `PLAN_NOT_FOUND` (404),
`PLAN_NOT_ACTIVE` (409) or `AGREEMENT_ENDED` (409). `result` holds the whole answer:
`MAX_AMOUNT_BELOW_PLAN_AMOUNT` carries the plan's amount in
`result["error"]["validationErrors"][0]["params"]["min"]`. The methods return plain dicts,
described by the `Plan` and `Agreement` `TypedDict`s. A status value not listed there is valid.

## Refunds

Refund a paid payment in full or in part with `create_refund`. The amount is minor units of the
payment's currency, so use `to_minor_units` rather than multiplying by 100: HUF has no decimals
at the gateway.

The API refunds online (card-not-present) payments only. `create_refund()` on a terminal
payment raises `RefundError` with `PAYMENT_NOT_FOUND` (404). Whoever holds the payments key can
refund up to the paid amount, so keep that key on your server.

```python
from dominaite import RefundError, RefundStatus, to_minor_units

refund = client.create_refund(
    transaction_id,                               # the payment's transactionId
    amount=to_minor_units("1500", "HUF"),         # 1500, whole forints; leave out for a full refund
    reason="Returned item 2 of 3",                # optional, at most 500 characters
    idempotency_key="refund-" + credit_note.id,   # derive it from YOUR refund, never random
)
# {"refundId": "re_...", "transactionId": ..., "status": "pending", "amount": 1500,
#  "currency": "HUF", "failureCode": None, "failureMessage": None, "completedAt": None}
```

Leave `amount` out to refund everything still refundable; the SDK then sends no `amount` at all.
Partial refunds add up, and the gateway refuses an amount above what is left (counting refunds
still in progress) with `REFUND_AMOUNT_EXCEEDED`.

**202 means queued, not done.** The refund comes back `pending`, then goes `processing` and ends
`succeeded` or `failed`. Learn the outcome from the `payment.refunded` webhook, which fires once
the money has moved, or read it back:

```python
refund = client.get_refund(transaction_id, refund["refundId"])
if refund["status"] == RefundStatus.SUCCEEDED:
    ...  # refund["amount"] is what went back to the payer
elif refund["status"] == RefundStatus.FAILED:
    ...  # refund["failureCode"]; a new attempt needs a NEW idempotency key
```

A failed refund sends no webhook, so poll `get_refund` if you need to know about failures. On
`failed`, `amount` is always None and `failureCode` is `REFUND_AMOUNT_EXCEEDED`,
`PAYMENT_NOT_REFUNDABLE` or `REFUND_FAILED` (`REFUND_FAILURE_CODES`); treat any other value as
`REFUND_FAILED`. Nulls are absent on the wire and the SDK fills them in as None, so every key of
the `Refund` is always there.

The idempotency key works like a charge's: it is required, signed, and the same key answers the
same refund and never refunds twice. Retry a timeout with the **same** key; a replay answers
with the refund as it stands now. The same key with a different amount, reason or payment is
refused as `IDEMPOTENCY_KEY_REUSED`.

When the gateway answers with a code instead of a refund you get a `RefundError` (a subclass of
`ApiError`) with `error_code`, `http_status`, and `retryable` plus `retry_stop_after_seconds`
for the two codes worth retrying with the same key:

| `error_code` | HTTP | Retry with the same key? |
|---|---|---|
| `PAYMENT_NOT_FOUND` | 404 | No: not a card-not-present payment of your account |
| `REFUND_NOT_FOUND` | 404 | `get_refund` right after the 202: yes, for up to 60 seconds |
| `PAYMENT_NOT_REFUNDABLE` | 422 | No: not paid, or already fully refunded. Nothing queued, key not burnt |
| `REFUND_AMOUNT_EXCEEDED` | 422 | No: the message names the amount left. Nothing queued, key not burnt |
| `IDEMPOTENCY_KEY_REUSED` | 422 | No: use a fresh key for a genuinely new refund |
| `DUPLICATE_REQUEST` | 409 | Yes, after a second, for up to 120 seconds |
| `IDEMPOTENCY_KEY_REQUIRED` | 400 | No: fix the call |

A 5xx is a `TransportError`: nothing was queued. The client already retried it; retry later with
the same key.

```python
try:
    refund = client.create_refund(transaction_id, idempotency_key=key)
except RefundError as error:
    if error.retryable:
        ...  # retry with the SAME key within error.retry_stop_after_seconds
    else:
        ...  # error.error_code says why; nothing was refunded
```

### List the refunds of a payment

`list_refunds` returns a payment's refunds made through this API and the amounts still open:

```python
listing = client.list_refunds(transaction_id)
listing["refundableAmount"]  # minor units a new refund can take now
listing["refundedAmount"]    # minor units refunded so far, from every source
for refund in listing["refunds"]:  # newest first, each with every Refund key
    print(refund["refundId"], refund["status"])
```

1. `refunds` holds the refunds made through this API. The two amounts also count refunds made
   in the dashboard.
2. `refundableAmount` leaves out refunds still in progress. It is 0 when the payment cannot be
   refunded.
3. The list is not paged. An unknown payment is a `RefundError` with `PAYMENT_NOT_FOUND`.

## Webhooks

Webhooks are how you find out what happened to a payment. Create an endpoint in the Dominaite
dashboard or with `create_webhook_endpoint()` (see "Manage endpoints with the API" below), pick
the events you care about, and store the `whsec_...` secret it shows you - like the API secret,
it is shown **once**.

Dominaite POSTs each event to your URL with an `X-Webhook-Signature` header:

```
X-Webhook-Signature: t=1755700000,v1=5305bcf1302fdaba8f8c19a20c899e916fb4d2a7d8d547c62529ff87c4697b72
```

`verify_webhook` checks it and hands you the decoded event:

```python
import os

from flask import Flask, request

from dominaite import WebhookVerificationError, verify_webhook

app = Flask(__name__)
SECRET = os.environ["DOMINAITE_WEBHOOK_SECRET"]


@app.post("/webhooks/dominaite")
def dominaite_webhook():
    try:
        event = verify_webhook(
            request.get_data(),                          # RAW body, not request.json
            request.headers.get("X-Webhook-Signature", ""),
            SECRET,
        )
    except WebhookVerificationError:
        return "", 400

    if already_handled(event["id"]):                     # delivery is at-least-once
        return "", 200

    enqueue(event)                                       # do the work outside the request
    return "", 200
```

**Pass the raw body.** `request.json` (or any parse-then-re-serialize round trip) gives you
different bytes than the ones that were signed, and verification will fail. This is the single
most common webhook integration bug.

**Verify before you read anything.** Until `verify_webhook` returns, the body is just bytes a
stranger POSTed at you. Do not branch on `event["type"]` or trust an amount from an unverified
payload.

### Manage endpoints with the API

You can also register, list and delete endpoints from your server. These calls need a reporting
key that holds the `webhooks:manage` scope: see [Credentials](#credentials). The dashboard
Webhooks page needs no key.

```python
admin = DominaiteClient(webhooks_key_id, webhooks_key_secret, base_url=base_url)

endpoint = admin.create_webhook_endpoint(
    url="https://shop.example/webhooks/dominaite",
    events=["payment.succeeded", "payment.failed", "payment.refunded"],
    idempotency_key="webhook-shop-main",
)
save_secret(endpoint["id"], endpoint["secret"])  # whsec_..., in this answer only

for item in admin.list_webhook_endpoints():  # newest first, never with a secret
    print(item["id"], item["url"], item["status"])

admin.delete_webhook_endpoint(endpoint["id"])
```

1. `secret` is in the first answer only. A replay with the same key and the list leave it out.
   If you lose the secret, delete the endpoint and create it again.
2. After a timeout or a 503, retry with the same `idempotency_key`. The retry replaces an
   endpoint that the lost attempt registered. A new key cannot do this.
3. A key used again more than a day later raises `WEBHOOK_ALREADY_REGISTERED`.
   `result["error"]["params"]["webhookId"]` names the endpoint. Use a fresh key for a new
   endpoint.
4. `events` takes the types listed under "The events" below. The SDK sends the names as given.
   The API refuses an unknown type with `VALIDATION_ERROR`.
5. The URL must be HTTPS on a public host, or the API refuses it with `WEBHOOK_URL_REJECTED`.
   You can have 25 active endpoints. One more raises `WEBHOOK_LIMIT_REACHED`.
6. `delete_webhook_endpoint()` on an unknown or deleted id raises `WEBHOOK_NOT_FOUND`. After a
   lost answer, treat that as done.

### The events

`payment.succeeded`, `payment.failed`, `payment.requires_capture`, `payment.cancelled`,
`payment.abandoned`, `payment.refunded`, `payment.disputed`.

Recurring agreements add `agreement.activated`, `agreement.past_due`, `agreement.cancelled`,
`charge.succeeded` and `charge.failed`. With scheduled charges on, they also add
`charge.retrying`. These events ride the same signed envelope.
A verify-only session sends `verification.succeeded` or `verification.failed`, and no
`payment.*` event. Their `data` has the keys of a `payment.*` event, with `verifyOnly` set to
`True`. On `payment.failed` and `verification.failed`, `declineClass` says why the card was
declined. It can be `None`, for example when nobody declined the payment.

`payment.succeeded` is the only one that means money in hand. `requires_capture` is an approved
hold, not a payment. `pending` and `processing` are never webhooked - if you want to show an
in-flight state to a customer, poll the session.

Each delivery is a flat JSON object - there is no `success` wrapper, so do not branch on one:

```json
{
  "id": "7f9c24e5-1d1f-4c0a-9b6c-2f3a4d5e6f70",
  "type": "payment.succeeded",
  "apiVersion": "2026-09-25",
  "createdAt": "2026-08-20T14:00:00Z",
  "data": {
    "transactionId": "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0",
    "orderReference": "order-123",
    "orderId": "dom_0f1e2d3c4b5a69788796a5b4c3d2e1f0",
    "pspReference": "psp_8f2a61c0",
    "description": "Pro plan",
    "status": "succeeded",
    "previousStatus": "pending",
    "kind": "sale",
    "amount": 8440,
    "grossAmount": 8701,
    "surchargeAmount": 261,
    "currency": "EUR",
    "paymentMethod": "card",
    "walletType": null,
    "paymentMethodBrand": "visa",
    "paymentMethodLast4": "4242",
    "originalTransactionId": null,
    "idempotencyKey": "order-123"
  }
}
```

`apiVersion` is the dated version of the payload shape. Fields are only ever added under a
version, never renamed or removed, so ignore keys you do not recognise. A retry resends the
bytes of its first attempt, so an event rendered before `apiVersion` existed arrives without it.

Amounts are minor units. On `payment.*` events `amount` is what you get paid and `grossAmount`
is what moved on the card; on `payment.refunded` `amount` is what went back to the customer.
`payment.refunded` fires once per completed refund, partial or full: its `transactionId` is the
refund's own id and `originalTransactionId` the payment it refunds.

Every `payment.*` event identifies the payment with the same keys:

- `orderReference` is your own order reference from create session. Match deliveries to your
  orders on it. It is null for payments not started through the API or created without one;
  refund and cancel events carry the original payment's value.
- `orderId` is the hosted checkout order id, as on `get_status()`. Null on refund and
  cancellation events and for payments taken outside hosted checkout.
- `pspReference` is the payment processor's reference, as on `get_status()`. Null until the
  processor reports it, so a `payment.succeeded` can arrive without it; read the status later to
  pick it up. Refund and cancel events carry the original payment's reference.
- `description` is what you sent on create session. Null when none was given and on refund and
  cancellation events.
- `paymentMethodBrand` (lower-cased, e.g. `visa`) and `paymentMethodLast4` are set once a card
  payment was attempted. Null while the payment is open, for non-card methods, and when the
  provider did not report them. `paymentMethod` and `walletType` say how the payer paid
  (`card`, `wallet`, ...; `apple_pay`, `google_pay`, ...). They are reporting data: a wallet
  payment refunds, captures and disputes like a card.
- `idempotencyKey` is the key you sent on create session, null on refund and dispute events.
  `originalTransactionId` is set on `payment.refunded` (the refunded payment) and on the
  `payment.succeeded` of a captured authorization (the authorization), null otherwise.

All of them are typed on `PaymentEventData`.

Unlike the API responses, webhooks write null fields out as `null`. Read a missing key and a
`null` the same way.

### The saved card on payment events

On a session created with `save_card=True`, `payment.succeeded` (and `payment.requires_capture`
for an authorization) carries the card in `data["storedPaymentMethod"]`: the same object, with
the same keys, as `storedPaymentMethod` on `get_status()`.

```json
"storedPaymentMethod": {
  "id": "pm_0123456789abcdef0123456789abcdef",
  "brand": "visa",
  "last4": "4242",
  "expiryMonth": 12,
  "expiryYear": 2030,
  "status": "active",
  "retiredReason": null
}
```

It is None (or missing) on every other event and when no card was saved. **It can also be None
when a card was saved**: the card can be stored after the approval was already announced, for
example on a payment approved on the spot server to server, or one settled later. The status
read is the source of truth, so on a `save_card` session whose webhook has no card, read it:

```python
card = event["data"].get("storedPaymentMethod")
if card is None and session_saved_a_card:
    card = client.get_status(event["data"]["transactionId"]).get("storedPaymentMethod")
```

### Delivery, retries, and staying enabled

- **At-least-once.** The same event can arrive twice. Dedupe on `event["id"]` and make your
  handler idempotent.
- **Answer fast.** Return 2xx as soon as the signature checks out and queue the real work.
  Doing it inline is how endpoints end up timing out and getting retried.
- **Retries** follow your endpoint's configured count (default 3, max 10), spaced 1m, 5m, 30m,
  2h, 12h.
- **Circuit breaker.** An endpoint whose first attempt and every retry fail, over and over, is
  disabled automatically. Any later successful delivery re-enables it. A disable you did
  yourself in the dashboard is never undone for you.

### Ordering agreement and charge events

`agreement.*` and `charge.*` events carry `data["sequence"]`, an integer that only ever rises
per object, and a redelivery carries the same number:

> Deliveries can arrive out of order. Keep the highest sequence you have processed per object
> and discard any event whose sequence is not higher; when you need current state, read the
> object by id. createdAt can repeat across events, so order by sequence, not createdAt. A
> sequence of 0 only comes from events recorded before the counter existed; treat it as older
> than any positive number.

The object is:

- `agreement.*`: the agreement, `data["id"]`.
- With scheduled charges on, `charge.*` for a charge the platform placed for an agreement (it has a
  `periodNumber`): the agreement period, `data["agreementId"]` plus `data["periodNumber"]`. With
  scheduled charges on, the retrying and failed events of one period compare across attempts.
- `charge.*` for a one-off charge you initiated: `data["chargeId"]`.

```python
data = event["data"]
if event["type"].startswith("agreement."):
    key = ("agreement", data["id"])
elif data.get("periodNumber") is not None:
    key = ("period", data["agreementId"], data["periodNumber"])
else:
    key = ("charge", data["chargeId"])

sequence = data.get("sequence", 0)       # absent on events from before the counter
last = last_sequence_processed(key)      # None when you have seen nothing for this object
if last is not None and sequence <= last:
    return "", 200                       # stale or duplicate: acknowledge, skip
```

A delivery from before the counter carries no `sequence` or `0`. Nothing orders two of those
against each other, so read the object by id when that matters.

`WebhookEvent`, `PaymentEventData`, `AgreementEventData` and `ChargeEventData` are `TypedDict`s
of these shapes, and `StoredPaymentMethod` is the card on both `get_status()` and payment events.
`verify_webhook` still returns a plain dict, so `cast` to them when you want the keys checked.

### Reconcile anyway

Webhooks complement your reconciliation sweep, they do not replace it. There are real loss
windows: a chain parked on an endpoint the breaker disabled, an event that never got published.
The only thing that closes them is a periodic sweep of your own open orders against
`get_status`. Keep the sweep. It is what catches the payment nobody told you about.

### Tolerance and clocks

Deliveries more than 300 seconds away from your clock are rejected as replays. If you see
`TIMESTAMP_OUT_OF_RANGE` on genuine traffic, your server clock has drifted - fix NTP rather than
widening `tolerance_seconds`.

Pass `now` to keep your own tests deterministic, and `sign_webhook` to forge a delivery in them:

```python
from dominaite import sign_webhook, verify_webhook

body = '{"id":"evt-1","type":"payment.succeeded","createdAt":"2026-08-20T14:00:00Z","data":{}}'
header = "t=1755700000,v1=" + sign_webhook("whsec_test", "1755700000", body)

event = verify_webhook(body, header, "whsec_test", now=1755700000)
```

## Status polling (fallback)

Polling is the fallback and the reconciliation tool, not the primary path - use
[webhooks](#webhooks) to learn that a payment completed, and use this to sweep your own open
orders and to answer "what is this order doing right now".

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

Those values are also exported as the `PaymentStatus` enum (and `PAYMENT_STATUSES`), so you can
match on a named member instead of a bare string literal. It subclasses `str`, so
`status["status"] == PaymentStatus.SUCCEEDED` works directly against what `get_status` returns.

`succeeded` is the only value that means the payment is complete. Keep polling on `pending`,
`processing` and `requires_capture` - none of them is terminal.

`requires_capture` is **not** "unpaid": the payer has already paid and the funds are held
awaiting capture. Never treat it as an abandoned order.

Treat any status you do not recognise as still-open as well: a value the API adds later should
make you keep polling, never silently close an order that is still live.

Two helpers encode those rules so you do not have to:

```python
from dominaite import is_paid, is_terminal

status = client.get_status(transaction_id)["status"]
if is_paid(status):        # succeeded, and nothing else
    ship(order)
elif is_terminal(status):  # failed, cancelled, abandoned, refunded, partially_refunded
    close(order)
# else: pending, processing, requires_capture, disputed or unknown - keep the order open
```

`is_terminal` is also True for `succeeded`, so check `is_paid` first. The terminal set is
exported as `TERMINAL_PAYMENT_STATUSES`.

`paymentMethod` (`card`, `wallet`, `bank_transfer` or `sepa`; None while the payment is still
open and on older transactions) and `walletType` (`apple_pay`, `google_pay`, `samsung_pay`; None
for non-wallet payments) say how the payer paid. They are reporting data, not a money-flow
switch: a wallet payment refunds, captures and disputes like a card. `walletType` can carry a
lower-cased wallet not in that list yet; treat it as a valid wallet, not an error. The known
values are exported as `PAYMENT_METHOD_CATEGORIES` and `WALLET_TYPES`.

The status read has more keys. The API leaves out a key whose value is null, so read them with
`.get`. The `CheckoutStatus` `TypedDict` names every key.

- `declineClass`: on a `failed` payment, what to do next: `hard`, `soft_funds`,
  `soft_sca_required` or `soft_other`. It can be `None`, for example when nobody declined the
  payment.
- `isRecurring`: `True` for a charge on a stored card, `False` for a checkout session.
- `verifyOnly`: `True` on a verify-only session.
- `agreement`: on a subscription session, the agreement `id` and its current `status`. Read
  `agreement["status"]`, not the payment status, to know if the subscription started.
- `pspReference`: the payment processor's reference, for documents such as a receipt. It is
  `None` until the processor reports it.
- `sequence`: orders this read against the `charge.*` events of the same object.

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

`DUPLICATE_REQUEST` means a session for this key is open, or expired within the last few
minutes. Either way the move is the same: re-POST the same key shortly, never a fresh one.

One replay is not a refusal at all. A session that expired unpaid is superseded: from a few
minutes past its expiry, re-POSTing the same key returns an ordinary success with a fresh
session (new transaction id, same key), so a customer who comes back late just pays. Keep the
order-derived key for the life of the order to keep that path open. The band is not endless -
once the platform has independently closed the attempt (about an hour past expiry), the replay
answers `PRIOR_ATTEMPT_FAILED` and the key is spent; reconcile and use a fresh key.

## Transactions

`list_transactions()` lists your card payments created in a time window, one page at a time. Use
it to reconcile and to build statements. It needs a reporting key that holds the
`transactions:read` scope: see [Credentials](#credentials).

```python
import datetime

start = datetime.datetime(2026, 10, 1, tzinfo=datetime.timezone.utc)
end = datetime.datetime(2026, 10, 8, tzinfo=datetime.timezone.utc)

cursor = None
while True:
    page = client.list_transactions(start, end, status="succeeded", cursor=cursor)
    for row in page["items"]:
        record(row["transactionId"], row["type"], row["amount"], row.get("refundedAmount"))
    cursor = page["nextCursor"]
    if cursor is None:
        break
```

1. The window is `from_` included, `to` not included, and at most 31 days long.
2. Send an ISO 8601 UTC string, a timezone-aware `datetime` or a `date`. The SDK refuses a
   naive `datetime`, because its zone is unknown.
3. Rows come oldest first. Send `nextCursor` back as `cursor` with the same window and status,
   until it is `None`. Paging never repeats or skips a row.
4. A payment is in the window of its `createdAt`, with its status at the time of the read. A
   refund is not a row of its own. It shows on its payment as `refundedAmount` and `status`.
5. `type` is `payment`, `charge` or `verification`. A `verification` row is not a purchase.
6. For a statement, end the window a few minutes in the past. Read again later to get changes
   such as refunds.
7. `fee`, `netAmount`, `settledAt` and `payoutId` are empty until the acquirer settles the
   payment. This is usually the next business day. Read them with `.get`.
8. `netAmount` is `amount` minus the fee of this payment only. Deductions on the whole payout
   are not in it. `fee` and `netAmount` can change, so read a settlement day again before you
   close your books on it.
9. To get one settlement day, send `settled_from` as that day and `settled_to` as the next day.
   Also send a `from_` and `to` that cover when those payments were created, for example the
   last 31 days.

```python
today = datetime.date(2026, 10, 14)
page = client.list_transactions(
    today - datetime.timedelta(days=31), today,
    settled_from=today - datetime.timedelta(days=1), settled_to=today,
)
```

## Logging and warnings

The SDK writes nothing to stdout or to `logging` on its own. It reports through two optional
callables on the client:

```python
import logging

client = DominaiteClient(
    os.environ["DOMINAITE_KEY_ID"],
    os.environ["DOMINAITE_SECRET"],
    base_url=os.environ.get("DOMINAITE_BASE_URL"),
    user_agent="shop/1.2",                                  # optional suffix
    on_warning=logging.getLogger("dominaite").warning,      # optional
    on_debug=logging.getLogger("dominaite").debug,          # optional, off when not set
)
```

- `on_warning` gets a message for: a `Dominaite-Sdk-Notice` response header (the API sends it
  when your SDK version is deprecated, once per distinct value per client), each retry, a call
  to `create_checkout_session_with_retry`, and a ping `clockSkewSeconds` over 60. Without it the
  SDK calls `warnings.warn`: `DeprecationWarning` for the notice and the deprecated helper,
  `UserWarning` for the others.
- `on_debug` gets one line per request, for example
  `dominaite-python: POST /merchant-api/checkout/sessions 201 184ms request_id=... idempotency_key=checkout-1042-2500-EUR`.
  Debug is off until you pass it. Give support the `request_id` when you ask about a call.
- No line carries the secret, the signature, a cashier token, a query string or a request or
  response body. An exception from either callable is dropped, so it never fails the request.
- The User-Agent is `dominaite-python/0.6.0 (python 3.12.4)`, then your `user_agent` suffix
  after one space. A suffix with a control character raises `ValueError`.

## Errors

| Exception | Means | Retry? |
|---|---|---|
| `AuthenticationError` | `http_status` 401: bad credentials, bad signature, clock skew, IP not allowlisted. `http_status` 403: the key is good but may not do this (`INSUFFICIENT_SCOPE`, `VERIFY_ONLY_NOT_ENABLED`) | No - fix config, or use a key with the right scope |
| `CheckoutRefusedError` | The gateway refused to open the session (`error_code`) | Depends on the code |
| `StorefrontError` | The storefront (website) cannot take payments yet, or the key belongs to another one (`error_code`, `http_status`). Subclass of `ApiError` | No - fix the setup |
| `ChargeError` | The gateway answered a charge with a code instead of a charge (`error_code`, `http_status`, `charge`, `transaction_id`). A coded 404 such as `PAYMENT_METHOD_NOT_FOUND` is a `ChargeError` too | Depends on the code; never with a new key |
| `RefundError` | The gateway answered a refund call with a code (`error_code`, `http_status`, `retryable`, `retry_stop_after_seconds`). Subclass of `ApiError` | Only when `retryable`, same key |
| `RevokeError` | The gateway refused to revoke a stored payment method; nothing changed (`error_code`, `http_status`) | `MERCHANT_API_UNAVAILABLE` only |
| `ApiError` | Unexpected response, or a 4xx like an unknown transaction id (`http_status`, `error_code`, and `result`: the whole JSON body, with `validationErrors` and `params` when the gateway sent them) | No |
| `RateLimitError` | HTTP 429; you are sending faster than you are allowed (`error_code`, `retry_after_seconds`) | Yes, after you wait |
| `TransportError` | Network failure or 5xx; you don't know if it landed. A coded 5xx keeps its code on `error_code` and the body on `result`; `http_status` is None for a network failure | Yes, same idempotency key (the client already retried it) |
| `WebhookVerificationError` | An incoming webhook is not authentic or not fresh (`error_code`) | No - respond 400 |

All of them inherit from `DominaiteError` if you only care that the call failed.

One rule picks the class: the gateway's code wins over the HTTP status.

- A 5xx keeps its code on `error_code`. A 5xx without a JSON body (a proxy error page) is a
  `TransportError` with no code.
- A coded answer to a charge is a `ChargeError`, a 404 and a 5xx included. Only 400, 401, 403
  and 429 keep their usual classes. Catch `ChargeError` around a charge, not only
  `TransportError`.
- A storefront code on checkout is a `StorefrontError`, also in the 200 shape.

Note the two different failure shapes on the create endpoint. A business refusal is HTTP 200
with `success: false` and raises `CheckoutRefusedError`; input validation is HTTP 400 and raises
`ApiError` with the code on `error_code` (currently `IDEMPOTENCY_KEY_REQUIRED`, exported as
`VALIDATION_ERROR_CODES`). Branch on the exception type, never on the HTTP status.

Every code in these tables is also a named constant on `ErrorCode`, a `str` enum, so
`error.error_code == ErrorCode.ALREADY_PROCESSED` works against the plain string:

```python
from dominaite import CheckoutRefusedError, ErrorCode

try:
    session = client.create_checkout_session(...)
except CheckoutRefusedError as refusal:
    if refusal.error_code == ErrorCode.ALREADY_PROCESSED:
        ...  # this order is paid; show the receipt
```

### Storefront errors

If your merchant account has more than one website (storefront), each session is tied to one of
them, and the gateway checks that storefront before it opens anything. These refusals are not
the 200 `success: false` shape: they arrive as `StorefrontError` (a subclass of `ApiError`), with
the code on `error_code` and the HTTP status on `http_status`. Nothing was created.

| `error_code` | HTTP | Means | What to do |
|---|---|---|---|
| `STOREFRONT_NOT_WHITELISTED` | 409 | The storefront's domain is not approved by the payment provider yet. | Nothing in your code. Ask Dominaite support to finish the domain whitelisting, then try again. |
| `STOREFRONT_INACTIVE` | 409 | The storefront was deactivated or deleted. | Use an API key for an active storefront, or ask support to reactivate it. |
| `STOREFRONT_MISMATCH` | 400 | The API key is bound to a different storefront than the request names. | Use the key issued for this storefront. |

```python
from dominaite import ErrorCode, StorefrontError

try:
    session = client.create_checkout_session(...)
except StorefrontError as error:
    if error.error_code == ErrorCode.STOREFRONT_NOT_WHITELISTED:
        alert_ops("checkout blocked: domain not whitelisted yet")
    raise
```

An idempotency key first used on another storefront is also refused with
`STOREFRONT_MISMATCH`, in the 200 shape. That refusal is a `StorefrontError` as well, with
`http_status` 200, so one `except StorefrontError` handles both. The codes are exported as
`STOREFRONT_ERROR_CODES`.

### Request ids

Every result and every error carries the gateway's request id. Give it to support when you ask
about a call. The SDK reads the `Dominaite-Request-Id` response header, and falls back to
`metadata.requestId` in the body. It is None when the gateway sent neither, and on a network
failure.

```python
from dominaite import DominaiteError, request_id_of

try:
    session = client.create_checkout_session(...)
    log.info("session opened, request_id=%s", request_id_of(session))
except DominaiteError as error:
    log.warning("checkout failed, request_id=%s", error.request_id)
```

A result is still a plain `dict` (or `list`); `request_id_of(result)` reads the id off it.
`dict(result)` makes a copy without the id. Calls that answer 204 return None, so they have no
id to carry.

### Webhook verification codes

`WebhookVerificationError.error_code` is one of `MALFORMED_SIGNATURE` (wrong header, or a proxy
rewrote it), `INVALID_SIGNATURE` (wrong secret, modified body, or you passed a re-serialized
body instead of the raw one), `TIMESTAMP_OUT_OF_RANGE` (replay, or your clock drifted), and
`INVALID_PAYLOAD` (signed, but not a JSON object).

## Rate limits

60 requests per minute per API key, and 120 per minute per IP address. Both are sliding
windows, and the IP limit is shared by every key sending from that address, so a busy host can
trip it while each key is well inside its own budget.

Going over gets you a `RateLimitError`. The SDK does **not** retry it for you, not even under the
client retry policy - answering "you are sending too much" with more traffic is
how a short spike turns into a sustained lockout. Wait, then send again:

```python
from dominaite import RateLimitError

try:
    session = client.create_checkout_session(...)
except RateLimitError as limit:
    time.sleep(limit.retry_after_seconds or 60)
```

`retry_after_seconds` is what the API asked you to wait, and honouring it is the shortest wait
that will work. It is `None` when the API did not give a number of seconds - back off on your
own schedule then.

`error_code` is `RATE_LIMIT_EXCEEDED` for the two limits above. A charge has two more limits,
each with its own code. Nothing was charged in either case:

- `CHARGE_VELOCITY_EXCEEDED`: the card already has the most charges allowed in 24 hours, or
  the charge would take you past your hourly limit for the currency.
- `CHARGE_DECLINE_RATE_EXCEEDED`: the key collected too many declined charges in a short time.
  Its charge route is locked for a while. Other routes keep working.

Many failed authentication attempts from one IP address in a short time block that address
for a while. Every request from it then gets a 429 with no `Retry-After`. Fix the credentials
before you send again, or the next failures start a new block.

The usual cause is polling `get_status` in a tight loop. Poll after the payer returns to you, or
on your order timeout, and let webhooks do the rest.

## Field lengths

`order_reference` and `idempotency_key` are capped at 100 characters each. For
`order_reference` that is characters, not bytes: a 100-character Cyrillic or Greek reference is
200 UTF-8 bytes and the platform takes it.

`idempotency_key` travels as an HTTP header and is part of the signature, so it is limited to
visible ASCII: letters, digits and punctuation, no spaces, no accented or non-Latin letters.
Anything else raises `ValueError` before the request is sent. If your order ids are not ASCII,
derive the key from something that is (your numeric order id, or a hash of the reference).

## Integration contract

`get_integration_contract()` reads the public contract of the API: the request fields, the
statuses, the error codes, the webhook events and the signing recipe that the API uses now.

1. The route is anonymous. The request goes out without a signature, so a success does not
   check your credentials. Use `ping()` for that.
2. Read accepted values from it, for example `recurring.consentTextVersions` for `consent`.

## Fields the SDK does not model yet

The API can accept a request field before this SDK has an argument for it. Send it in `extra`:

```python
session = client.create_checkout_session(..., extra={"newField": "value"})
```

1. `create_checkout_session`, `charge_payment_method`, `create_refund`, `create_plan`,
   `update_plan`, `create_agreement` and `create_webhook_endpoint` take `extra`. They send each
   key as given, in the signed body.
2. A key in `extra` replaces the same field that an argument set.
3. The API ignores a field that it does not know, so a misspelled name is not an error. Check
   the name in the API reference.

## Running the tests

```bash
python -m venv .venv && .venv/bin/pip install pytest
.venv/bin/python -m pytest
```

`tests/test_signing.py` reproduces the signing test vector published on the dashboard's
Website-integration tab. If it ever fails, the SDK cannot authenticate - fix the signing, never
the expected value.

`tests/test_webhooks.py` pins the cross-SDK webhook vector: the same secret, timestamp and
body bytes every Dominaite SDK verifies against. A failure there means the SDK is rejecting
genuine deliveries or accepting forged ones. Same rule - fix the code, not the vector.

## Versioning and support

1. The SDK is on 0.x. A minor release (0.6 to 0.7) can contain a breaking change. A patch
   release (0.6.0 to 0.6.1) does not. `CHANGELOG.md` marks every breaking change.
2. Pin the minor version in your requirements, for example `dominaite>=0.6,<0.7`, and read
   the changelog before you move to the next minor.
3. The SDK supports Python 3.9 and later, and it has no runtime dependencies.
4. The API can send a `Dominaite-Sdk-Notice` response header when your SDK version is
   deprecated. The SDK passes it to `on_warning` once per distinct value per client, or emits a
   `DeprecationWarning` when `on_warning` is not set (see "Logging and warnings"). When you
   get the notice, upgrade to the latest release.
