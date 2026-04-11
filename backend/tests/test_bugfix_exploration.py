"""
Bug condition exploration tests for the order-support flow fix.

These tests are EXPECTED TO FAIL on unfixed code — failure confirms the bugs exist.
DO NOT attempt to fix the tests or the code when they fail.

Validates: Requirements 1.1, 1.2, 1.3, 1.4
"""
import asyncio
import sys
import os
import types

# ---------------------------------------------------------------------------
# Stub out livekit and other heavy dependencies before importing worker
# ---------------------------------------------------------------------------

def _make_stub_module(name):
    mod = types.ModuleType(name)
    sys.modules[name] = mod
    return mod


def _stub_livekit():
    """Create minimal stubs for livekit so worker.py can be imported in tests."""
    if "livekit" in sys.modules:
        return  # already stubbed or real package loaded

    # livekit
    lk = _make_stub_module("livekit")

    # livekit.rtc
    rtc = _make_stub_module("livekit.rtc")
    rtc.combine_audio_frames = lambda *a, **kw: None
    lk.rtc = rtc

    # livekit.agents
    agents = _make_stub_module("livekit.agents")

    class _FakeLLM:
        @staticmethod
        def function_tool(fn=None, **kw):
            """Decorator that returns the function unchanged."""
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

    # livekit.agents.voice
    voice = _make_stub_module("livekit.agents.voice")

    class _FakeAgent:
        def __init__(self, *, instructions="", **kw):
            self.instructions = instructions

    class _FakeAgentSession:
        pass

    voice.Agent = _FakeAgent
    voice.AgentSession = _FakeAgentSession
    agents.voice = voice

    # livekit.agents.voice.agent_session
    agent_session_mod = _make_stub_module("livekit.agents.voice.agent_session")
    agent_session_mod.SessionConnectOptions = object
    voice.agent_session = agent_session_mod

    # livekit.plugins
    plugins = _make_stub_module("livekit.plugins")
    for plugin_name in ("openai", "silero", "groq"):
        sub = _make_stub_module(f"livekit.plugins.{plugin_name}")
        setattr(plugins, plugin_name, sub)
    lk.plugins = plugins

    # silero.VAD stub
    plugins.silero.VAD = types.SimpleNamespace(load=lambda **kw: None)
    # groq.LLM stub
    plugins.groq.LLM = lambda **kw: None
    # openai stub
    plugins.openai.LLM = lambda **kw: None

    # dotenv
    if "dotenv" not in sys.modules:
        dotenv = _make_stub_module("dotenv")
        dotenv.load_dotenv = lambda *a, **kw: None

    # httpx stub (only needed for SarvamSTT/TTS, not for our tests)
    if "httpx" not in sys.modules:
        httpx = _make_stub_module("httpx")
        httpx.AsyncClient = object


_stub_livekit()

# Now safe to add backend to path and import
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MOCK_CONFIRMATION_REQUIRED = {
    "ok": False,
    "reason": "confirmation_required",
    "customer": {"user_id": "u1", "name": "Ravi Kumar"},
    "phone_last10": "9876543210",
    "message": "Please tell me your name to confirm your identity.",
}

MOCK_ORDER_OK = {
    "ok": True,
    "customer": {"user_id": "u1", "name": "Ravi Kumar"},
    "order": {"external_order_id": "EXT-001", "status": "pending"},
    "latest_status": None,
    "items": [],
}


def make_agent():
    """Create an OrderSupportAgent without triggering LiveKit imports at module level."""
    from worker import OrderSupportAgent
    return OrderSupportAgent(language_code="en-IN")


