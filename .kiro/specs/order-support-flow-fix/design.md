# Order Support Flow Fix — Bugfix Design

## Overview

Four defects in `worker.py` and `order_lookup.py` prevent the order-support voice agent from
completing its 6-step flow (collect phone → look up user → confirm name → show orders → pick
order → speak status). The fix is surgical: reorder the guard logic in `worker.py` so the DB
lookup always runs on the first call and session state is populated before returning, and add a
phone-based cross-DB fallback in `order_lookup.py` so orders are found even when the
`farmer_engagement` user_id does not match the `vikrayasetu` user_id.

---

## Glossary

- **Bug_Condition (C)**: The set of inputs that trigger one of the four defects.
- **Property (P)**: The desired output for any input in C after the fix is applied.
- **Preservation**: All behaviours that must remain identical for inputs outside C.
- **`get_order_status_from_db`**: The `@llm.function_tool` in `OrderSupportAgent` (`backend/worker.py`) that the LLM calls to drive the conversation flow.
- **`get_order_status`**: The async method on `OrderLookupService` (`backend/order_lookup.py`) that performs the actual DB queries.
- **`list_active_orders_for_user`**: Queries `transactions.orders` by `user_id`; returns empty when the `farmer_engagement` user_id differs from the `vikrayasetu` user_id.
- **`list_orders_for_phone_direct`**: Queries `transactions.orders` by the phone column (`ORDERS_PHONE_COL`); used as the cross-DB fallback.
- **`_pending_phone`**: Session-level field on `OrderSupportAgent` that caches the normalised phone after the first lookup.
- **`_pending_customer`**: Session-level field on `OrderSupportAgent` that caches the resolved customer dict after the first lookup.
- **`ORDERS_PHONE_COL`**: Environment variable naming the phone column in `transactions.orders`; required for the cross-DB fallback.

---

## Bug Details

### Bug Condition

The four defects share a common root: the early guard in `get_order_status_from_db` fires
**before** `ORDER_LOOKUP.get_order_status()` is called, so `_pending_customer` and
`_pending_phone` are never populated on the first call. A separate structural defect causes
orders to be invisible when the two databases use different user_id namespaces.

**Formal Specification:**

```
FUNCTION isBugCondition_EarlyGuard(call)
  INPUT: call — a get_order_status_from_db invocation
  OUTPUT: boolean

  RETURN call._pending_customer IS NULL
     AND call.customer_confirmed = false
     // Guard fires before DB is queried → _pending_customer stays NULL forever
END FUNCTION

FUNCTION isBugCondition_CrossDbMismatch(lookup)
  INPUT: lookup — a user resolved from farmer_engagement queried against vikrayasetu
  OUTPUT: boolean

  RETURN lookup.farmer_engagement_user_id ≠ lookup.vikrayasetu_user_id
     AND ORDERS_PHONE_COL IS NOT NULL   // fallback is possible but not attempted
END FUNCTION
```

### Examples

- **Defect 1 (early guard)**: Agent calls `get_order_status_from_db(phone_number="9876543210", customer_confirmed=False)`. Guard sees `_pending_customer is None` and returns `confirmation_required` with message "Ask the caller for their 10-digit phone number first" — DB is never queried, `_pending_customer` stays `None`.
- **Defect 2 (broken confirmation)**: Agent calls `get_order_status_from_db(phone_number="9876543210", customer_confirmed=True)` after caller speaks name. Guard sees `_pending_customer is None` (never set due to Defect 1) and returns `confirmation_required` again — infinite loop.
- **Defect 3 (lost phone)**: Agent calls `get_order_status_from_db(customer_confirmed=True)` without repeating `phone_number`. `_pending_phone` is `None` (never set), so `effective_phone` is empty → `get_order_status` returns `missing_input`.
- **Defect 4 (cross-DB mismatch)**: Customer exists in `master.users` with `user_id = "fe-uuid-123"`. Their orders in `transactions.orders` have `user_id = "vs-uuid-456"`. `list_active_orders_for_user("fe-uuid-123")` returns `[]` → agent says "no active orders" even though orders exist.
- **Defect 5 (null display id)**: `order_for_caller` sets `"external_order_id": eid or None`. When `display_order_id_for_ui` returns `""`, the key is `None` in the summary dict, so the UI panel shows `external_order_id: null`.

