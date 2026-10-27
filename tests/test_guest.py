"""Guest (public demo) access: a read-only session for visitors who have no account.

What these tests pin down:
  * it is off unless switched on, and refuses to start in an unsafe configuration;
  * a guest is never a user row, never more than 'viewer', and cannot be forged, extended or revived (epoch, expiry, shape);
  * EVERY route that changes something refuses a guest (found by walking the app's route table, not by a hand-written list),
    and the central gate refuses a guest even on a route that forgot to ask for a stricter role;
  * the demonstration features that are safe (read the data, ask the assistant, search documents) work, within budgets;
  * real accounts, the admin area and the existing role matrix are unchanged while guest access is on.
Groq is faked (tests/chat_support.py); everything else runs for real.
"""
import re
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app import auth, guest as guest_mod
from app.auth import SAFE_METHODS, Principal, guest_may, require_roles
from app.auth_routes import guest_global_limiter, guest_ip_limiter
from app.config import settings
from app.db import AuditLog, Base, Email, Invoice, SessionLocal, User, engine
from app.main import create_app
from tests.mcp_support import JWT_SECRET, PW, USER_TOOLS, world  # noqa: F401  (fixture)
from tests.rag_support import ONBOARDING, POLICY, SECRET, chat, rag, say, tc  # noqa: F401  (fixtures / helpers)
from tests.test_mailbox import light_pipeline


@pytest.fixture(autouse=True)
def _clean_limits():
    for limiter in (guest_ip_limiter, guest_global_limiter):
        limiter.hits.clear()
    guest_mod.budget.reset()
    yield
    guest_mod.budget.reset()


@pytest.fixture
def guests_on(monkeypatch):
    monkeypatch.setattr(settings, "guest_enabled", True)


def start_guest(world_or_app, **kw):
    """A fresh browser (own cookie jar) that has started a guest session -> (client, the session token)."""
    app = world_or_app if isinstance(world_or_app, FastAPI) else world_or_app.client.app
    c = TestClient(app, **kw)
    r = c.post("/auth/guest")
    assert r.status_code == 200, r.text
    return c, c.cookies.get(auth.COOKIE)


def forge(claims: dict, secret: str = JWT_SECRET) -> str:
    base = {"exp": datetime.now(timezone.utc) + timedelta(minutes=30), "iat": datetime.now(timezone.utc)}
    return jwt.encode({**base, **claims}, secret, algorithm="HS256")


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------- switch and startup checks
def test_guest_access_is_off_by_default(world):
    assert world.client.post("/auth/guest").status_code == 404
    assert world.client.get("/health").json()["guest_access"] is False


def test_guest_endpoint_is_a_404_without_authentication(world, monkeypatch, guests_on):
    monkeypatch.setattr(settings, "auth_enabled", False)
    assert world.client.post("/auth/guest").status_code == 404
    assert world.client.get("/health").json()["guest_access"] is False


@pytest.mark.parametrize("field,value,fragment", [
    ("auth_enabled", False, "AUTH_ENABLED"),
    ("demo_mode", True, "DEMO_MODE"),
    ("imap_host", "imap.example.com", "IMAP"),
    ("guest_session_minutes", 0, "SESSION_MINUTES"),
    ("guest_session_minutes", 1000, "SESSION_MINUTES"),
    ("guest_epoch", 0, "EPOCH"),
    ("guest_chat_per_day_global", 0, "limits"),
])
def test_guest_access_refuses_to_start_in_an_unsafe_configuration(monkeypatch, guests_on, field, value, fragment):
    monkeypatch.setattr(settings, "auth_enabled", True)
    auth.guest_startup_checks()                                           # the safe baseline starts
    monkeypatch.setattr(settings, field, value)
    with pytest.raises(RuntimeError, match=fragment):
        auth.guest_startup_checks()


def test_startup_hook_runs_the_guest_checks(monkeypatch, guests_on):
    monkeypatch.setattr(settings, "demo_mode", True)
    with pytest.raises(RuntimeError, match="guest access"):
        auth.startup_checks_and_bootstrap_pre()