def run(coro):
    """Run a coroutine synchronously, creating a new event loop each time."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Test 1.1 — First-call guard fires (Defect 1.1)
#
# The bug: when customer_confirmed=True is passed on the very first call
# (before _pending_customer is set), the guard fires and returns early with
# "Ask the caller for their 10-digit phone number first" — DB is never queried.
#
# This simulates the LLM incorrectly passing customer_confirmed=True on the
# first call, or the scenario where _pending_customer is None.
#
# EXPECTED TO FAIL on unfixed code.
# ---------------------------------------------------------------------------

def test_1_1_first_call_with_confirmed_true_should_still_query_db():
    """
    **Validates: Requirements 1.1**

    When customer_confirmed=True is passed on the first call (before
    _pending_customer is set), the fixed code MUST still query the DB and
    return a result with customer data — NOT short-circuit with
    "Ask the caller for their 10-digit phone number first".

    FAILS on unfixed code because the early guard fires before the DB call
    when _pending_customer is None and customer_confirmed=True.
    """
    agent = make_agent()
    # _pending_customer is None (first call, no prior state)
    assert agent._pending_customer is None

    mock_lookup = MagicMock()
    mock_lookup.get_order_status = AsyncMock(return_value=MOCK_CONFIRMATION_REQUIRED)
    mock_lookup.normalize_phone = lambda p: (p or "")[-10:] if p else ""

    with patch("worker.ORDER_LOOKUP", mock_lookup):
        result = run(agent.get_order_status_from_db(
            phone_number="9876543210",
            customer_confirmed=True,  # first call, but confirmed=True
        ))

    # The DB MUST have been called — not short-circuited
    mock_lookup.get_order_status.assert_called_once(), (
        "Defect 1.1: DB was never called — early guard fired before DB query"
    )

    # result must come from the DB, not the guard's hardcoded message
    assert result.get("message") != "Ask the caller for their 10-digit phone number first.", (
        f"Defect 1.1: Guard returned early with hardcoded message before DB was queried. "
        f"result={result}"
    )

    # _pending_customer must be set from the DB result
    assert agent._pending_customer is not None, (
        f"Defect 1.1: agent._pending_customer is None after first call. "
        f"The early guard returned before the DB was queried."
    )


# ---------------------------------------------------------------------------
# Test 1.2 — Confirmation loop (Defect 1.2)
#
# The bug: on the second call with customer_confirmed=True, the guard checks
# is_affirmative(_latest_user_text). In a real LLM flow, _latest_user_text
# may not contain the customer's name verbatim (e.g., it's empty or a
# different utterance). The guard then fires again → infinite loop.
#
# EXPECTED TO FAIL on unfixed code.
# ---------------------------------------------------------------------------

def test_1_2_confirmation_loop_broken():
    """
    **Validates: Requirements 1.2**

    After the first call sets _pending_customer, a second call with
    customer_confirmed=True and _latest_user_text="" (empty — simulating
    the LLM calling the tool without the user having spoken yet in this turn)
    MUST NOT return confirmation_required again.

    FAILS on unfixed code because the guard checks is_affirmative("", name)
    which returns False, causing the guard to fire and return confirmation_required
    — infinite loop.
    """
    agent = make_agent()

    mock_lookup = MagicMock()
    # First call returns confirmation_required (sets _pending_customer)
    # Second call returns ok (orders found)
    mock_lookup.get_order_status = AsyncMock(side_effect=[
        MOCK_CONFIRMATION_REQUIRED,
        MOCK_ORDER_OK,
    ])
    mock_lookup.normalize_phone = lambda p: (p or "")[-10:] if p else ""

    with patch("worker.ORDER_LOOKUP", mock_lookup):
        # First call — sets _pending_customer
        run(agent.get_order_status_from_db(
            phone_number="9876543210",
            customer_confirmed=False,
        ))

        # _pending_customer should now be set
        assert agent._pending_customer is not None, "Setup: first call should set _pending_customer"

        # Simulate the LLM calling the tool — _latest_user_text is empty
        # (the user spoke their name but the LLM is now calling the tool)
        agent._latest_user_text = ""

        # Second call — should proceed past the guard since _pending_customer is set
        result = run(agent.get_order_status_from_db(
            phone_number="9876543210",
            customer_confirmed=True,
        ))

    assert result.get("reason") != "confirmation_required", (
        f"Defect 1.2: Infinite confirmation loop — second call returned "
        f"confirmation_required because is_affirmative('', name) is False. "
        f"The guard should not re-fire when _pending_customer is already set "
        f"and the DB itself returns ok. result={result}"
    )


# ---------------------------------------------------------------------------
# Test 1.3 — Lost phone (Defect 1.3)
#
# The bug: the effective_phone fallback is gated on `if customer_confirmed`.
# If the LLM calls with customer_confirmed=False and omits phone_number
# (e.g., a follow-up call), effective_phone stays None and the DB returns
# missing_input.
#
# EXPECTED TO FAIL on unfixed code.
# ---------------------------------------------------------------------------

def test_1_3_lost_phone_reused_without_confirmed():
    """
    **Validates: Requirements 1.3**

    After a first call sets _pending_phone, a follow-up call with
    customer_confirmed=False and NO phone_number MUST reuse _pending_phone
    so the DB lookup succeeds.

    FAILS on unfixed code because the effective_phone fallback is gated on
    `if customer_confirmed` — when customer_confirmed=False, the phone is
    not reused and effective_phone stays None, causing the DB to return missing_input.
    """
    agent = make_agent()

    def smart_mock_get_order_status(*, phone_number=None, customer_confirmed=False, external_order_id=None):
        """Simulate real DB behavior: return missing_input when phone is empty."""
        from order_lookup import OrderLookupService
        cleaned = OrderLookupService.normalize_phone(phone_number)
        if not cleaned:
            return {
                "ok": False,
                "reason": "missing_input",
                "message": "Provide only the customer's 10-digit phone number.",
            }
        return MOCK_CONFIRMATION_REQUIRED

    mock_lookup = MagicMock()
    mock_lookup.get_order_status = AsyncMock(side_effect=smart_mock_get_order_status)
    mock_lookup.normalize_phone = lambda p: (p or "")[-10:] if p else ""

    with patch("worker.ORDER_LOOKUP", mock_lookup):
        # First call with phone — sets _pending_phone
        run(agent.get_order_status_from_db(
            phone_number="9876543210",
            customer_confirmed=False,
        ))

        assert agent._pending_phone is not None, "Setup: first call should set _pending_phone"

        # Second call WITHOUT phone_number and customer_confirmed=False
        # (e.g., LLM retries the first step without repeating the phone)
        result = run(agent.get_order_status_from_db(
            phone_number=None,
            customer_confirmed=False,
        ))

    # Should reuse _pending_phone, not return missing_input
    assert result.get("reason") != "missing_input", (
        f"Defect 1.3: result reason is 'missing_input' — _pending_phone was not "
        f"reused because the effective_phone fallback is gated on customer_confirmed=True. "
        f"result={result}"
    )


# ---------------------------------------------------------------------------
# Test 1.4 — Cross-DB empty orders (Defect 1.4)
#
# list_active_orders_for_user returns [] (cross-DB mismatch), but
# list_active_orders_for_user_by_phone would return one order.
# On unfixed code the fallback method does not exist, so no_active_orders is
# returned immediately.
#
# EXPECTED TO FAIL on unfixed code (no_active_orders returned).
# ---------------------------------------------------------------------------

def test_1_4_cross_db_fallback_used():
    """
    **Validates: Requirements 1.4**

    When list_active_orders_for_user returns [] but
    list_active_orders_for_user_by_phone returns one order (and _orders_phone_col
    is set), get_order_status MUST NOT return no_active_orders.

    FAILS on unfixed code because list_active_orders_for_user_by_phone does not
    exist yet, so the fallback is never attempted and no_active_orders is returned.
    """
    from order_lookup import OrderLookupService

    service = OrderLookupService(
        orders_db_url=None,
        farmer_engagement_db_url=None,
        orders_phone_col="phone",
    )

    # Mark as configured
    service._orders_engine = MagicMock()
    service._farmer_engine = MagicMock()

    fake_customer = {"user_id": "fe-uuid-123", "name": "Ravi Kumar"}
    fake_order = {
        "order_id": "ord-1",
        "order_number": "ON-001",
        "external_order_number": "EXT-001",
        "internal_order_id": None,
        "user_id": "vs-uuid-456",
        "status": "pending",
        "payment_status": "paid",
        "payment_method": "upi",
        "total_amount": 500.0,
        "delivered_at": None,
        "created_at": "2024-01-01T00:00:00",
    }

    service.resolve_users_by_phone = AsyncMock(return_value=[fake_customer])
    service.list_active_orders_for_user = AsyncMock(return_value=[])  # cross-DB mismatch
    service.fetch_latest_status = AsyncMock(return_value=None)
    service.fetch_order_items = AsyncMock(return_value=[])

    # list_active_orders_for_user_by_phone is the NEW fallback method (doesn't exist yet)
    service.list_active_orders_for_user_by_phone = AsyncMock(return_value=[fake_order])

    result = run(service.get_order_status(
        phone_number="9876543210",
        customer_confirmed=True,
    ))

    assert result.get("reason") != "no_active_orders", (
        f"Defect 1.4: result is no_active_orders even though "
        f"list_active_orders_for_user_by_phone would return an order. "
        f"The cross-DB fallback is not implemented. result={result}"
    )


# ---------------------------------------------------------------------------
# PBT — Property 1: Guard must not fire before DB on first call with confirmed=True
#
# Generate random 10-digit phone strings; for each, mock DB to return a
# customer; assert _pending_customer is not None after first call with
# customer_confirmed=True (the bug condition).
#
# EXPECTED TO FAIL on unfixed code.
# ---------------------------------------------------------------------------

from hypothesis import given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402


@given(
    phone=st.from_regex(r"[6-9][0-9]{9}", fullmatch=True),
)
@settings(max_examples=20, deadline=5000)
def test_pbt_first_call_confirmed_true_sets_pending_customer(phone: str):
    """
    **Validates: Requirements 1.1**

    Property: For any valid 10-digit phone number, when get_order_status_from_db
    is called with customer_confirmed=True and _pending_customer is None (first call),
    the fixed code MUST query the DB and set _pending_customer.

    FAILS on unfixed code because the early guard fires before the DB is called
    when _pending_customer is None and customer_confirmed=True.
    """
    agent = make_agent()
    assert agent._pending_customer is None  # fresh agent, no prior state

    mock_response = {
        "ok": False,
        "reason": "confirmation_required",
        "customer": {"user_id": "u-pbt", "name": "Test Customer"},
        "phone_last10": phone[-10:],
        "message": "Please tell me your name to confirm your identity.",
    }

    mock_lookup = MagicMock()
    mock_lookup.get_order_status = AsyncMock(return_value=mock_response)
    mock_lookup.normalize_phone = lambda p: (p or "")[-10:] if p else ""

    with patch("worker.ORDER_LOOKUP", mock_lookup):
        result = run(agent.get_order_status_from_db(
            phone_number=phone,
            customer_confirmed=True,  # bug condition: confirmed=True, _pending_customer=None
        ))

    # DB must have been called
    mock_lookup.get_order_status.assert_called_once()

    # _pending_customer must be set from DB result
    assert agent._pending_customer is not None, (
        f"Defect 1.1 (PBT): phone={phone!r} — _pending_customer is None after first call "
        f"with customer_confirmed=True. The early guard fired before the DB was queried. "
        f"result={result}"
    )