---

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- `customer_not_found` is returned when the phone number has no match in `master.users`.
- `multiple_users` is returned when the phone matches more than one user record.
- `no_active_orders` is returned when a confirmed customer genuinely has no active orders (after the cross-DB fallback also returns empty).
- Single-order customers receive the order status directly without an order-selection prompt.
- `order_selection_required` is returned when a confirmed customer has more than one active order.
- `order_not_found` is returned when the caller supplies an `external_order_id` that does not match any active order.
- The STT empty-transcript fallback continues to emit `STT_EMPTY_FALLBACK_USER_TEXT`.
- The greeting fires on session start in the selected language.

**Scope:**
All inputs that do NOT satisfy `isBugCondition_EarlyGuard` or `isBugCondition_CrossDbMismatch`
must produce exactly the same result as the original code. This includes:
- Calls where `_pending_customer` is already set (subsequent turns).
- Calls where `customer_confirmed=True` and the name check passes normally.
- Calls with `external_order_id` set (order-selection step).
- All non-order-lookup paths (STT, TTS, VAD, greeting).

---

## Hypothesized Root Cause

1. **Guard placed before DB call (Defects 1, 2, 3)**: In `get_order_status_from_db`, the block
   ```python
   if customer_confirmed:
       if not self._pending_customer or not same_phone or not is_affirmative:
           return { "reason": "confirmation_required", ... }
   ```
   runs unconditionally before `ORDER_LOOKUP.get_order_status()`. On the very first call
   `_pending_customer` is `None`, so the guard always fires and the DB is never reached.
   Because the DB is never reached, `_pending_phone` and `_pending_customer` are never stored,
   breaking every subsequent call.

2. **State stored only after DB call (Defect 3)**: The lines
   ```python
   if result.get("reason") == "confirmation_required":
       self._pending_phone = ...
       self._pending_customer = result.get("customer")
   ```
   appear after the early return, so they are unreachable on the first call.

3. **Cross-DB user_id namespace mismatch (Defect 4)**: `resolve_users_by_phone` queries
   `master.users` in the `farmer_engagement` database and returns its `user_id`. That id is
   then passed to `list_active_orders_for_user`, which queries `transactions.orders` in the
   `vikrayasetu` database. The two databases assign independent `user_id` sequences, so the
   lookup returns zero rows. The existing `list_orders_for_phone_direct` method can bypass this
   by querying `transactions.orders` directly by phone, but it is never called as a fallback.

4. **Null display id in summary (Defect 5)**: `order_for_caller` uses `eid or None`, which
   converts an empty string to `None`. The summary dict then contains `"external_order_id": None`,
   which the UI renders as `null`.

---

## Correctness Properties

Property 1: Bug Condition — First-Call DB Lookup Populates Session State

_For any_ call to `get_order_status_from_db` where `_pending_customer` is `None` and
`customer_confirmed=False` (i.e., `isBugCondition_EarlyGuard` holds), the fixed function SHALL
query `ORDER_LOOKUP.get_order_status()`, store the resolved customer in `_pending_customer` and
the normalised phone in `_pending_phone`, and return `confirmation_required` with a non-`None`
`customer` field so the agent can ask the caller to confirm their name.

**Validates: Requirements 2.1, 2.2, 2.3**

Property 2: Bug Condition — Cross-DB Fallback Returns Orders

_For any_ lookup where `list_active_orders_for_user(farmer_engagement_user_id)` returns an
empty list and `ORDERS_PHONE_COL` is configured (i.e., `isBugCondition_CrossDbMismatch` holds),
the fixed `get_order_status` SHALL fall back to `list_orders_for_phone_direct(phone)` and return
those orders, so a customer with active orders in `vikrayasetu` is never told they have none.

**Validates: Requirements 2.4**

Property 3: Preservation — Non-Buggy Inputs Unchanged

_For any_ call where neither `isBugCondition_EarlyGuard` nor `isBugCondition_CrossDbMismatch`
holds, the fixed functions SHALL produce the same result as the original functions, preserving
all existing `customer_not_found`, `multiple_users`, `no_active_orders`, `order_selection_required`,
`order_not_found`, and success paths.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8**

---

## Fix Implementation

### Fix 1 / 2 / 3 — `backend/worker.py`: Rewrite `get_order_status_from_db`