# ---------------------------------------------------------------- the session itself
def test_a_guest_session_is_a_locked_down_viewer_and_not_a_user(world, guests_on):
    with SessionLocal() as s:
        users_before = s.query(User).count()
    c = TestClient(world.client.app)
    r = c.post("/auth/guest")
    body = r.json()
    assert r.status_code == 200 and body["user"]["role"] == "viewer" and body["user"]["guest"] is True
    assert re.fullmatch(r"guest-[0-9a-f]{12}", body["user"]["username"])
    assert "access_token" not in body and "token" not in r.text.lower().replace("expires", "")      # the browser only gets the cookie
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    me = c.get("/auth/me").json()
    assert me == {"auth_enabled": True, "username": body["user"]["username"], "role": "viewer", "guest": True}
    with SessionLocal() as s:
        assert s.query(User).count() == users_before                       # no account was created
        rows = s.query(AuditLog).filter_by(action="guest_login").all()
    assert [x.actor for x in rows] == [body["user"]["username"]]
    assert c.get("/health").json()["guest_access"] is True
    assert "demo_accounts" not in c.get("/health").json()                  # no shared password is advertised


def test_logging_out_ends_the_visit_in_the_browser(world, guests_on):
    c, _ = start_guest(world)
    assert c.get("/metrics").status_code == 200
    c.post("/auth/logout")
    assert c.get("/metrics").status_code == 401


def test_starting_guest_sessions_is_rate_limited(world, guests_on, monkeypatch):
    monkeypatch.setattr(settings, "guest_logins_per_minute_per_ip", 3)
    codes = [TestClient(world.client.app).post("/auth/guest").status_code for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]
    monkeypatch.setattr(settings, "guest_logins_per_minute_per_ip", 100)
    guest_ip_limiter.hits.clear()
    monkeypatch.setattr(settings, "guest_logins_per_minute_global", 2)
    guest_global_limiter.hits.clear()
    assert [TestClient(world.client.app).post("/auth/guest").status_code for _ in range(3)] == [200, 200, 429]


# ---------------------------------------------------------------- what a guest may see and do
def test_a_guest_can_browse_the_sample_data(world, guests_on):
    c, _ = start_guest(world)
    for path in ["/metrics", "/stats", "/invoices", "/vendors", "/models", "/invoices.csv", f"/invoices/{world.ids['i1']}"]:
        assert c.get(path).status_code == 200, path
    assert len(c.get("/invoices").json()) == 3


@pytest.mark.parametrize("path", ["/review-queue", "/triage", "/audit", "/settings", "/users", "/invoices/1/audit"])
def test_a_guest_cannot_read_the_reviewer_or_admin_areas(world, guests_on, path):
    c, _ = start_guest(world)
    assert c.get(path).status_code == 403


def test_no_route_that_changes_anything_accepts_a_guest(world, guests_on):
    """Walks the real route table, so a route added tomorrow is covered without anyone remembering to list it."""
    c, _ = start_guest(world)
    skip = {("POST", "/chat"), ("POST", "/rag/search"),                    # the two allowed demonstration calls
            ("POST", "/auth/guest"), ("POST", "/auth/login"), ("POST", "/auth/logout"),   # session management, no data
            ("POST", "/mcp"), ("DELETE", "/mcp")}                          # MCP has its own credentials (see the MCP test)
    with SessionLocal() as s:
        before = (s.query(Invoice).count(), s.query(Email).count(), s.query(User).count(), [(i.id, i.status) for i in s.query(Invoice).order_by(Invoice.id)])
    checked = []
    for route in world.client.app.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in sorted(route.methods - SAFE_METHODS):
            if (method, route.path) in skip:
                continue
            path = re.sub(r"\{[^}]+\}", "1", route.path)
            if path == "/rag/documents":
                r = c.request(method, path, files={"file": ("a.md", POLICY)}, data={"min_role": "viewer"})
            else:
                r = c.request(method, path, json={"action": "approve", "role": "admin", "username": "x", "password": PW,
                                                  "message_id": "g", "subject": "s", "body": "b"})
            assert r.status_code == 403, f"{method} {route.path} answered {r.status_code} for a guest"
            checked.append((method, route.path))
    assert len(checked) >= 8 and ("POST", "/emails") in checked and ("POST", "/invoices/{invoice_id}/review") in checked
    assert ("POST", "/users") in checked and ("PATCH", "/users/{user_id}") in checked and ("DELETE", "/rag/documents/{doc_id}") in checked
    with SessionLocal() as s:
        after = (s.query(Invoice).count(), s.query(Email).count(), s.query(User).count(), [(i.id, i.status) for i in s.query(Invoice).order_by(Invoice.id)])
    assert after == before


