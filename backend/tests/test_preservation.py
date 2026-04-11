"""
Preservation property tests for the order-support flow fix.

These tests verify UNCHANGED behaviors (regression prevention).
They MUST PASS on the current unfixed code — passing confirms the baseline to preserve.

Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8
"""
import asyncio
import sys
import os
import types

# ---------------------------------------------------------------------------
# Stub out livekit and other heavy dependencies before importing worker
# (same approach as test_bugfix_exploration.py)
# ---------------------------------------------------------------------------

def _make_stub_module(name):
    mod = types.ModuleType(name)
    sys.modules[name] = mod
    return mod


def _stub_livekit():
    """Create minimal stubs for livekit so worker.py can be imported in tests."""
    if "livekit" in sys.modules:
        return

    lk = _make_stub_module("livekit")

    rtc = _make_stub_module("livekit.rtc")
    rtc.combine_audio_frames = lambda *a, **kw: None
    lk.rtc = rtc

    agents = _make_stub_module("livekit.agents")

    class _FakeLLM:
        @staticmethod
        def function_tool(fn=None, **kw):
            if fn is not None:
                return fn
            return lambda f: f

        class ChatContext:
            pass

        class ChatMessage:
            text_content = ""

    agents.llm = _FakeLLM()
    agents.APIConnectOptions = object
    agents.AutoSubscribe = object
    agents.JobContext = object
    agents.WorkerOptions = object
    agents.WorkerType = object
    agents.cli = types.SimpleNamespace(run_app=lambda *a, **kw: None)
    agents.get_job_context = lambda: None
    agents.stt = types.SimpleNamespace(
        STT=object,
        STTCapabilities=lambda **kw: None,
        SpeechEvent=object,
        SpeechEventType=types.SimpleNamespace(FINAL_TRANSCRIPT=None),
        SpeechData=object,
    )
    agents.tts = types.SimpleNamespace(
        TTS=object,
        TTSCapabilities=lambda **kw: None,
        ChunkedStream=object,
        AudioEmitter=object,
    )
    agents.utils = types.SimpleNamespace(AudioBuffer=object)
    lk.agents = agents

    voice = _make_stub_module("livekit.agents.voice")

    class _FakeAgent:
        def __init__(self, *, instructions="", **kw):
            self.instructions = instructions

    class _FakeAgentSession:
        pass

    voice.Agent = _FakeAgent
    voice.AgentSession = _FakeAgentSession
    agents.voice = voice

    agent_session_mod = _make_stub_module("livekit.agents.voice.agent_session")
    agent_session_mod.SessionConnectOptions = object
    voice.agent_session = agent_session_mod

    plugins = _make_stub_module("livekit.plugins")
    for plugin_name in ("openai", "silero", "groq"):
        sub = _make_stub_module(f"livekit.plugins.{plugin_name}")
        setattr(plugins, plugin_name, sub)
    lk.plugins = plugins

    plugins.silero.VAD = types.SimpleNamespace(load=lambda **kw: None)
    plugins.groq.LLM = lambda **kw: None
    plugins.openai.LLM = lambda **kw: None

    if "dotenv" not in sys.modules:
        dotenv = _make_stub_module("dotenv")
        dotenv.load_dotenv = lambda *a, **kw: None

    if "httpx" not in sys.modules:
        httpx = _make_stub_module("httpx")
        httpx.AsyncClient = object


_stub_livekit()

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_agent():
    from worker import OrderSupportAgent
    return OrderSupportAgent(language_code="en-IN")


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Test 2.1 — customer_not_found preserved
#
# When the DB returns customer_not_found, the agent must pass it through unchanged.
# Validates: Requirements 3.1
# ---------------------------------------------------------------------------

def test_2_1_customer_not_found_preserved():
    """
    **Validates: Requirements 3.1**

    When get_order_status returns customer_not_found, get_order_status_from_db
    must return that reason unchanged.
    """
    agent = make_agent()

    mock_response = {
        "ok": False,
        "reason": "customer_not_found",
        "phone_last10": "9876543210",
        "message": "No active customer was found for this phone number in master.users.",
    }

    mock_lookup = MagicMock()
    mock_lookup.get_order_status = AsyncMock(return_value=mock_response)
    mock_lookup.normalize_phone = lambda p: (p or "")[-10:] if p else ""

    with patch("worker.ORDER_LOOKUP", mock_lookup):
        result = run(agent.get_order_status_from_db(
            phone_number="9876543210",
            customer_confirmed=False,
        ))

    assert result["reason"] == "customer_not_found", (
        f"Preservation 2.1: expected reason='customer_not_found', got {result}"
    )