**File**: `backend/worker.py`  
**Method**: `OrderSupportAgent.get_order_status_from_db`

The guard must be restructured so that:
1. The DB lookup always runs first (no early return before `ORDER_LOOKUP.get_order_status()`).
2. `_pending_phone` and `_pending_customer` are stored from the DB result **before** any return.
3. The `customer_confirmed=True` name-check guard only runs when `_pending_customer` is already
   populated (i.e., on the second call, not the first).

**Current (buggy) structure:**
```python
async def get_order_status_from_db(self, phone_number=None, customer_confirmed=False, external_order_id=None):
    effective_phone = phone_number
    if customer_confirmed and self._pending_phone and (not effective_phone or ...):
        effective_phone = self._pending_phone

    if customer_confirmed:                          # ← guard fires BEFORE DB call
        picking_order = bool(external_order_id) and self._pending_customer is not None
        if not picking_order:
            same_phone = ...
            is_affirmative = ...
            if not self._pending_customer or not same_phone or not is_affirmative:
                return { "reason": "confirmation_required", ... }   # ← early return, DB never called

    result = await ORDER_LOOKUP.get_order_status(...)   # ← unreachable on first call

    if result.get("reason") == "confirmation_required":
        self._pending_phone = ...       # ← unreachable on first call
        self._pending_customer = ...    # ← unreachable on first call
    ...
    return result
```

**Fixed structure:**
```python
async def get_order_status_from_db(self, phone_number=None, customer_confirmed=False, external_order_id=None):
    # 1. Resolve effective phone (reuse cached value when caller omits it on follow-up turns)
    effective_phone = phone_number
    if self._pending_phone and (not effective_phone or not str(effective_phone).strip()):
        effective_phone = self._pending_phone

    # 2. ALWAYS call the DB first — this populates _pending_customer on the first call
    result = await ORDER_LOOKUP.get_order_status(
        phone_number=effective_phone,
        customer_confirmed=customer_confirmed,
        external_order_id=external_order_id,
    )

    # 3. Store session state from the DB result BEFORE any guard check or return
    if result.get("reason") == "confirmation_required":
        self._pending_phone = ORDER_LOOKUP.normalize_phone(effective_phone) or self._pending_phone
        self._pending_customer = result.get("customer") or self._pending_customer
    elif result.get("ok"):
        self._pending_phone = None
        self._pending_customer = None

    # 4. Name-confirmation guard — only runs when _pending_customer is already set
    #    (i.e., second call after the first lookup populated it)
    if customer_confirmed and result.get("reason") == "confirmation_required":
        picking_order = bool((external_order_id or "").strip()) and self._pending_customer is not None
        if not picking_order:
            same_phone = (
                ORDER_LOOKUP.normalize_phone(effective_phone)
                == ORDER_LOOKUP.normalize_phone(self._pending_phone)
            )
            customer_name = (self._pending_customer or {}).get("name")
            is_affirmative = self._is_affirmative_confirmation(
                self._latest_user_text, str(customer_name or "")
            )
            if not self._pending_customer or not same_phone or not is_affirmative:
                return {
                    "ok": False,
                    "reason": "confirmation_required",
                    "phone_last10": ORDER_LOOKUP.normalize_phone(effective_phone),
                    "customer": self._pending_customer,
                    "message": (
                        "Please tell me your name to confirm your identity."
                        if self._pending_customer
                        else "Ask the caller for their 10-digit phone number first."
                    ),
                }

    # 5. Publish active order IDs to UI when selection is required
    if result.get("reason") == "order_selection_required":
        await publish_active_order_ids_to_ui(
            result.get("active_orders") or [],
            "These are the external order IDs from your app. Tap or say the one you want.",
        )

    return result
```

**Key differences from original:**
- `result = await ORDER_LOOKUP.get_order_status(...)` is now the **first** substantive statement.
- `_pending_phone` / `_pending_customer` are stored immediately after the DB call.
- The `customer_confirmed` guard is now a **post-DB** check that only re-prompts when the DB
  itself returned `confirmation_required` and the name check fails.
- The `effective_phone` fallback no longer requires `customer_confirmed=True`; it applies
  whenever the caller omits the phone (covers Defect 3).

---

### Fix 4 — `backend/order_lookup.py`: Cross-DB Phone Fallback in `get_order_status`

