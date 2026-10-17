# Changelog

## 0.7.0 (unreleased)

### Breaking

- The client retries by default. Every GET, and every POST with an idempotency key, is retried
  up to twice on a network failure, a 5xx or `PAYMENT_PROCESSING_UNAVAILABLE`, with full-jitter
  backoff (0 to 0.5 s times 2^n, capped at 8 s). Pass `max_retries=0` to send every call once.
  A 429, `CHARGE_OUTCOME_UNKNOWN`, `CHARGE_FAILED` and `PAYMENT_METHOD_CHARGES_DISABLED` are
  never retried.
  `create_webhook_endpoint` is never retried either: a replay answers without the secret.
- `timeout` is a socket timeout on the connect and on each read, not a cap on one attempt.
  Each retry starts with the full value, so one call can take `(max_retries + 1) * timeout` plus
  the waits (just under 137 seconds with the defaults), or longer when a server sends its answer
  slowly.
- A 5xx whose body is not JSON (for example an HTML 502 from a proxy) raises `TransportError`.
  It was an `ApiError`.
- `TransportError.__init__` takes `(message, http_status=None, error_code=None, result=None, *,
  request_id=None)`. Code that builds a `TransportError` with more than the message, in a test
  double for example, must pass those by name.
- `create_checkout_session_with_retry` waits with full jitter, capped at 8 s: a random time
  between 0 and `min(8, backoff_seconds * 2**n)`. It waited exactly `backoff_seconds * 2**n`.
- A coded 404 on a charge (`PAYMENT_METHOD_NOT_FOUND`, `AGREEMENT_NOT_FOUND`) is a
  `ChargeError`. It was an `ApiError`.
- `STOREFRONT_MISMATCH` on a checkout replay (the 200 shape) is a `StorefrontError` with
  `http_status` 200. It was a `CheckoutRefusedError`.
- A 5xx keeps the gateway's code: `TransportError` now has `error_code`, `http_status` and
  `result`. Code that only caught `TransportError` keeps working.
- A currency that is not three upper-case letters raises `ValueError` before anything is sent.
- The payment method id is lower-cased before it is signed and sent, so `PM_...` works.
- The retry warning names the method and the path:
  `dominaite-python: retrying POST /merchant-api/checkout/sessions after HTTP 503 (attempt 2 of 3)`.

### Added

- Request ids. Every result and every error carries the gateway's request id: read
  `error.request_id`, or `request_id_of(result)` on a result. The SDK reads the
  `Dominaite-Request-Id` header first, then `metadata.requestId` in the body. The debug line
  carries it too.
- The client keywords `max_retries`, `sleep` and `random` (the last two for tests).
- `ErrorCode` names every code the gateway answers: subscription refusals, charge codes, plan and
  agreement codes, webhook endpoint codes, generic codes and field validation codes. New lists:
  `RECURRING_ERROR_CODES`, `WEBHOOK_ERROR_CODES` and `FIELD_VALIDATION_CODES`.
  `CHARGE_ERROR_CODES` holds the new charge codes.
- `SUBSCRIPTION_REFUSAL_ERROR_CODES`: the `AGREEMENT_*` codes and `SUBSCRIPTIONS_NOT_ENABLED`
  that refuse a checkout session with `agreement_id`. They raise `CheckoutRefusedError`, like
  the codes in `SESSION_REFUSAL_ERROR_CODES`. That list stays the five codes the integration
  contract publishes.
- `WebhookEventType` names every webhook event, with the lists `PAYMENT_WEBHOOK_EVENTS`,
  `RECURRING_WEBHOOK_EVENTS` and `VERIFICATION_WEBHOOK_EVENTS`.
- `ChargeEventData.storedPaymentMethodId` is `Optional[str]`: the gateway can send null.

### Deprecated

- `create_checkout_session_with_retry`. Call `create_checkout_session`: the client retry policy
  now covers it. The helper still works and warns with a `DeprecationWarning` on each call.

## 0.6.0

### Breaking

- The base URL is required. The client no longer falls back to production when you leave it
  out, so an unset environment variable cannot send sandbox credentials to production.
  Construction fails with `ValueError` when the base URL is missing, empty or whitespace. Pass
  `SANDBOX_BASE_URL` (`https://api.staging.dominaite.com/payments`) or `PRODUCTION_BASE_URL`
  (`https://api.dominaite.com/payments`), or your own value.