# ---------------------------------------------------------------------------
# Test 2.2 — multiple_users preserved
#
# When the DB returns multiple_users, the agent must pass it through unchanged.
# Validates: Requirements 3.2
# ---------------------------------------------------------------------------

def test_2_2_multiple_users_preserved():
    """
    **Validates: Requirements 3.2**

    When get_order_status returns multiple_users, get_order_status_from_db
    must return that reason unchanged.
    """
    agent = make_agent()

    mock_response = {
        "ok": False,
        "reason": "multiple_users",
        "phone_last10": "9876543210",
        "message": "Multiple user records matched this phone number.",
    }

    mock_lookup = MagicMock()
    mock_lookup.get_order_status = AsyncMock(return_value=mock_response)
    mock_lookup.normalize_phone = lambda p: (p or "")[-10:] if p else ""

    with patch("worker.ORDER_LOOKUP", mock_lookup):
        result = run(agent.get_order_status_from_db(
            phone_number="9876543210",
            customer_confirmed=False,
        ))

    assert result["reason"] == "multiple_users", (
        f"Preservation 2.2: expected reason='multiple_users', got {result}"
    )


# ---------------------------------------------------------------------------
# Test 2.3 — Single active order preserved
#
# When _pending_customer is set, name matches, and DB returns ok:True,
# the agent must return ok:True.
# Validates: Requirements 3.3
# ---------------------------------------------------------------------------

def test_2_3_single_active_order_preserved():
    """
    **Validates: Requirements 3.3**

    When _pending_customer is already set, _latest_user_text contains the
    customer name, and the DB returns ok:True, get_order_status_from_db
    must return ok:True.
    """
    agent = make_agent()

    # Pre-set session state (simulating a prior first call)
    agent._pending_customer = {"user_id": "u1", "name": "Ravi Kumar"}
    agent._pending_phone = "9876543210"
    agent._latest_user_text = "Ravi Kumar"

    mock_response = {
        "ok": True,
        "customer": {"user_id": "u1", "name": "Ravi Kumar"},
        "order": {
            "external_order_id": "EXT-001",
            "status": "pending",
            "payment_status": "paid",
            "payment_method": "upi",
            "total_amount": 500.0,
            "created_at": "2024-01-01T00:00:00",
        },
        "items": [],
        "latest_status": None,
    }

    mock_lookup = MagicMock()
    mock_lookup.get_order_status = AsyncMock(return_value=mock_response)
    mock_lookup.normalize_phone = lambda p: (p or "")[-10:] if p else ""

    with patch("worker.ORDER_LOOKUP", mock_lookup):
        result = run(agent.get_order_status_from_db(
            phone_number="9876543210",
            customer_confirmed=True,
        ))

    assert result.get("ok") is True, (
        f"Preservation 2.3: expected ok=True for confirmed customer with single order, got {result}"
    )


# ---------------------------------------------------------------------------
# Test 2.4 — no_active_orders preserved when ORDERS_PHONE_COL not set
#
# When orders_phone_col is None and list_active_orders_for_user returns [],
# get_order_status must return no_active_orders (no fallback attempted).
# Validates: Requirements 3.4
# ---------------------------------------------------------------------------

def test_2_4_no_active_orders_preserved_when_phone_col_not_set():
    """
    **Validates: Requirements 3.4**

    When OrderLookupService is created with orders_phone_col=None and
    list_active_orders_for_user returns [], get_order_status must return
    no_active_orders (the cross-DB fallback is not attempted).
    """
    from order_lookup import OrderLookupService

    service = OrderLookupService(
        orders_db_url=None,
        farmer_engagement_db_url=None,
        orders_phone_col=None,  # no phone col → no fallback
    )

    # Mark as configured so the "not_configured" early return is skipped
    service._orders_engine = MagicMock()
    service._farmer_engine = MagicMock()

    fake_customer = {"user_id": "u1", "name": "Ravi Kumar"}

    service.resolve_users_by_phone = AsyncMock(return_value=[fake_customer])
    service.list_active_orders_for_user = AsyncMock(return_value=[])
    service.fetch_latest_status = AsyncMock(return_value=None)
    service.fetch_order_items = AsyncMock(return_value=[])

    result = run(service.get_order_status(
        phone_number="9876543210",
        customer_confirmed=True,
    ))

    assert result.get("reason") == "no_active_orders", (
        f"Preservation 2.4: expected reason='no_active_orders' when phone_col not set "
        f"and list_active_orders_for_user returns [], got {result}"
    )