**File**: `backend/order_lookup.py`  
**Method**: `OrderLookupService.get_order_status`

After `list_active_orders_for_user` returns an empty list, attempt
`list_orders_for_phone_direct` using the original phone number. If that returns results, use
them as the active orders list. The fallback is skipped when `ORDERS_PHONE_COL` is not
configured (preserving existing behaviour for single-DB deployments).

**Location in `get_order_status`** — replace the `no_active_orders` early-return block:

```python
# BEFORE (buggy — returns no_active_orders immediately):
if not active_orders:
    return {
        "ok": False,
        "reason": "no_active_orders",
        ...
    }

# AFTER (fixed — try phone-based fallback first):
if not active_orders and self._orders_phone_col:
    try:
        active_orders = await self.list_active_orders_for_user_by_phone(
            cleaned_phone, limit=50
        )
    except Exception as exc:
        pass  # fallback failed; proceed to no_active_orders below

if not active_orders:
    return {
        "ok": False,
        "reason": "no_active_orders",
        ...
    }
```

The fallback uses a new private helper `list_active_orders_for_user_by_phone` that mirrors
`list_active_orders_for_user` but filters by phone column instead of `user_id`:

```python
async def list_active_orders_for_user_by_phone(
    self, phone_number: str, *, limit: int = 50
) -> list[dict[str, Any]]:
    """Active orders looked up by phone column — cross-DB fallback when user_id namespaces differ."""
    if not self._orders_phone_col:
        return []
    d10 = self.normalize_phone(phone_number)
    if not d10:
        return []
    lim = max(1, min(limit, 50))
    excluded_sql = self._sql_excluded_status_literals()
    query = f"""
        SELECT
            o.order_id::text AS order_id,
            o.order_number::text AS order_number,
            o.external_order_id::text AS external_order_number,
            o.internal_order_id::text AS internal_order_id,
            o.user_id::text AS user_id,
            o.status::text AS status,
            o.payment_status::text AS payment_status,
            o.payment_method::text AS payment_method,
            o.total_amount,
            o.delivered_at,
            o.created_at
        FROM transactions.orders AS o
        WHERE NOT COALESCE(o.is_deleted, false)
          AND COALESCE(o.is_active, true)
          AND RIGHT(
                REGEXP_REPLACE(COALESCE(o.{self._orders_phone_col}::text, ''), '[^0-9]', '', 'g'),
                10
              ) = :d10
          AND LOWER(TRIM(COALESCE(o.status::text, ''))) NOT IN ({excluded_sql})
        ORDER BY o.created_at DESC
        LIMIT :limit
    """
    return await self._fetch_all(self._orders_engine, query, {"d10": d10, "limit": lim})
```

---

### Fix 5 — `backend/order_lookup.py`: Non-Null Display ID in Summary

**File**: `backend/order_lookup.py`  
**Method**: `OrderLookupService.order_for_caller`

Change `"external_order_id": eid or None` to always use the display fallback:

```python
# BEFORE:
eid = self.display_order_id_for_ui(row)
out: dict[str, Any] = {
    "external_order_id": eid or None,
    ...
}

# AFTER:
eid = self.display_order_id_for_ui(row)
out: dict[str, Any] = {
    "external_order_id": eid if eid else str(row.get("order_id", "")),
    ...
}
```

The same pattern applies to the inline summary dicts in `get_order_status` (the
`order_selection_required` and `order_not_found` branches):

```python
# BEFORE (in both summary list comprehensions):
"external_order_id": self.display_order_id_for_ui(o),

# AFTER — already correct; display_order_id_for_ui returns a non-empty string or ""
# The only change needed is in order_for_caller where `eid or None` converts "" to None.
```

---

## Corrected Flow Sequence