- `DEFAULT_BASE_URL` is removed. Use `PRODUCTION_BASE_URL`.

### Added

- The User-Agent is now `dominaite-python/<version> (python <version>)`, then your suffix. The
  new keyword `user_agent` sets the suffix. A suffix with a control character raises
  `ValueError`.
- Warning hook: the keyword `on_warning` takes a callable. Without it the SDK calls
  `warnings.warn`. It reports a deprecation notice from the API, a retry in
  `create_checkout_session_with_retry`, and a ping clock skew over 60 seconds.
- SDK notice: the client reads the `Dominaite-Sdk-Notice` response header on every response,
  errors included, and warns once per distinct value. A missing header does nothing.
- Debug log: the keyword `on_debug`, off by default, gets one line per request with the
  method, path, status, time in ms, the `Dominaite-Request-Id` header and the idempotency key.
  No warning or debug line carries the secret, the signature, a cashier token or a body.

## 0.5.2

### Added

- Billing address on the payer: the `customer` keys `address`, `city`, `postalCode` and `state`,
  all optional. Address is one line, at most 200 characters; city at most 100; postal code at
  most 20. State is the state or province code, at most 50 characters, and only used for the
  United States, Canada and Australia. The widget does not ask the payer for a field that you
  send. A value that is too long fails with 400 `VALIDATION_ERROR`, code `MAX_LENGTH`.
  The SDK already sent `customer` as given; the docstring and the README now document the keys.

## 0.5.1

### Added

- `Transaction` names `fee`, `netAmount`, `settledAt` and `payoutId`. They are absent until the
  acquirer settles the payment. `fee` and `netAmount` are in minor units, `settledAt` is the
  settlement date at midnight UTC, and `payoutId` is set once the money is paid out.
- `list_transactions` takes `settled_from` and `settled_to`, a settlement window on the
  settlement date. They are dates (`date` or `"2026-10-13"`). The SDK refuses a `datetime`
  before sending. `from_` and `to` are still required.

## 0.5.0

### Added

- Wallet reporting fields: `paymentMethod` and `walletType` on the `get_status` response, passed
  through as sent. `paymentMethod` is `card`, `wallet`, `bank_transfer` or `sepa`, `None` while
  the payment is still open and on older transactions; `walletType` names the wallet, `None` for
  non-wallet payments, and an unknown lower-cased value is a valid wallet. Reporting data only:
  a wallet payment refunds, captures and disputes like a card. `PaymentMethodCategory`,
  `PAYMENT_METHOD_CATEGORIES`, `WalletType` and `WALLET_TYPES` are exported.
- Contract fixtures: `paymentMethod` and `walletType` in the status read fields and examples, and
  the `wallets` block in the wire contract, pinned against `WALLET_TYPES`.
- `ApiError.result`, on every subclass too: the whole JSON body the API sent. Read
  `result["error"]["validationErrors"]` for the fields a 400 refused, and `params` where a code
  carries them.
- `AUTHORIZATION_ERROR_CODES`, and the `ErrorCode` members `INSUFFICIENT_SCOPE`,
  `VERIFY_ONLY_NOT_ENABLED`, `RATE_LIMIT_EXCEEDED`, `CHARGE_VELOCITY_EXCEEDED` and
  `CHARGE_DECLINE_RATE_EXCEEDED`.
- Contract fixture: the new status read, checkout session and charge fields, with examples.
- Wire contract fixture: the `authorization` error group (`INSUFFICIENT_SCOPE`, HTTP 403), pinned
  against `AUTHORIZATION_ERROR_CODES`.
- `create_checkout_session` takes `verify_only`, `agreement_id`, `consent`, `location_id` and
  `extra`. `verify_only=True` saves a card without a payment and sends `amount` 0; we switch it
  on for your account. Every other session still refuses an amount below 1 before sending.
  `agreement_id` with `consent` makes a subscription session. `extra` sends request fields that
  this version does not model, inside the signed body.
