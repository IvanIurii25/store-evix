# Delivery subsystem — own logistics + Nova Post carrier

> **TL;DR.** The shop delivers two ways. **Own logistics** — pickup (free) and a
> flat-rate courier — has no external dependency and always works. **Nova Post**
> is an env-gated carrier: it quotes each shipment over its API, ships to a
> branch, a postomat or the customer's door, and is **off unless configured**.
> A delivery method is the pair `(delivery_service, delivery_type)`.

This document is the authoritative reference for how delivery works. Any agent
touching checkout money, carrier code or the delivery UI should read it first.

⚠️ **The Nova Post endpoint contract is reconstructed from `ecom-obr`'s
implementation, not from official documentation.** Field names, response shapes
and the division-category vocabulary are educated guesses until they are checked
against the real API and a live sandbox. Treat §4 as provisional.

---

## 1. Methods

| `delivery_service` | `delivery_type` | What it is | Price |
|---|---|---|---|
| `own` | `pickup` | Collect from the store | 0 |
| `own` | `courier` | Our own courier | `COURIER_RATE` (flat) |
| `novapost` | `branch` | Carrier office | quoted per shipment |
| `novapost` | `postomat` | Carrier parcel locker | quoted per shipment |
| `novapost` | `courier` | Carrier courier to the door | quoted per shipment |

Any other pair is rejected with `422 invalid_delivery_method`. The pair — not the
type alone — identifies a method: `courier` means two different things depending
on the service.

`GET /api/v1/delivery/methods` is the **single source of truth** for what the
storefront may offer, including the free-delivery thresholds and the carrier's
courier address fields. The front-end renders what it is told; enabling a
category is a back-end change.

## 2. Configuration

```
# Off (default): pickup + own courier only, carrier endpoints closed.
NOVAPOST_MODE=                  # "" | stub | sandbox | live
NOVAPOST_API_TOKEN=             # apiKey from the carrier
NOVAPOST_CONTRACT_NUMBER=       # payerContractNumber
NOVAPOST_SENDER_DIVISION_ID=    # branch parcels are handed over at
NOVAPOST_SENDER_NAME/PHONE/EMAIL/COMPANY=
NOVAPOST_DIVISION_CATEGORIES=branch,postomat,courier
NOVAPOST_FREE_DELIVERY_FROM=    # falls back to FREE_DELIVERY_FROM

# Parcel metrics (shared by any carrier)
PARCEL_DEFAULT_ITEM_WEIGHT_G=500
PARCEL_WIDTH_MM / PARCEL_LENGTH_MM / PARCEL_HEIGHT_MM
PARCEL_VOLUMETRIC_DIVISOR=5000  # cm³ per kg, from the carrier's tariff
```

`settings.novapost_enabled` is true when the mode is `stub`, or when a real mode
has both a token and a sender branch. **A half-filled config stays off** rather
than failing at a customer's checkout.

### `stub` mode

`NOVAPOST_MODE=stub` swaps the HTTP client for
`app/services/delivery/novapost_stub.py`: three Moldovan cities, five pickup
points, a weight-and-distance tariff, synthetic waybills. It exists so the
feature could be built and tested before the merchant contract — every delivery
test runs against it.

**The settings validator refuses `stub` outside `local`/`test`**, so a fake
carrier can never quote a real customer.

## 3. Money path

```
POST /checkout/quote ─┐
                      ├─→ CheckoutService._quote_from_lines
POST /checkout      ──┘        │
                               ├─ own      → flat rate / free over threshold
                               └─ novapost → threshold check FIRST,
                                             then NovaPostService.quote()
```

Rules that are load-bearing:

1. **The client never sets the price.** Both endpoints recompute it; a
   `delivery_cost` in the request body is ignored.
2. **The free-delivery threshold is measured on `subtotal − discount_total`**
   (clamped at 0) — what the customer actually pays for goods. A coupon that
   drops the payable below the threshold brings the charge back.
3. **The threshold is checked before calling the carrier.** A free shipment
   needs no quote.
4. **Fail-closed.** If the carrier cannot price the shipment, checkout raises
   `502 delivery_quote_unavailable` and **no order is created**. There is no
   fallback price: quoting a number the carrier never agreed to sells delivery
   at a loss. Own methods stay available, so the customer is never stuck.
5. **`order_delivery_np.calculated_cost` is NULL when the threshold applied** —
   we never asked for a price, and 0 would claim the shipment is free for us.

### Parcel weight

`max(actual, volumetric)`, where actual is `Σ (variant.weight_g ?? product.weight_g
?? PARCEL_DEFAULT_ITEM_WEIGHT_G) × qty` and volumetric comes from the configured
box. `ecom-obr` sets the two equal, which under-declares bulky-but-light orders —
the difference returns as a carrier invoice.

Product weights are nullable on purpose: NULL means "not entered", which stays
distinguishable from "really light". `GET /admin/products?no_weight=true` lists
what is still missing; `scripts/backfill_weights.py` fills defaults per category.

## 4. Carrier API (provisional — see the warning above)

