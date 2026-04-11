# Bugfix Requirements Document

## Introduction

The order support voice agent is intended to guide a caller through a 6-step flow:
collect mobile number → look up user in DB → ask caller to confirm their name → show active orders → let caller pick one → speak the order status.

Code review reveals several defects that break this flow before it can complete:

1. The agent-side confirmation guard in `worker.py` short-circuits on the very first tool call, preventing the DB lookup from ever running and leaving `_pending_phone` / `_pending_customer` permanently `None`.
2. Because `_pending_customer` is never populated, every subsequent `customer_confirmed=True` call also fails the guard, making name confirmation impossible.
3. The user-lookup query runs against the `farmer_engagement` database (`master.users`) while the order queries run against the `vikrayasetu` database (`transactions.orders`). The `user_id` returned from `farmer_engagement` is not guaranteed to match the `user_id` stored in `vikrayasetu`, so orders are never found even when the customer exists in both databases.
4. The `livekit-plugins-sarvam` package is absent from `requirements.txt`; the worker imports a hand-rolled Sarvam STT/TTS class that depends on `httpx` and `livekit-agents` internals — if those internals change the worker silently breaks.

Together these defects mean the end-to-end flow (mobile number → name confirmation → show orders → order status) never succeeds.

---

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN the agent calls `get_order_status_from_db` with a phone number and `customer_confirmed=False` for the first time THEN the system returns `confirmation_required` with message "Ask the caller for their 10-digit phone number first" without ever querying the database, because the early guard in `worker.py` fires before `ORDER_LOOKUP.get_order_status()` is called and `_pending_customer` is `None`.

1.2 WHEN the agent calls `get_order_status_from_db` with `customer_confirmed=True` after the caller speaks their name THEN the system returns `confirmation_required` again because `_pending_customer` was never stored (due to defect 1.1), causing an infinite confirmation loop.

1.3 WHEN `_pending_phone` is `None` and `customer_confirmed=True` is passed without an explicit `phone_number` argument THEN the system uses an empty phone string, causing the DB lookup to return `missing_input` and the agent to ask for the phone number again.

1.4 WHEN a customer is found in `master.users` (farmer_engagement DB) and their orders are queried from `transactions.orders` (vikrayasetu DB) using the resolved `user_id` THEN the system returns `no_active_orders` even when the customer has active orders, because the `user_id` namespace differs between the two databases.

1.5 WHEN the caller has multiple active orders and the agent reads out the order IDs THEN the system displays `external_order_id: null` in the UI panel for orders that have no `external_order_number` value, because `display_order_id_for_ui` falls back to `order_number` or `order_id` but the summary dict key is `external_order_id` which is set to `None` when the display value is empty.

### Expected Behavior (Correct)

2.1 WHEN the agent calls `get_order_status_from_db` with a phone number and `customer_confirmed=False` for the first time THEN the system SHALL query `ORDER_LOOKUP.get_order_status()`, store the resolved customer in `_pending_customer` and the phone in `_pending_phone`, and return `confirmation_required` with the customer object so the agent can ask the caller to confirm their name.

2.2 WHEN the agent calls `get_order_status_from_db` with `customer_confirmed=True` after the caller speaks their name THEN the system SHALL use the stored `_pending_phone` and `_pending_customer`, verify the spoken name against the stored customer name, and proceed to fetch active orders when the name matches.

2.3 WHEN `_pending_phone` is set from a prior step and `customer_confirmed=True` is passed without an explicit `phone_number` argument THEN the system SHALL reuse `_pending_phone` so the DB lookup succeeds without asking the caller for their number again.

2.4 WHEN a customer is resolved from `master.users` and their orders are queried from `transactions.orders` THEN the system SHALL use a join or cross-database lookup strategy that correctly maps the `user_id` from `farmer_engagement` to the matching `user_id` in `vikrayasetu`, returning the customer's actual active orders.

2.5 WHEN the caller has multiple active orders and the agent reads out the order IDs THEN the system SHALL display a non-empty, human-readable order identifier (preferring `external_order_number`, then `order_number`, then `order_id`) in the UI panel for every order row so the caller can identify which order to select.

### Unchanged Behavior (Regression Prevention)

3.1 WHEN a phone number that does not exist in `master.users` is provided THEN the system SHALL CONTINUE TO return `customer_not_found` and the agent SHALL CONTINUE TO inform the caller that no account was found for that number.

3.2 WHEN a phone number matches more than one user record in `master.users` THEN the system SHALL CONTINUE TO return `multiple_users` and the agent SHALL CONTINUE TO ask the caller to contact support.

3.3 WHEN a confirmed customer has exactly one active order THEN the system SHALL CONTINUE TO return that order's status, payment details, and items directly without asking the caller to select an order.

3.4 WHEN a confirmed customer has no active orders (all delivered or none exist) THEN the system SHALL CONTINUE TO return `no_active_orders` and the agent SHALL CONTINUE TO inform the caller clearly.

3.5 WHEN the caller selects a specific order by ID and that ID matches an active order for the confirmed customer THEN the system SHALL CONTINUE TO return the full order status, latest status history, and line items for that order.

3.6 WHEN the caller selects a specific order by ID and that ID does not match any active order for the confirmed customer THEN the system SHALL CONTINUE TO return `order_not_found` with the list of active order IDs so the caller can try again.

3.7 WHEN the Sarvam STT returns an empty transcript for a user utterance THEN the system SHALL CONTINUE TO emit the fallback user text so the agent still produces a spoken reply rather than staying silent.

3.8 WHEN the agent session starts THEN the system SHALL CONTINUE TO greet the caller in the selected language and ask for their 10-digit mobile number as the first spoken turn.

---

## Bug Condition Pseudocode

### Bug Condition Functions

```pascal
FUNCTION isBugCondition_EarlyGuard(call)
  // Defects 1.1, 1.2, 1.3
  INPUT: call — a get_order_status_from_db invocation
  OUTPUT: boolean

  RETURN call._pending_customer IS NULL
     AND call.customer_confirmed = false
     // The guard fires before the DB is queried, so _pending_customer stays NULL forever
END FUNCTION

FUNCTION isBugCondition_CrossDbUserIdMismatch(lookup)
  // Defect 1.4
  INPUT: lookup — a user resolved from farmer_engagement queried against vikrayasetu
  OUTPUT: boolean

  RETURN lookup.farmer_engagement_user_id ≠ lookup.vikrayasetu_user_id
END FUNCTION
```

### Fix-Checking Properties

```pascal
// Property: First-call DB lookup must run and populate session state
FOR ALL call WHERE isBugCondition_EarlyGuard(call) DO
  result ← get_order_status_from_db'(call.phone_number, customer_confirmed=false)
  ASSERT result.reason = "confirmation_required"
  ASSERT result.customer IS NOT NULL
  ASSERT session._pending_customer IS NOT NULL
  ASSERT session._pending_phone = normalize(call.phone_number)
END FOR

// Property: Cross-DB order lookup must return orders when they exist
FOR ALL lookup WHERE isBugCondition_CrossDbUserIdMismatch(lookup) DO
  orders ← list_active_orders_for_user'(lookup.farmer_engagement_user_id)
  ASSERT LENGTH(orders) > 0  // when orders exist in vikrayasetu for that customer
END FOR
```

### Preservation Property

```pascal
// For all non-buggy inputs, fixed code must behave identically to original
FOR ALL call WHERE NOT isBugCondition_EarlyGuard(call)
                AND NOT isBugCondition_CrossDbUserIdMismatch(call) DO
  ASSERT get_order_status_from_db'(call) = get_order_status_from_db(call)
END FOR
```