- `CheckoutSession`, `CheckoutStatus` and `CheckoutAgreement` `TypedDict`s. They name the keys
  of the session and of the status read, `verifyOnly`, `agreementId`, `declineClass`,
  `isRecurring`, `agreement` and `sequence` included. The methods still return plain dicts.
- `PaymentEventData` names `verifyOnly` and `declineClass`. A `verification.*` event carries
  the same data, with `verifyOnly` True. `declineClass` can be None, for example when nobody
  declined the payment.
- `charge_payment_method` takes `agreement_id` and `extra`. A charge with `agreement_id` bills
  that agreement. The gateway checks the charge against it and refuses one that does not fit
  with a 422 `ChargeError`, for example `AGREEMENT_PERIOD_ALREADY_CHARGED`. The returned charge
  carries `agreementId`, `None` on a charge outside an agreement, and `sequence` as the gateway
  sends it.
- `list_refunds(transaction_id)` returns a `RefundList`: the refunds of a payment made through
  this API, newest first, each a `Refund` with every key, plus `refundedAmount` and
  `refundableAmount`. `create_refund` takes `extra`.
- `get_integration_contract()` reads the public integration contract of the API. The route is
  anonymous, so the request goes out without a signature. `CONTRACT_PATH` is exported.
- Subscriptions: `create_plan`, `list_plans`, `get_plan`, `update_plan` and `delete_plan` (which
  archives the plan), and `create_agreement`, `get_agreement` and `cancel_agreement`. They
  return the `Plan` and `Agreement` `TypedDict`s. A create needs an idempotency key and takes
  `extra`. `update_plan` sends no key, and `trial_days=0` and `cancel_url=""` reach the API.
  Plan and agreement ids keep their case. `PLANS_PATH` and `AGREEMENTS_PATH` are exported.
- Webhook endpoints: `create_webhook_endpoint`, `list_webhook_endpoints` and
  `delete_webhook_endpoint`, with the `WebhookEndpoint` `TypedDict`. They need a key with the
  `webhooks:manage` scope. `secret` is in the first answer of a create only. The SDK sends event
  names as given. `WEBHOOKS_PATH` is exported.
- `list_transactions(from_, to, status, limit, cursor)` returns a `TransactionPage`: the card
  payments created in a window, oldest first, and `nextCursor` for the next page. It needs a key
  with the `transactions:read` scope. The query goes in the URL, and the signature covers the
  path only. `Transaction`, `TransactionPage` and `TRANSACTIONS_PATH` are exported.

### Changed

- A 403 is no longer reported as a credential failure. `AuthenticationError` has `http_status`
  (401 or 403) and `result`. Only a 401 keeps the "check your key id, secret, and server clock"
  hint. A 403 `INSUFFICIENT_SCOPE` keeps the gateway's message, which names the missing scope. A
  403 `VERIFY_ONLY_NOT_ENABLED` says that we switch it on for your account.
- `RateLimitError.error_code` falls back to `RATE_LIMIT_EXCEEDED`, the gateway's own code, when
  a 429 carries none (it was `RATE_LIMITED`). A 429 whose body is not JSON is now a
  `RateLimitError` too, with its `Retry-After`.

### Fixed

- The docs no longer promise a `declineClass` on every decline. It can be None, for example when
  nobody declined the payment.

## 0.4.0

### Added

- Card fields: `integration` on `create_checkout_session` (`"widget"` or `"fields"`, left out of
  the body when None), and `integration` and `clientSecret` on the returned session.
  `clientSecret` is set only for fields sessions. `CheckoutIntegration` and
  `CHECKOUT_INTEGRATIONS` are exported. Card fields are enabled per merchant on request; see the
  README.
- Contract fixture: `integrationVocabulary`, the two new checkout fields and a fields session
  example.
- Processor reference: `pspReference` on the `get_status` response and on `PaymentEventData`. The
  payment processor's reference for the transaction, `None` until known and on a sale settled by
  reconciliation without a processor webhook. Refund and cancel events carry the original sale's
  reference. Not on `charge.*` events.
- Contract fixture: `pspReference` in the status read fields and examples.

## 0.3.1

### Added

