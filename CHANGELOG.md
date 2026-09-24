# Changelog

## 1.0.0

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

### Fixed

- `create_checkout_session_with_retry` retries a `PAYMENT_PROCESSING_UNAVAILABLE` refusal with
  the same key, as the API contract says to.