# ---------------------------------------------------------------------------
# Test 2.5 — order_selection_required preserved
#
# When the DB returns order_selection_required, the agent must pass it through.
# Validates: Requirements 3.5
# ---------------------------------------------------------------------------

def test_2_5_order_selection_required_preserved():
    """
    **Validates: Requirements 3.5**

    When get_order_status returns order_selection_required (multiple active orders),
    get_order_status_from_db must return that reason with the active_orders list.
    """
    agent = make_agent()

    # Pre-set session state
    agent._pending_customer = {"user_id": "u1", "name": "Ravi Kumar"}
    agent._pending_phone = "9876543210"
    agent._latest_user_text = "Ravi Kumar"

    mock_response = {
        "ok": False,
        "reason": "order_selection_required",
        "phone_last10": "9876543210",
        "customer": {"user_id": "u1", "name": "Ravi Kumar"},
        "active_orders": [
            {"external_order_id": "EXT-001", "status": "pending", "created_at": "2024-01-01"},
            {"external_order_id": "EXT-002", "status": "processing", "created_at": "2024-01-02"},
        ],
        "message": "Multiple active orders found.",
    }

    mock_lookup = MagicMock()
    mock_lookup.get_order_status = AsyncMock(return_value=mock_response)
    mock_lookup.normalize_phone = lambda p: (p or "")[-10:] if p else ""

    with patch("worker.ORDER_LOOKUP", mock_lookup), \
         patch("worker.publish_active_order_ids_to_ui", new=AsyncMock()):
        result = run(agent.get_order_status_from_db(
            phone_number="9876543210",
            customer_confirmed=True,
        ))

    assert result.get("reason") == "order_selection_required", (
        f"Preservation 2.5: expected reason='order_selection_required', got {result}"
    )
    assert "active_orders" in result, (
        f"Preservation 2.5: active_orders missing from result {result}"
    )


# ---------------------------------------------------------------------------
# Test 2.6 — order_not_found preserved
#
# When the DB returns order_not_found, the agent must pass it through unchanged.
# Validates: Requirements 3.6
# ---------------------------------------------------------------------------

def test_2_6_order_not_found_preserved():
    """
    **Validates: Requirements 3.6**

    When get_order_status returns order_not_found (external_order_id supplied
    but not matched), get_order_status_from_db must return that reason unchanged.
    """
    agent = make_agent()

    # Pre-set session state
    agent._pending_customer = {"user_id": "u1", "name": "Ravi Kumar"}
    agent._pending_phone = "9876543210"
    agent._latest_user_text = "Ravi Kumar"

    mock_response = {
        "ok": False,
        "reason": "order_not_found",
        "phone_last10": "9876543210",
        "customer": {"user_id": "u1", "name": "Ravi Kumar"},
        "message": "No active order matches that ID for this customer.",
    }

    mock_lookup = MagicMock()
    mock_lookup.get_order_status = AsyncMock(return_value=mock_response)
    mock_lookup.normalize_phone = lambda p: (p or "")[-10:] if p else ""

    with patch("worker.ORDER_LOOKUP", mock_lookup):
        result = run(agent.get_order_status_from_db(
            phone_number="9876543210",
            customer_confirmed=True,
            external_order_id="EXT-NONEXISTENT",
        ))

    assert result.get("reason") == "order_not_found", (
        f"Preservation 2.6: expected reason='order_not_found', got {result}"
    )


# ---------------------------------------------------------------------------
# Test 2.7 — Subsequent confirmed call preserved (pending_customer already set)
#
# When _pending_customer is already set and name matches, the guard must not
# fire and the DB result must be returned as-is.
# Validates: Requirements 3.7, 3.8
# ---------------------------------------------------------------------------

