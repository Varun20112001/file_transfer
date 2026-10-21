"""Database models and batched audit-event persistence for SBM."""

import logging
import threading
from datetime import datetime, timezone

from flask_login import UserMixin
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import insert


logger = logging.getLogger(__name__)
db = SQLAlchemy()
migrate = Migrate()


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(16), nullable=False, default="user")
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    def get_id(self):
        return str(self.id)


class AuditEvent(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="SET NULL"), nullable=True, index=True)
    action = db.Column(db.String(16), nullable=False, index=True)
    drive = db.Column(db.String(3), nullable=False)
    path = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, index=True)
    user = db.relationship(User, lazy="joined")


class AuditBuffer:
    """Thread-safe in-memory audit queue, bulk committed at a record threshold or timer."""

    def __init__(self, batch_size=20):
        self.batch_size = batch_size
        self._lock = threading.Lock()
        self._pending = []

    def add(self, app, user_id, action, drive, path):
        event = {
            "user_id": user_id,
            "action": action,
            "drive": drive,
            "path": path,
            "created_at": datetime.now(timezone.utc),
        }
        with self._lock:
            self._pending.append(event)
            should_flush = len(self._pending) >= self.batch_size
        if should_flush:
            self.flush(app)

    def flush(self, app):
        with self._lock:
            if not self._pending:
                return 0
            batch = self._pending
            self._pending = []

        try:
            with app.app_context():
                db.session.execute(insert(AuditEvent), batch)
                db.session.commit()
            return len(batch)
        except Exception:
            with app.app_context():
                db.session.rollback()
            with self._lock:
                self._pending = batch + self._pending
            logger.exception("Could not persist the audit-event batch; it remains queued")
            return 0

    def pending_count(self):
        with self._lock:
            return len(self._pending)


audit_buffer = AuditBuffer(batch_size=20)


class AuditFlushWorker:
    def __init__(self, app, interval_seconds):
        self.app = app
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, args=(interval_seconds,), name="sbm-audit-flusher", daemon=True)
        self.thread.start()

    def _run(self, interval_seconds):
        while not self.stop_event.wait(interval_seconds):
            audit_buffer.flush(self.app)

    def shutdown(self):
        self.stop_event.set()
        self.thread.join(timeout=2)
        audit_buffer.flush(self.app)


def start_audit_flush_worker(app, interval_seconds=300):
    """Flush queued audit events every five minutes and once on clean shutdown."""
    import atexit
    worker = AuditFlushWorker(app, interval_seconds)
    atexit.register(worker.shutdown)
    return worker
