"""Sample data for the public demo. Dates are relative to today, so the demo never looks stale
(a hard-coded date would eventually look old, or lie in the future, next to the validation rules)."""
import random
from datetime import date, datetime, timedelta, timezone

from app import auth
from app.ml import registry
from app.config import settings
from app.db import Base, engine
from app.services import processing


def demo_emails(today: date | None = None, seed: int = 7) -> list[dict]:
    rng = random.Random(seed)
    today = today or date.today()
    out: list[dict] = []

    def mk(vendor, number, amount, days_ago, no_due=False, tag=None):
        d = today - timedelta(days=days_ago)
        due = "" if no_due else f"Due Date: {(d + timedelta(days=30)).isoformat()}\n"
        body = (f"Hello,\n\nPlease find invoice {number} attached.\nVendor: {vendor}\n"
                f"Invoice Date: {d.isoformat()}\n{due}Total Amount: USD {amount:.2f}\n\nRegards,\n{vendor}")
        out.append({"message_id": f"seed-{len(out):03d}", "sender": f"billing@{vendor.split()[0].lower()}.example",
                    "subject": f"Invoice {number}", "body": body,
                    "received_at": datetime.combine(d, datetime.min.time(), tzinfo=timezone.utc)})

    num = lambda: f"INV-{rng.randint(10000, 99999)}"
    # a few weeks of ordinary history, oldest first, so vendor baselines exist
    for i, a in enumerate([1000, 1040, 980, 1020, 1010]):
        mk("Acme Supplies Ltd", num(), a + rng.randint(0, 99) / 100, 40 - i * 4)
    for i, a in enumerate([450, 470, 430, 465]):
        mk("BrightCloud Hosting", num(), a + rng.randint(0, 99) / 100, 36 - i * 5)
    for i, a in enumerate([2900, 3100, 3000, 3200]):
        mk("Northwind Logistics", num(), a + rng.randint(0, 99) / 100, 34 - i * 6)
    # the interesting ones
    mk("Acme Supplies Ltd", num(), 6400.00, 3)                              # unusual amount for this vendor
    dup = num()
    mk("BrightCloud Hosting", dup, 512.40, 2)
    mk("BrightCloud Hosting", dup, 512.40, 1)                               # exact duplicate
    mk("Northwind Logistics", num(), 3050.00, 1, no_due=True)               # missing due date
    mk("Quantum Freight GmbH", num(), 5200.00, 1)                           # first invoice from an unknown vendor
    out.append({"message_id": f"seed-{len(out):03d}", "sender": "events@cloudconf.example",
                "subject": "Webinar invitation", "received_at": datetime.now(timezone.utc),
                "body": "Hi there,\n\nJoin our free webinar on cloud cost optimisation next Thursday.\n\nCheers,\nThe CloudConf team"})
    return out


def seed(pipeline) -> int:
    emails = demo_emails()
    for e in emails:
        processing.process_email(pipeline, e)
    return len(emails)


def reset_and_seed(pipeline) -> int:
    """Wipe everything and load the sample data again (used by the demo's scheduled reset)."""
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    if settings.auth_enabled:
        auth.seed_demo_users()
    try:
        registry.sync_registry()                      # the reset dropped the registry table too
    except Exception:
        pass
    return seed(pipeline)


def seed_guest_data(pipeline) -> dict:
    """Guest showcase: load the sample invoices once, only into an EMPTY database. Never wipes anything (unlike demo mode), so
    real users and any documents an admin added are untouched. Returns what was loaded."""
    from app.db import Email, SessionLocal
    with SessionLocal() as s:
        empty = s.query(Email).first() is None
    return {"invoices": seed(pipeline) if empty else 0}


def seed_guest_documents(actor: str = "guest-seed") -> int:
    """Guest showcase: index the sample policy documents (readable by every signed-in role) so document Q&A has something to
    answer from. Skipped when document search is off or any document already exists. Returns how many were stored."""
    if not settings.rag_enabled:
        return 0
    from pathlib import Path
    from app.rag.extract import SUPPORTED
    from app.rag.service import RagError, get_service
    folder = Path(settings.guest_sample_docs_dir)
    files = sorted(p for p in folder.glob("*") if p.is_file() and p.suffix.lower() in SUPPORTED) if folder.is_dir() else []
    if not files:
        return 0
    svc = get_service()
    if svc.list_documents("admin"):
        return 0
    stored = 0
    for f in files:
        try:
            svc.ingest(data=f.read_bytes(), filename=f.name, title=None, min_role="viewer", actor=actor)
            stored += 1
        except RagError:
            continue
    return stored