def test_2_7_subsequent_confirmed_call_preserved():
    """
    **Validates: Requirements 3.7, 3.8**

    When _pending_customer is already set, _pending_phone matches, and
    _latest_user_text contains the customer name, a call with customer_confirmed=True
    must NOT fire the guard — the DB must be called and its result returned.
    """
    agent = make_agent()

    # Pre-set session state (simulating a prior first call)
    agent._pending_customer = {"user_id": "u1", "name": "Ravi Kumar"}
    agent._pending_phone = "9876543210"
    agent._latest_user_text = "Ravi Kumar"

    mock_response = {
        "ok": True,
        "customer": {"user_id": "u1", "name": "Ravi Kumar"},
        "order": {
            "external_order_id": "EXT-001",
            "status": "pending",
            "payment_status": "paid",
            "payment_method": "upi",
            "total_amount": 500.0,
            "created_at": "2024-01-01T00:00:00",
        },
        "items": [],
        "latest_status": None,
    }

    mock_lookup = MagicMock()
    mock_lookup.get_order_status = AsyncMock(return_value=mock_response)
    mock_lookup.normalize_phone = lambda p: (p or "")[-10:] if p else ""

    with patch("worker.ORDER_LOOKUP", mock_lookup):
        result = run(agent.get_order_status_from_db(
            phone_number="9876543210",
            customer_confirmed=True,
        ))

    # DB must have been called
    mock_lookup.get_order_status.assert_called_once(), (
        "Preservation 2.7: DB was not called — guard fired unexpectedly"
    )

    assert result.get("ok") is True, (
        f"Preservation 2.7: expected ok=True when _pending_customer is set and name matches, "
        f"got {result}"
    )


# ---------------------------------------------------------------------------
# PBT — Property 3: Preservation — Non-Buggy Inputs Unchanged
#
# Generate random (phone, customer_name) pairs where _pending_customer is
# already set and name matches. Assert the DB is always called and the result
# is returned as-is (not short-circuited by the guard).
#
# These tests MUST PASS on unfixed code (they test non-bug-condition inputs).
# Validates: Requirements 3.7, 3.8
# ---------------------------------------------------------------------------

from hypothesis import given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402


@given(
    phone=st.from_regex(r"[6-9][0-9]{9}", fullmatch=True),
    first_name=st.text(
        alphabet=st.characters(whitelist_categories=("Lu", "Ll")),
        min_size=2,
        max_size=10,
    ),
    last_name=st.text(
        alphabet=st.characters(whitelist_categories=("Lu", "Ll")),
        min_size=2,
        max_size=10,
    ),
)
@settings(max_examples=20, deadline=5000)
def test_pbt_preservation_subsequent_confirmed_call(phone: str, first_name: str, last_name: str):
    """
    **Validates: Requirements 3.7, 3.8**

    Property: For any valid phone number and customer name, when _pending_customer
    is already set and _latest_user_text contains the customer name, a call with
    customer_confirmed=True must call the DB and return its result unchanged.

    This is a non-bug-condition input (isBugCondition_EarlyGuard is False because
    _pending_customer is already set). The guard must NOT fire.

    MUST PASS on unfixed code.
    """
    customer_name = f"{first_name} {last_name}"
    agent = make_agent()

    # Pre-set session state — this is the non-bug-condition path
    agent._pending_customer = {"user_id": "u-pbt", "name": customer_name}
    agent._pending_phone = phone
    agent._latest_user_text = customer_name  # name matches → guard should not fire

    mock_response = {
        "ok": True,
        "customer": {"user_id": "u-pbt", "name": customer_name},
        "order": {
            "external_order_id": "EXT-PBT",
            "status": "pending",
            "payment_status": "paid",
            "payment_method": "upi",
            "total_amount": 100.0,
            "created_at": "2024-01-01T00:00:00",
        },
        "items": [],
        "latest_status": None,
    }

    mock_lookup = MagicMock()
    mock_lookup.get_order_status = AsyncMock(return_value=mock_response)
    mock_lookup.normalize_phone = lambda p: (p or "")[-10:] if p else ""

    with patch("worker.ORDER_LOOKUP", mock_lookup):
        result = run(agent.get_order_status_from_db(
            phone_number=phone,
            customer_confirmed=True,
        ))

    # DB must have been called (guard must not have short-circuited)
    mock_lookup.get_order_status.assert_called_once(), (
        f"PBT Preservation: DB was not called for phone={phone!r}, name={customer_name!r} — "
        f"guard fired unexpectedly on non-bug-condition input"
    )

    assert result.get("ok") is True, (
        f"PBT Preservation: expected ok=True for phone={phone!r}, name={customer_name!r}, "
        f"got {result}"
    )