def test_the_central_gate_refuses_a_guest_even_on_a_route_open_to_every_role(world, guests_on):
    """Defence in depth: a future POST route that allows 'viewer' by mistake still cannot be used by a guest."""
    app = create_app(light_pipeline)

    @app.post("/_probe")
    def probe(p: Principal = Depends(require_roles("admin", "reviewer", "viewer"))):
        return {"by": p.username}

    with TestClient(app) as viewer:
        assert viewer.post("/auth/login", json={"username": "view1", "password": PW}).status_code == 200
        assert viewer.post("/_probe").status_code == 200                    # a real viewer account is unaffected
        guest_client, _ = start_guest(app)
        r = guest_client.post("/_probe")
        assert r.status_code == 403 and "read-only" in r.json()["detail"]
        assert guest_client.get("/metrics").status_code == 200


def test_guest_may_allows_reads_and_exactly_two_writes():
    assert guest_may("GET", "/anything") and guest_may("head", "/x") and guest_may("OPTIONS", "/x")
    assert guest_may("POST", "/chat") and guest_may("POST", "/chat/") and guest_may("POST", "/rag/search")
    for method, path in [("POST", "/emails"), ("POST", "/users"), ("PATCH", "/users/1"), ("DELETE", "/rag/documents/1"),
                         ("POST", "/rag/reindex"), ("POST", "/invoices/1/review"), ("POST", "/triage/1"), ("PUT", "/chat"),
                         ("POST", "/chat/status"), ("POST", "/rag/search/extra"), ("DELETE", "/chat")]:
        assert not guest_may(method, path), (method, path)


# ---------------------------------------------------------------- tokens cannot be forged or revived
def test_forged_and_stale_guest_tokens_are_rejected(world, guests_on, monkeypatch):
    good = "guest-0123456789ab"
    assert TestClient(world.client.app).get("/metrics", headers=bearer(forge({"sub": good, "tv": 1, "gst": True}))).status_code == 200
    wrong_secret = forge({"sub": good, "tv": 1, "gst": True}, secret="y" * 48)
    expired = forge({"sub": good, "tv": 1, "gst": True, "exp": datetime.now(timezone.utc) - timedelta(minutes=1)})
    wrong_epoch = forge({"sub": good, "tv": 2, "gst": True})
    impersonate_admin = forge({"sub": "admin1", "tv": 1, "gst": True})
    impersonate_id = forge({"sub": "1", "tv": 1, "gst": True})
    sloppy_name = forge({"sub": "guest-ZZZ", "tv": 1, "gst": True})
    for token in (wrong_secret, expired, wrong_epoch, impersonate_admin, impersonate_id, sloppy_name):
        assert TestClient(world.client.app).get("/metrics", headers=bearer(token)).status_code == 401


def test_a_guest_token_cannot_carry_a_role(world, guests_on):
    token = forge({"sub": "guest-0123456789ab", "tv": 1, "gst": True, "role": "admin"})
    c = TestClient(world.client.app)
    assert c.get("/auth/me", headers=bearer(token)).json()["role"] == "viewer"
    assert c.get("/users", headers=bearer(token)).status_code == 403


def test_raising_the_epoch_ends_every_open_guest_session(world, guests_on, monkeypatch):
    c, _ = start_guest(world)
    assert c.get("/metrics").status_code == 200
    monkeypatch.setattr(settings, "guest_epoch", 2)
    assert c.get("/metrics").status_code == 401


def test_turning_guest_access_off_ends_open_sessions(world, monkeypatch):
    monkeypatch.setattr(settings, "guest_enabled", True)
    c, _ = start_guest(world)
    assert c.get("/metrics").status_code == 200
    monkeypatch.setattr(settings, "guest_enabled", False)
    assert c.get("/metrics").status_code == 401


