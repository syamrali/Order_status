import os
import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _to_asyncpg_url(url: str) -> str:
    if url.startswith("postgresql+asyncpg://"):
        return url
    if url.startswith("postgresql+psycopg2://"):
        return "postgresql+asyncpg://" + url[len("postgresql+psycopg2://") :]
    if url.startswith("postgresql+psycopg://"):
        return "postgresql+asyncpg://" + url[len("postgresql+psycopg://") :]
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url[len("postgresql://") :]
    if url.startswith("postgres://"):
        return "postgresql+asyncpg://" + url[len("postgres://") :]
    return url


def _safe_identifier(value: str, *, fallback: str) -> str:
    candidate = (value or "").strip()
    if not candidate or not _IDENTIFIER_RE.fullmatch(candidate):
        return fallback
    return candidate


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _jsonable_row(row: dict[str, Any]) -> dict[str, Any]:
    return {k: _jsonable(v) for k, v in row.items()}


class OrderLookupService:
    def __init__(
        self,
        *,
        orders_db_url: str | None,
        farmer_engagement_db_url: str | None,
        orders_phone_col: str | None = None,
        status_col: str = "new_status",
        status_changed_at_col: str = "changed_at",
    ) -> None:
        self._orders_engine: AsyncEngine | None = None
        self._farmer_engine: AsyncEngine | None = None

        if orders_db_url:
            self._orders_engine = create_async_engine(
                _to_asyncpg_url(orders_db_url), pool_pre_ping=True, future=True
            )

        # If farmer-engagement DB URL is missing, reuse order DB connection.
        if farmer_engagement_db_url:
            self._farmer_engine = create_async_engine(
                _to_asyncpg_url(farmer_engagement_db_url), pool_pre_ping=True, future=True
            )
        else:
            self._farmer_engine = self._orders_engine

        self._orders_phone_col = (
            _safe_identifier(orders_phone_col, fallback="") if orders_phone_col else ""
        )
        self._status_col = _safe_identifier(status_col, fallback="new_status")
        self._status_changed_at_col = _safe_identifier(
            status_changed_at_col, fallback="changed_at"
        )
        self._excluded_active_statuses = self._parse_excluded_statuses(
            os.getenv("ORDER_EXCLUDED_FROM_ACTIVE_STATUSES", "delivered")
        )

    @staticmethod
    def _parse_excluded_statuses(raw: str | None) -> frozenset[str]:
        parts = [p.strip().lower() for p in (raw or "").split(",") if p.strip()]
        return frozenset(parts) if parts else frozenset({"delivered"})

    @classmethod
    def from_env(cls) -> "OrderLookupService":
        return cls(
            orders_db_url=os.getenv("DATABASE_URL"),
            farmer_engagement_db_url=os.getenv("FARMER_ENGAGEMENT_DATABASE_URL"),
            orders_phone_col=os.getenv("ORDERS_PHONE_COL"),
            status_col=os.getenv("ORDER_STATUS_HISTORY_STATUS_COL", "new_status"),
            status_changed_at_col=os.getenv("ORDER_STATUS_HISTORY_CHANGED_AT_COL", "changed_at"),
        )

    def _normalized_order_status(self, status: Any) -> str:
        return str(status or "").strip().lower()

    def _is_active_order_status(self, status: Any) -> bool:
        return self._normalized_order_status(status) not in self._excluded_active_statuses

    def _app_external_order_id(self, row: dict[str, Any]) -> str:
        """Value from `transactions.orders.external_order_number` when set."""
        ext = row.get("external_order_number")
        if ext is None:
            return ""
        return str(ext).strip()

    def display_order_id_for_ui(self, row: dict[str, Any]) -> str:
        """ID to show in chat / read aloud: prefer app external id, else order_number, else short ref."""
        ext = self._app_external_order_id(row)
        if ext:
            return ext
        on = row.get("order_number")
        if on is not None and str(on).strip():
            return str(on).strip()
        oid = row.get("order_id")
        if oid is not None and str(oid).strip():
            return str(oid).strip()
        return ""

    def order_for_caller(self, row: dict[str, Any]) -> dict[str, Any]:
        """Caller-facing order fields; `external_order_id` uses app id when present, else a display fallback."""
        eid = self.display_order_id_for_ui(row)
        out: dict[str, Any] = {
            "external_order_id": eid if eid else str(row.get("order_id", "")),
            "status": _jsonable(row.get("status")),
            "payment_status": _jsonable(row.get("payment_status")),
            "payment_method": _jsonable(row.get("payment_method")),
            "total_amount": _jsonable(row.get("total_amount")),
            "created_at": _jsonable(row.get("created_at")),
            "delivered_at": _jsonable(row.get("delivered_at")),
        }
        return {k: v for k, v in out.items() if v is not None}

    @staticmethod
    def order_item_for_caller(row: dict[str, Any]) -> dict[str, Any]:
        """Line items without internal DB identifiers."""
        out = {
            "quantity": _jsonable(row.get("quantity")),
            "price": _jsonable(row.get("price")),
            "total_amount": _jsonable(row.get("total_amount")),
        }
        return {k: v for k, v in out.items() if v is not None}

    def _sql_excluded_status_literals(self) -> str:
        """Safe comma-separated quoted literals for NOT IN (...)."""
        parts: list[str] = []
        for s in self._excluded_active_statuses:
            if s and len(s) <= 64 and re.fullmatch(r"[a-z0-9_\-]+", s):
                parts.append("'" + s.replace("'", "") + "'")
        if not parts:
            parts = ["'delivered'"]
        return ", ".join(parts)

    @property
    def configured(self) -> bool:
        return self._orders_engine is not None

    async def aclose(self) -> None:
        if self._farmer_engine and self._farmer_engine is not self._orders_engine:
            await self._farmer_engine.dispose()
        if self._orders_engine:
            await self._orders_engine.dispose()

    @staticmethod
    def normalize_phone(phone_number: str | None) -> str:
        digits = re.sub(r"[^0-9]", "", phone_number or "")
        return digits[-10:] if len(digits) >= 10 else ""

    async def _fetch_all(
        self, engine: AsyncEngine | None, query: str, params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        if engine is None:
            return []
        async with engine.connect() as conn:
            result = await conn.execute(text(query), params)
            rows = result.mappings().all()
        return [_jsonable_row(dict(row)) for row in rows]

    async def resolve_users_by_phone(self, phone_number: str) -> list[dict[str, Any]]:
        d10 = self.normalize_phone(phone_number)
        if not d10:
            return []
        query = """
            SELECT u.user_id::text AS resolved_user_id
                 , COALESCE(
                       NULLIF(
                           BTRIM(
                               CONCAT_WS(
                                   ' ',
                                   NULLIF(BTRIM(u.first_name::text), ''),
                                   NULLIF(BTRIM(u.last_name::text), '')
                               )
                           ),
                           ''
                       ),
                       NULLIF(BTRIM(u.first_name::text), ''),
                       NULLIF(BTRIM(u.last_name::text), ''),
                       'Customer'
                   ) AS resolved_user_name
            FROM master.users AS u
            WHERE RIGHT(
                REGEXP_REPLACE(COALESCE(u.phone::text, ''), '[^0-9]', '', 'g'),
                10
            ) = :d10
              AND NOT COALESCE(u.is_deleted, false)
              AND COALESCE(u.is_active, true)
            LIMIT 2
        """
        rows = await self._fetch_all(self._farmer_engine, query, {"d10": d10})
        return [
            {
                "user_id": str(row["resolved_user_id"]),
                "name": str(row.get("resolved_user_name") or "Customer"),
            }
            for row in rows
            if row.get("resolved_user_id")
        ]

    async def list_orders_for_user(self, user_id: str, *, limit: int = 5) -> list[dict[str, Any]]:
        if not user_id:
            return []
        query = """
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
              AND (
                    o.user_id::text = BTRIM(:app_user_id)
                 OR replace(upper(o.user_id::text), '-', '') = replace(upper(BTRIM(:app_user_id)), '-', '')
              )
            ORDER BY o.created_at DESC
            LIMIT :limit
        """
        return await self._fetch_all(
            self._orders_engine, query, {"app_user_id": user_id, "limit": max(1, min(limit, 10))}
        )

    async def list_active_orders_for_user(
        self, user_id: str, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Non-delivered (etc.) orders for the user, newest first. Uses `transactions.orders.status`."""
        if not user_id:
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
              AND (
                    o.user_id::text = BTRIM(:app_user_id)
                 OR replace(upper(o.user_id::text), '-', '') = replace(upper(BTRIM(:app_user_id)), '-', '')
              )
              AND LOWER(TRIM(COALESCE(o.status::text, ''))) NOT IN ({excluded_sql})
            ORDER BY o.created_at DESC
            LIMIT :limit
        """
        return await self._fetch_all(self._orders_engine, query, {"app_user_id": user_id, "limit": lim})

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

    async def list_orders_for_phone_direct(
        self, phone_number: str, *, limit: int = 5
    ) -> list[dict[str, Any]]:
        if not self._orders_phone_col:
            return []
        d10 = self.normalize_phone(phone_number)
        if not d10:
            return []

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
            ORDER BY o.created_at DESC
            LIMIT :limit
        """
        return await self._fetch_all(
            self._orders_engine, query, {"d10": d10, "limit": max(1, min(limit, 10))}
        )

    async def find_orders_by_reference(
        self, order_reference: str, *, user_id: str | None = None
    ) -> list[dict[str, Any]]:
        if not order_reference:
            return []

        scoped_filter = """
            AND (
                    o.user_id::text = BTRIM(:app_user_id)
                 OR replace(upper(o.user_id::text), '-', '') = replace(upper(BTRIM(:app_user_id)), '-', '')
            )
        """ if user_id else ""

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
              AND (
                    o.order_id::text = BTRIM(:order_ref)
                 OR o.order_number::text = BTRIM(:order_ref)
                 OR COALESCE(o.external_order_id::text, '') = BTRIM(:order_ref)
                 OR COALESCE(o.internal_order_id::text, '') = BTRIM(:order_ref)
              )
              {scoped_filter}
            ORDER BY o.created_at DESC
            LIMIT 5
        """

        params: dict[str, Any] = {"order_ref": order_reference}
        if user_id:
            params["app_user_id"] = user_id
        return await self._fetch_all(self._orders_engine, query, params)

    async def fetch_latest_status(self, order_id: str) -> dict[str, Any] | None:
        query = f"""
            SELECT DISTINCT ON (h.order_id)
                h.order_id::text AS order_id,
                h.old_status::text AS old_status,
                h.{self._status_col}::text AS latest_status,
                h.{self._status_changed_at_col} AS status_changed_at
            FROM transactions.order_status_history AS h
            WHERE h.order_id::text = BTRIM(:order_id)
              AND NOT COALESCE(h.is_deleted, false)
            ORDER BY h.order_id, h.{self._status_changed_at_col} DESC NULLS LAST
        """
        rows = await self._fetch_all(self._orders_engine, query, {"order_id": order_id})
        return rows[0] if rows else None

    async def fetch_order_items(self, order_id: str) -> list[dict[str, Any]]:
        query = """
            SELECT
                oi.order_item_id::text AS order_item_id,
                oi.order_item_external_id::text AS order_item_external_id,
                oi.product_variant_id::text AS product_variant_id,
                oi.quantity,
                oi.price,
                oi.total_amount
            FROM transactions.order_items AS oi
            WHERE oi.order_id::text = BTRIM(:order_id)
              AND NOT COALESCE(oi.is_deleted, false)
            ORDER BY oi.order_item_id
        """
        return await self._fetch_all(self._orders_engine, query, {"order_id": order_id})

    async def get_order_status(
        self,
        *,
        phone_number: str | None = None,
        customer_confirmed: bool = False,
        external_order_id: str | None = None,
    ) -> dict[str, Any]:
        if not self.configured:
            return {
                "ok": False,
                "reason": "not_configured",
                "message": "DATABASE_URL is missing. Configure database access first.",
            }

        cleaned_phone = self.normalize_phone(phone_number)
        if not cleaned_phone:
            return {
                "ok": False,
                "reason": "missing_input",
                "message": "Provide only the customer's 10-digit phone number.",
            }

        resolved_users: list[dict[str, Any]] = []
        resolve_warning: str | None = None
        try:
            resolved_users = await self.resolve_users_by_phone(cleaned_phone)
        except Exception as exc:
            resolve_warning = str(exc)

        if len(resolved_users) > 1:
            return {
                "ok": False,
                "reason": "multiple_users",
                "phone_last10": cleaned_phone,
                "message": "Multiple user records matched this phone number.",
            }

        customer = resolved_users[0] if resolved_users else None
        if not customer:
            return {
                "ok": False,
                "reason": "customer_not_found",
                "phone_last10": cleaned_phone,
                "message": "No active customer was found for this phone number in master.users.",
            }

        if not customer_confirmed:
            return {
                "ok": False,
                "reason": "confirmation_required",
                "phone_last10": cleaned_phone,
                "customer": customer,
                "warnings": {"resolve_user_id": resolve_warning} if resolve_warning else None,
                "message": (
                    "Ask the customer to tell you their name for verification. "
                    "DO NOT say their name. Just ask: 'Please tell me your name to confirm your identity.'"
                ),
            }

        user_id = str(customer["user_id"])
        ext_id = (external_order_id or "").strip()

        try:
            active_orders = await self.list_active_orders_for_user(user_id, limit=50)
        except Exception as exc:
            return {
                "ok": False,
                "reason": "db_error",
                "message": "Database query failed while searching orders.",
                "error": str(exc),
            }

        if ext_id:
            matches = await self.find_orders_by_reference(ext_id, user_id=user_id)
            active_matches = [m for m in matches if self._is_active_order_status(m.get("status"))]
            if not active_matches:
                return {
                    "ok": False,
                    "reason": "order_not_found",
                    "phone_last10": cleaned_phone,
                    "customer": customer,
                    "resolved_user_id": user_id,
                    "active_orders_summary": [
                        {
                            "external_order_id": self.display_order_id_for_ui(o),
                            "status": o.get("status"),
                            "created_at": o.get("created_at"),
                        }
                        for o in active_orders
                    ],
                    "message": (
                        "No active order matches that ID for this customer. "
                        "Ask them to check the order ID in the app, or pick one from active_orders_summary."
                    ),
                }
            selected_order = active_matches[0]
        else:
            if not active_orders and self._orders_phone_col:
                try:
                    active_orders = await self.list_active_orders_for_user_by_phone(
                        cleaned_phone, limit=50
                    )
                except Exception:
                    pass  # fallback failed; proceed to no_active_orders below

            if not active_orders:
                return {
                    "ok": False,
                    "reason": "no_active_orders",
                    "phone_last10": cleaned_phone,
                    "customer": customer,
                    "resolved_user_id": user_id,
                    "message": (
                        "This customer has no active orders (for example, all may be delivered). "
                        "Say that clearly and offer help only with active orders."
                    ),
                }
            if len(active_orders) > 1:
                summary = [
                    {
                        "external_order_id": self.display_order_id_for_ui(o),
                        "status": o.get("status"),
                        "created_at": o.get("created_at"),
                    }
                    for o in active_orders
                ]
                return {
                    "ok": False,
                    "reason": "order_selection_required",
                    "phone_last10": cleaned_phone,
                    "customer": customer,
                    "resolved_user_id": user_id,
                    "active_orders": summary,
                    "message": (
                        "Multiple active orders found. The chat lists an order id for each row — prefer the "
                        "same id the customer sees in the app (external_order_number) when present. "
                        "Ask which order they mean, then call again with external_order_id set to that id."
                    ),
                }
            selected_order = active_orders[0]

        order_id = str(selected_order.get("order_id", ""))
        try:
            latest_status = await self.fetch_latest_status(order_id)
            items = await self.fetch_order_items(order_id)
        except Exception as exc:
            return {
                "ok": False,
                "reason": "db_error",
                "message": "Database query failed while fetching order details.",
                "error": str(exc),
            }

        result = {
            "ok": True,
            "query": {
                "phone_last10": cleaned_phone,
                "customer_confirmed": customer_confirmed,
                "external_order_id": ext_id or None,
            },
            "warnings": {"resolve_user_id": resolve_warning} if resolve_warning else None,
            "resolved_user_id": user_id,
            "customer": customer,
            "active_orders_count": len(active_orders) if not ext_id else None,
            "order": self.order_for_caller(selected_order),
            "latest_status": latest_status,
            "items": [self.order_item_for_caller(i) for i in items],
        }
        return result