`app/services/delivery/novapost_client.py`, base URL per mode
(`sandbox` → `api-stage.novapost.pl/v.1.0`, `live` → `api.novapost.com/v.1.0`).

| Method | Endpoint |
|---|---|
| `settlements` | `GET /settlements?countryCodes[]&textSearch` |
| `divisions` | `GET /divisions?settlementIds[]&divisionCategories[]&textSearch` |
| `division_by_id` | `GET /divisions?ids[]` |
| `calculate` | `POST /shipments/calculations` |
| `create_shipment` | `POST /shipments` |
| `tracking` | `GET /shipments/tracking?numbers[]` |
| `delete_shipment` | `DELETE /shipments/{id}` |

* Auth: `GET /clients/authorization?apiKey=` returns a JWT, **cached in Redis**
  (`np:jwt`, 50 min) and shared across requests and workers. A 401 re-mints it
  once and retries.
* `Accept-language` controls the locale of settlement and branch names — this is
  what makes the ru/ro snapshots possible.
* Row lists are read from `data` **or** `items`: the reference implementation
  uses both, and until the contract is confirmed, accepting either beats
  silently returning nothing.
* Every failure becomes `NovaPostError`; no `httpx` exception escapes.

## 5. Reference lookups

`POST /api/v1/delivery/novapost/settlements` and `.../divisions` back the
checkout pickers. Both are public (guests check out too), **rate-limited per IP**
(`RATE_LIMIT_DELIVERY`, default 60/60) and **cached in Redis** — settlements 24 h,
divisions 6 h, keyed per language. Without a cache a typeahead would turn our
public endpoint into a free proxy for a third-party API.

Cache failures are logged and ignored: a Redis outage costs the cache, not the
feature. While the carrier is off both endpoints return `404 novapost_disabled`.

## 6. Order data

`order.delivery_service` + `order.delivery_type` on every order. Carrier orders
additionally get one `order_delivery_np` row (1:1, `ON DELETE CASCADE`):

* destination **snapshot** — settlement name (in the order's language and in
  Russian), division number and address, or the courier `address_parts` JSON;
* `calculated_cost` — what the carrier quoted;
* `awb_id` / `awb_number` / `awb_data` — the waybill (phase P4);
* `status_code` / `status_text` / `status_updated_at` — tracking (phase P5),
  stored as separate fields so they can be filtered, unlike the reference's
  single formatted string.

The snapshot deliberately duplicates data the carrier could return: an order has
to stay readable years later, after a branch is renamed or the integration is
switched off. It is resolved **before** the order transaction opens and degrades
to empty names on failure — a third-party read must never roll back a committed
order.

`OrderOut.novapost` exposes the block on every read path. **The guest lookup
(`POST /orders/{number}/lookup`) withholds `awb_number`**: number + email is a
weak credential and must not hand out a parcel tracking key.

## 7. Waybills (back-office)

`POST` / `DELETE /admin/orders/{number}/np/shipment`, staff-only.

* **Creation is idempotent.** The carrier row is locked `FOR UPDATE` for the
  whole operation, so two operators clicking at once cannot buy two shipments:
  the second waits, sees the number the first wrote, and gets `409
  awb_already_exists`.
* **A refused creation writes nothing** — claiming a waybill exists when it does
  not is worse than a retry. A refused cancellation **keeps** our copy of the
  number, so a live shipment is never hidden.
* **Cancelling an order cancels its waybill first, fail-closed.** If the carrier
  refuses, the order is not cancelled either (`502 waybill_cancel_failed`): our
  records saying "cancelled" while the parcel still travels is the expensive
  failure.
* The payload declares the **snapshotted** `parcel_weight_g` and names the
  recipient from `order.delivery_name` (collected as `np_recipient_name` at
  checkout — the carrier's address fields carry no name). Payer is the contract
  when `NOVAPOST_CONTRACT_NUMBER` is set, otherwise the sender.

## 8. Tracking and notifications

`novapost.sync_statuses` (Celery beat, every 30 min) polls the carrier for every
**open** waybill and stores `status_code` + `status_text` + `status_updated_at`.

* A delivered / returned / lost / cancelled parcel is never asked about again —
  otherwise the sweep would grow with order history forever.
* An unchanged answer writes nothing, so `status_updated_at` keeps meaning "when
  it actually moved".
* A carrier outage costs that run only: rows stay open and are retried.
* The task no-ops when the carrier is off.

Creating a waybill enqueues `novapost.waybill_email` — the customer's tracking
number plus the pickup point. Enqueued **after** the commit and swallowed on
failure: a broker hiccup must not fail an operation whose shipment already
exists at the carrier.

## 9. What is not built yet

* Nothing has ever talked to the real carrier: no credentials, no sandbox run,
  so §4 remains provisional.
* The storefront shows the tracking status but does not link to the carrier's
  tracking page (needs the real URL format).

## 10. Testing

`tests/delivery/` (stub behaviour, config gates, transport via
`httpx.MockTransport`, the public lookups) and
`tests/checkout/integration/business/test_novapost_checkout.py` (the money path:
pricing, thresholds, validation refusals, outage → no order, snapshots, AWB
hiding). All of it runs against the stub — no network, no credentials.