def test_cross_origin_requests_with_a_guest_cookie_are_blocked(world, guests_on):
    c, _ = start_guest(world)
    assert c.post("/chat", json={"message": "hi"}, headers={"Origin": "http://evil.example"}).status_code == 403


def test_a_guest_session_is_never_an_mcp_credential(world, guests_on):
    c, token = start_guest(world)
    assert world.rpc("tools/list", token=token).status_code == 401
    assert c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).status_code == 401      # cookie neither
    status = c.get("/mcp").text
    assert "tools_you_may_call" not in status and "Tools you may call" not in status           # the status page does not know this visitor


# ---------------------------------------------------------------- real accounts are untouched
def test_real_users_and_the_admin_area_work_as_before_with_guest_access_on(world, guests_on):
    admin = world.tokens["admin1"]
    h = bearer(admin)
    assert world.client.get("/users", headers=h).status_code == 200
    assert world.client.get("/audit", headers=h).status_code == 200
    assert world.client.get("/settings", headers=h).status_code == 200
    assert world.client.get("/review-queue", headers=bearer(world.tokens["rev1"])).status_code == 200
    ingest = world.client.post("/emails", json={"message_id": "real-1", "subject": "Invoice", "body": "Invoice INV-9 total 10 USD"},
                               headers=bearer(world.tokens["rev1"]))
    assert ingest.status_code == 200, ingest.text                                        # a real reviewer can still write
    assert world.client.post("/emails", json={"message_id": "x1", "subject": "s", "body": "b"}, headers=bearer(world.tokens["view1"])).status_code == 403
    login = TestClient(world.client.app).post("/auth/login", json={"username": "admin1", "password": PW})
    assert login.status_code == 200 and "access_token" in login.json()
    assert TestClient(world.client.app).get("/auth/me", headers=h).json() == {"auth_enabled": True, "username": "admin1", "role": "admin"}


def test_guest_activity_is_attributed_in_the_audit_log(world, guests_on):
    c, _ = start_guest(world)
    name = c.get("/auth/me").json()["username"]
    c.post("/auth/logout")
    with SessionLocal() as s:
        actors = [r.actor for r in s.query(AuditLog).filter_by(action="guest_login")]
    assert actors == [name]


# ---------------------------------------------------------------- the AI assistant and document Q&A for guests
def _guest_token(rag, guests=None):
    c, token = start_guest(rag.world)
    rag.world.tokens["guest"] = token
    return c, token


def test_a_guest_can_ask_the_assistant_with_viewer_tools_only(chat, guests_on):
    _guest_token(chat)
    i1 = chat.ids["i1"]
    r = chat.ask("What is invoice INV-1001?", who="guest",
                 script=[tc("get_invoice", {"invoice_id": i1}), say("Invoice INV-1001 from Acme Corp is approved: USD 1,250.50, due 2026-10-31.")])
    assert r.status_code == 200 and r.json()["verified"] and not r.json()["blocked"]
    assert set(chat.tool_names_offered()) == USER_TOOLS                      # exactly a viewer's tools: no audit log, queue, users
    asked = chat.ask("Show me the audit log", who="guest", script=[tc("get_audit_log", {}), say("Your role is not allowed to view the audit log.")]).json()
    assert asked["tools_used"][0]["outcome"] == "forbidden"
    status = chat.world.client.get("/chat/status", headers=bearer(chat.world.tokens["guest"])).json()
    assert status["enabled"] and set(status["tools"]) == USER_TOOLS


def test_guest_assistant_questions_have_a_per_session_budget(chat, guests_on, monkeypatch):
    monkeypatch.setattr(settings, "guest_chat_per_session", 2)
    _guest_token(chat)
    for _ in range(2):
        assert chat.ask("hi", who="guest", script=[say("Hello!")]).status_code == 200
    sent = len(chat.fake.requests)
    r = chat.ask("hi again", who="guest", script=[say("Hello!")])
    assert r.status_code == 429 and "AI questions" in r.json()["detail"]
    assert len(chat.fake.requests) == sent                                    # the model was not called
    assert chat.ask("hi", who="view1", script=[say("Hello!")]).status_code == 200      # real users have no guest budget
    assert chat.ask("hi", who="admin1", script=[say("Hello!")]).status_code == 200