```
Caller speaks phone number
        │
        ▼
get_order_status_from_db(phone="9876543210", customer_confirmed=False)
        │
        ├─► ORDER_LOOKUP.get_order_status(phone, confirmed=False)
        │       ├─► resolve_users_by_phone(phone)  →  customer = {user_id, name}
        │       └─► returns {reason:"confirmation_required", customer:{...}}
        │
        ├─► _pending_phone  = "9876543210"   ← stored BEFORE return
        ├─► _pending_customer = {user_id, name}  ← stored BEFORE return
        └─► returns {reason:"confirmation_required", customer:{...}}  to LLM

LLM asks: "Please tell me your name"
Caller speaks name
        │
        ▼
get_order_status_from_db(customer_confirmed=True)   [phone omitted]
        │
        ├─► effective_phone = _pending_phone = "9876543210"  ← reused
        ├─► ORDER_LOOKUP.get_order_status(phone, confirmed=True)
        │       ├─► resolve_users_by_phone(phone)  →  customer
        │       ├─► list_active_orders_for_user(user_id)
        │       │       └─► [] (cross-DB mismatch)
        │       ├─► [FIX 4] list_active_orders_for_user_by_phone(phone)
        │       │       └─► [order_A, order_B]
        │       └─► returns {reason:"order_selection_required", active_orders:[...]}
        │
        ├─► post-DB guard: customer_confirmed=True, result=order_selection_required
        │       → not confirmation_required → guard does NOT fire
        ├─► publish_active_order_ids_to_ui([order_A, order_B])
        └─► returns {reason:"order_selection_required", active_orders:[...]}  to LLM

LLM reads order IDs; caller says "order A"
        │
        ▼
get_order_status_from_db(customer_confirmed=True, external_order_id="ORD-A")
        │
        ├─► ORDER_LOOKUP.get_order_status(phone, confirmed=True, ext_id="ORD-A")
        │       └─► returns {ok:True, order:{...}, latest_status:{...}, items:[...]}
        └─► returns full order status to LLM

LLM speaks order status to caller
```

---

## Testing Strategy

### Validation Approach

Two-phase: first run exploratory tests on **unfixed** code to confirm the bug manifests as
described; then run fix-checking and preservation tests on the **fixed** code.

---

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples that demonstrate the bugs on unfixed code and confirm the
root-cause analysis.

**Test Plan**: Call `get_order_status_from_db` with a mock `ORDER_LOOKUP` that returns a
`confirmation_required` response with a customer object. Assert that `_pending_customer` is
populated after the call. On unfixed code this assertion will fail.

**Test Cases:**

1. **First-call guard fires (Defect 1)**: Call with `phone_number="9876543210"`, `customer_confirmed=False`. Assert `result["customer"] is not None` and `agent._pending_customer is not None`. Will fail on unfixed code — guard returns before DB is called.
2. **Confirmation loop (Defect 2)**: After a first call (which fails to set `_pending_customer`), call again with `customer_confirmed=True`. Assert result is not `confirmation_required`. Will fail on unfixed code.
3. **Lost phone (Defect 3)**: After a successful first call, call again with `customer_confirmed=True` and no `phone_number`. Assert `effective_phone` is not empty. Will fail on unfixed code because `_pending_phone` was never set.
4. **Cross-DB empty orders (Defect 4)**: Configure `list_active_orders_for_user` to return `[]` and `list_orders_for_phone_direct` to return `[order_A]`. Assert result is not `no_active_orders`. Will fail on unfixed code.

**Expected Counterexamples:**
- `_pending_customer` is `None` after the first call.
- `result["reason"] == "confirmation_required"` on the second call even after the caller spoke their name.
- `result["reason"] == "missing_input"` when phone is omitted on the third call.
- `result["reason"] == "no_active_orders"` even when `list_orders_for_phone_direct` would return results.

---

### Fix Checking

**Goal**: Verify Property 1 and Property 2 hold on fixed code.

**Pseudocode:**
```
FOR ALL call WHERE isBugCondition_EarlyGuard(call) DO
  result ← get_order_status_from_db'(call.phone_number, customer_confirmed=False)
  ASSERT result["reason"] = "confirmation_required"
  ASSERT result["customer"] IS NOT NULL
  ASSERT agent._pending_customer IS NOT NULL
  ASSERT agent._pending_phone = normalize(call.phone_number)
END FOR

FOR ALL lookup WHERE isBugCondition_CrossDbMismatch(lookup) DO
  orders ← get_order_status'(phone, customer_confirmed=True)
  ASSERT orders["ok"] = True OR orders["reason"] = "order_selection_required"
  // i.e., NOT "no_active_orders" when phone-based fallback returns results
END FOR
```

---

### Preservation Checking

**Goal**: Verify Property 3 — non-buggy inputs produce identical results before and after the fix.