- `WebhookEvent`, `AgreementEventData`, `ChargeEventData` and `ChargeEventPaymentMethod`
  `TypedDict`s for webhook payloads. `WebhookEvent` has the optional `apiVersion` (the dated
  payload version, `2026-09-25` today); the agreement and charge data types have the optional
  `sequence` that orders deliveries per object. `verify_webhook` still returns a plain dict,
  and payloads without these fields verify and parse as before.
- `create_refund(transaction_id, amount=None, reason=None, idempotency_key=...)` and
  `get_refund(transaction_id, refund_id)` for `POST` and `GET`
  `/merchant-api/payments/{transactionId}/refunds`. The idempotency key is required and signed,
  like a charge's; leaving `amount` out refunds everything still refundable and sends no
  `amount` key. Both return a `Refund` with every field present (absent reads as None).
  `RefundStatus`, `REFUND_STATUSES`, `REFUND_ERROR_CODES`, `REFUND_FAILURE_CODES` and
  `PAYMENTS_PATH`.
- `RefundError` (a subclass of `ApiError`) for the codes the refund routes answer with, with
  `retryable` and `retry_stop_after_seconds` (`REFUND_NOT_FOUND` 60 seconds,
  `DUPLICATE_REQUEST` 120 seconds, both with the same key; also `REFUND_RETRY_WINDOWS_SECONDS`).
  A 5xx stays a `TransportError`.
- `PaymentEventData` for `payment.*` webhook data, including `storedPaymentMethod`: the card a
  `save_card` payment kept on file, on `payment.succeeded` and `payment.requires_capture`. It
  can be None even when a card was saved; `get_status()` stays the source of truth.
- `StoredPaymentMethod`, the one type for the card on file on `get_status()` and on payment
  webhooks.
- Contract fixture refreshed with the refund endpoints and vocabularies.

## 0.3.0

### Breaking

- `idempotency_key` is required on `create_checkout_session`, `create_checkout_session_with_retry`
  and `charge_payment_method`. The SDK no longer generates a random key; a missing or empty key
  raises `ValueError` before any request is sent.
- An idempotency key must be 1 to 100 visible ASCII characters. Non-ASCII keys used to pass the
  local check and then fail in the HTTP layer or the signature check; they now raise
  `ValueError` up front.
- A create call refused for its storefront raises `StorefrontError` instead of a plain
  `ApiError`. It subclasses `ApiError`, so `except ApiError` still catches it.

### Migration

Pass `idempotency_key=order_idempotency_key("checkout", order_id, amount, currency)` on every
create call, and a key derived from the billing period on every charge. The same order at the
same amount then replays the same session, and a changed amount gets a new one.

### Added

- `order_idempotency_key(scope, order_id, amount_minor, currency)` builds
  `{scope}-{order_id}-{amount_minor}-{CURRENCY}`.
- `to_minor_units(amount, currency)` converts a decimal string or `Decimal` using the gateway's
  decimals per currency (`CURRENCY_EXPONENTS`). HUF is 0 decimals there, unlike ISO 4217.
  ISK, KRW, OMR, JOD and TND raise (`UNSUPPORTED_CURRENCIES`).
- `is_paid`, `is_terminal` and `TERMINAL_PAYMENT_STATUSES`.
- `ErrorCode` enum, `STOREFRONT_ERROR_CODES` and `StorefrontError` for
  `STOREFRONT_NOT_WHITELISTED` (409), `STOREFRONT_INACTIVE` (409) and `STOREFRONT_MISMATCH` (400).
  `STOREFRONT_ERROR_CODES` is now in the contract's order.
- Saved cards can be `retired`: the platform stopped the card on its own and it never becomes
  active again. `StoredPaymentMethodStatus.RETIRED`, plus `StoredPaymentMethodRetiredReason` and
  `STORED_PAYMENT_METHOD_RETIRED_REASONS` (`hard_decline`, `chargeback`, `source_sale_reversed`).
  `get_status()["storedPaymentMethod"]["retiredReason"]` is always present, None unless retired.
- Contract fixtures refreshed from the gateway (contract version 2026-09-16).

### Fixed

- `create_checkout_session_with_retry` retries a `PAYMENT_PROCESSING_UNAVAILABLE` refusal with
  the same key, as the API contract says to.