def test_all_guests_share_a_hard_daily_ceiling_on_assistant_questions(chat, guests_on, monkeypatch):
    monkeypatch.setattr(settings, "guest_chat_per_day_global", 2)
    for _ in range(2):
        _guest_token(chat)
        assert chat.ask("hi", who="guest", script=[say("Hello!")]).status_code == 200
    _guest_token(chat)                                                        # a brand-new session cannot get around it
    r = chat.ask("hi", who="guest", script=[say("Hello!")])
    assert r.status_code == 429 and "daily limit" in r.json()["detail"] and int(r.headers["retry-after"]) >= 1


def test_a_guest_sees_and_searches_only_documents_open_to_everyone(rag, guests_on):
    rag.upload(name="policy.md", data=POLICY, min_role="viewer", title="AP Policy")
    rag.upload(name="onboarding.md", data=ONBOARDING, min_role="reviewer")
    rag.upload(name="exec.md", data=SECRET, min_role="admin")
    c, _ = _guest_token(rag)
    assert [d["filename"] for d in c.get("/rag/documents").json()] == ["policy.md"]
    assert c.get("/rag/status").json() == {"enabled": True, "documents": 1}
    hits = c.post("/rag/search", json={"query": "approval limit for a new vendor"}).json()
    assert hits["returned"] >= 1 and all(p["title"] == "AP Policy" for p in hits["passages"])
    leak = c.post("/rag/search", json={"query": "executive bonus pool"}).json()
    assert all("Executive" not in p["title"] and "4,500,000" not in p["text"] for p in leak["passages"])
    assert c.post("/rag/reindex").status_code == 403
    assert c.delete("/rag/documents/1").status_code == 403
    assert c.post("/rag/documents", files={"file": ("x.md", POLICY)}, data={"min_role": "viewer"}).status_code == 403


def test_a_guest_gets_a_cited_document_answer_and_never_a_restricted_one(rag, guests_on):
    rag.upload(name="ap_policy.md", data=POLICY, min_role="viewer", title="AP Policy")
    rag.upload(name="exec.md", data=SECRET, min_role="admin", title="Executive Compensation")
    _guest_token(rag)
    ref = next(p.ref for p in rag.svc.search("admin", "new vendor approval human review", 8).passages if "new vendor above 2,000 USD" in p.text)
    answer = f"An invoice from a new vendor above 2,000 USD must go to human review [{ref}]."
    body = rag.chat.ask("What does the policy say about new vendor invoices?", who="guest",
                        script=[tc("search_documents", {"query": "new vendor invoice approval"}), say(answer)]).json()
    assert body["answer"] == answer and body["verified"] and [c["ref"] for c in body["citations"]] == [ref]
    shown_to_model = str(rag.fake.requests)
    assert "4,500,000" not in shown_to_model and "Executive" not in shown_to_model


def test_guest_document_searches_have_a_per_session_budget(rag, guests_on, monkeypatch):
    monkeypatch.setattr(settings, "guest_search_per_session", 2)
    rag.upload(name="policy.md", data=POLICY, min_role="viewer")
    c, _ = _guest_token(rag)
    assert [c.post("/rag/search", json={"query": "payment terms"}).status_code for _ in range(3)] == [200, 200, 429]
    assert rag.search("payment terms", who="view1").status_code == 200


# ---------------------------------------------------------------- budget unit tests
def test_budget_counts_per_session_and_per_kind():
    b = guest_mod.GuestBudget()
    settings_cap = settings.guest_chat_per_session
    for _ in range(settings_cap):
        b.charge("guest-a", "chat")
    with pytest.raises(HTTPException) as e:
        b.charge("guest-a", "chat")
    assert e.value.status_code == 429
    b.charge("guest-b", "chat")                                               # another session is unaffected
    b.charge("guest-a", "search")                                             # another kind is unaffected