**Pseudocode:**
```
FOR ALL call WHERE NOT isBugCondition_EarlyGuard(call)
               AND NOT isBugCondition_CrossDbMismatch(call) DO
  ASSERT get_order_status_from_db'(call) = get_order_status_from_db(call)
END FOR
```

**Testing Approach**: Property-based testing is recommended because:
- It generates many random phone numbers, customer names, and order states automatically.
- It catches edge cases (empty names, Unicode names, partial phone numbers) that manual tests miss.
- It provides strong guarantees that the guard restructuring did not alter any existing path.

**Test Cases:**
1. **`customer_not_found` preserved**: Random phone with no DB match → still returns `customer_not_found`.
2. **`multiple_users` preserved**: Phone matching two users → still returns `multiple_users`.
3. **Single active order preserved**: Confirmed customer with one order → still returns `ok:True` directly.
4. **`no_active_orders` preserved**: Confirmed customer, `list_active_orders_for_user` returns `[]`, `ORDERS_PHONE_COL` not set → still returns `no_active_orders`.
5. **`order_selection_required` preserved**: Confirmed customer with two active orders → still returns `order_selection_required` with summary.
6. **`order_not_found` preserved**: `external_order_id` supplied but not matched → still returns `order_not_found`.
7. **Subsequent confirmed call preserved**: `_pending_customer` already set, name matches → guard does not fire, DB result returned as-is.

---

### Unit Tests

- `test_first_call_always_queries_db`: Mock `ORDER_LOOKUP.get_order_status` to return `confirmation_required`. Call `get_order_status_from_db(phone, confirmed=False)`. Assert `_pending_customer` and `_pending_phone` are set.
- `test_second_call_reuses_pending_phone`: After first call sets `_pending_phone`, call again with no `phone_number`. Assert `ORDER_LOOKUP.get_order_status` was called with the cached phone.
- `test_name_confirmation_succeeds`: Set `_pending_customer`, set `_latest_user_text` to customer name, call with `confirmed=True`. Assert result is not `confirmation_required`.
- `test_cross_db_fallback_used`: `list_active_orders_for_user` returns `[]`; `list_orders_for_phone_direct` returns one order. Assert `get_order_status` returns that order (not `no_active_orders`).
- `test_cross_db_fallback_skipped_when_no_phone_col`: `ORDERS_PHONE_COL` not set. `list_active_orders_for_user` returns `[]`. Assert `get_order_status` returns `no_active_orders` (fallback not attempted).
- `test_display_order_id_never_null`: For any order row, `order_for_caller` must return `external_order_id` that is not `None`.

### Property-Based Tests

- **Property 1 PBT**: Generate random valid phone numbers (10-digit strings). For each, mock DB to return a customer. Assert `_pending_customer is not None` and `_pending_phone == normalize(phone)` after the first call.
- **Property 2 PBT**: Generate random `(farmer_engagement_user_id, phone)` pairs where `list_active_orders_for_user` returns `[]` but `list_orders_for_phone_direct` returns 1–5 orders. Assert result reason is never `no_active_orders`.
- **Property 3 PBT (preservation)**: Generate random `(phone, customer, orders)` triples where `_pending_customer` is already set and name matches. Assert fixed `get_order_status_from_db` returns the same result as the original for all such inputs.
- **Property 4 PBT (display id)**: Generate random order rows with arbitrary combinations of `None`/empty/non-empty `external_order_number`, `order_number`, `order_id`. Assert `order_for_caller` always returns a non-`None` `external_order_id`.

### Integration Tests

- **Full happy path**: Phone → `confirmation_required` (with customer) → name spoken → `order_selection_required` (cross-DB fallback) → order ID spoken → `ok:True` with status.
- **Single-order path**: Phone → `confirmation_required` → name confirmed → single active order returned directly.
- **No orders path**: Phone → `confirmation_required` → name confirmed → `list_active_orders_for_user` empty, `ORDERS_PHONE_COL` not set → `no_active_orders`.
- **Cross-DB fallback path**: Phone → `confirmation_required` → name confirmed → `list_active_orders_for_user` empty, `list_orders_for_phone_direct` returns orders → orders shown.
- **UI panel display**: `order_selection_required` response → `publish_active_order_ids_to_ui` called with non-null `external_order_id` for every order row.