def test_budget_daily_ceiling_resets_at_midnight_utc(monkeypatch):
    monkeypatch.setattr(settings, "guest_chat_per_day_global", 2)
    now = [datetime(2026, 10, 26, 23, 0, tzinfo=timezone.utc)]
    b = guest_mod.GuestBudget(clock=lambda: now[0])
    b.charge("guest-a", "chat"); b.charge("guest-b", "chat")
    with pytest.raises(HTTPException) as e:
        b.charge("guest-c", "chat")
    assert 3000 <= int(e.value.headers["Retry-After"]) <= 3600
    now[0] = datetime(2026, 10, 27, 0, 1, tzinfo=timezone.utc)
    b.charge("guest-c", "chat")


def test_a_refused_question_does_not_use_up_the_daily_ceiling(monkeypatch):
    monkeypatch.setattr(settings, "guest_chat_per_day_global", 3)
    monkeypatch.setattr(settings, "guest_chat_per_session", 1)
    b = guest_mod.GuestBudget()
    b.charge("guest-a", "chat")
    for _ in range(5):
        with pytest.raises(HTTPException):
            b.charge("guest-a", "chat")
    b.charge("guest-b", "chat"); b.charge("guest-c", "chat")                   # 3 real questions in total, none lost to refusals


def test_budget_memory_is_bounded(monkeypatch):
    monkeypatch.setattr(guest_mod, "MAX_TRACKED_SESSIONS", 50)
    b = guest_mod.GuestBudget()
    for i in range(500):
        b.charge(f"guest-{i}", "search")
    assert len(b._used) == 50


def test_charge_is_a_no_op_for_real_users():
    b = guest_mod.budget
    for _ in range(100):
        guest_mod.charge(Principal("rev1", "reviewer", 2, "session"), "chat")
    assert b._used == {}


# ---------------------------------------------------------------- sample data
def test_sample_invoices_load_once_into_an_empty_database_and_never_wipe(monkeypatch):
    from app import demo
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    calls = []
    monkeypatch.setattr(demo, "seed", lambda pipeline: calls.append(1) or 5)
    assert demo.seed_guest_data(object()) == {"invoices": 5}
    with SessionLocal() as s:
        s.add(Email(message_id="real-1", sender="a@b.example", subject="s", body="b"))
        s.commit()
    assert demo.seed_guest_data(object()) == {"invoices": 0} and calls == [1]
    with SessionLocal() as s:
        assert s.query(Email).count() == 1                                   # nothing was dropped


def test_startup_seeds_sample_data_only_when_guest_access_is_on(monkeypatch):
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    from app import demo
    seen = []
    monkeypatch.setattr(demo, "seed_guest_data", lambda pipeline: seen.append("seeded") or {"invoices": 0})
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "jwt_secret", type(settings.jwt_secret)("s" * 48))
    with TestClient(create_app(light_pipeline)):
        pass
    assert seen == []                                                          # guest access off: nothing is seeded
    monkeypatch.setattr(settings, "guest_enabled", True)
    with TestClient(create_app(light_pipeline)):
        pass
    assert seen == ["seeded"]
    monkeypatch.setattr(settings, "guest_seed_on_start", False)
    with TestClient(create_app(light_pipeline)):
        pass
    assert seen == ["seeded"]


def test_sample_documents_are_indexed_for_everyone_once(rag, monkeypatch, tmp_path):
    from app import demo
    monkeypatch.setattr(settings, "guest_sample_docs_dir", str(tmp_path / "docs"))
    assert demo.seed_guest_documents() == 0                                    # no folder: nothing to do, no error
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "ap_policy.md").write_bytes(POLICY)
    (tmp_path / "docs" / "sop.md").write_bytes(ONBOARDING)
    assert demo.seed_guest_documents() == 2
    assert sorted(d["min_role"] for d in rag.svc.list_documents("admin")) == ["viewer", "viewer"]
    assert [d["filename"] for d in rag.svc.list_documents("viewer")].count("sop.md") == 1
    assert demo.seed_guest_documents() == 0                                    # a second start adds nothing
    monkeypatch.setattr(settings, "rag_enabled", False)
    assert demo.seed_guest_documents() == 0
