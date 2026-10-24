"""Small real-MySQL protocol fixture, isolated from the production ORM schema."""

import hashlib
import json
import time
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Column,
    Float,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    UniqueConstraint,
    create_engine,
    insert,
    select,
    update,
)

from app.services.pricing_service import OfferTerms, PricingPolicy, ShippingPayer, evaluate_offer

metadata = MetaData()
sessions = Table(
    "p0_sessions",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("cursor", Integer, nullable=False, default=0),
    Column("next_seq", Integer, nullable=False, default=1),
    Column("rounds", Integer, nullable=False, default=0),
    Column("version", Integer, nullable=False, default=1),
)
inputs = Table(
    "p0_inputs",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("session_id", String(64), nullable=False),
    Column("seq", Integer, nullable=False),
    Column("request_id", String(64), nullable=False),
    Column("payload", JSON, nullable=False),
    Column("payload_hash", String(64), nullable=False),
    Column("status", String(32), nullable=False, default="PENDING"),
    Column("token", String(64)),
    Column("epoch", Integer, nullable=False, default=0),
    Column("lease_until", Float(53), nullable=False, default=0),
    UniqueConstraint("session_id", "seq"),
    UniqueConstraint("session_id", "request_id"),
)
products = Table(
    "p0_products",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("title", String(128)),
    Column("price", Numeric(12, 2)),
)
offers = Table(
    "p0_offers",
    metadata,
    Column("source", String(64), primary_key=True),
    Column("amount", Numeric(12, 2)),
)
approvals = Table(
    "p0_approvals",
    metadata,
    Column("source", String(64), primary_key=True),
    Column("status", String(32)),
)
receipts = Table(
    "p0_receipts",
    metadata,
    Column("source", String(64), primary_key=True),
    Column("payload_hash", String(64)),
    Column("result", JSON),
)
notifications = Table(
    "p0_notifications",
    metadata,
    Column("source", String(64), primary_key=True),
    Column("status", String(32)),
)
outbox = Table(
    "p0_outbox",
    metadata,
    Column("source", String(64), primary_key=True),
    Column("event", String(32)),
)
artifacts = Table(
    "p0_artifacts",
    metadata,
    Column("key", String(64), primary_key=True),
    Column("run_id", String(64)),
    Column("input_hash", String(64)),
    Column("config_hash", String(64)),
    Column("value", JSON),
    Column("value_hash", String(64)),
)
attempts = Table(
    "p0_attempts",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("run_id", String(64)),
    Column("accepted", Integer),
)
messages = Table(
    "p0_messages",
    metadata,
    Column("source", String(64), primary_key=True),
    Column("body", String(256)),
)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class StaleOwner(RuntimeError):
    pass


class InjectedFailure(RuntimeError):
    pass


class Store:
    def __init__(self, url):
        self.engine = create_engine(
            url,
            pool_pre_ping=True,
            pool_size=3,
            max_overflow=1,
            connect_args={"connect_timeout": 3, "read_timeout": 5, "write_timeout": 5},
        )

    def seed(self):
        sid = uuid4().hex
        with self.engine.begin() as db:
            db.execute(insert(sessions).values(id=sid))
            db.execute(insert(products).values(id=sid, title="P0 synthetic phone", price=900))
        return sid

    def enqueue(self, sid, request_id, payload):
        with self.engine.begin() as db:
            session = (
                db.execute(select(sessions).where(sessions.c.id == sid).with_for_update())
                .mappings()
                .one()
            )
            old = (
                db.execute(
                    select(inputs).where(
                        inputs.c.session_id == sid, inputs.c.request_id == request_id
                    )
                )
                .mappings()
                .first()
            )
            if old:
                if old["payload_hash"] != digest(payload):
                    raise ValueError("request payload conflict")
                return dict(old)
            run = dict(
                id=uuid4().hex,
                session_id=sid,
                seq=session["next_seq"],
                request_id=request_id,
                payload=payload,
                payload_hash=digest(payload),
            )
            db.execute(insert(inputs).values(**run))
            db.execute(
                update(sessions)
                .where(sessions.c.id == sid)
                .values(next_seq=session["next_seq"] + 1)
            )
            return run

    def read_run(self, run_id):
        with self.engine.connect() as db:
            return dict(db.execute(select(inputs).where(inputs.c.id == run_id)).mappings().one())

    def claim(self, run_id, *, lease_seconds=30):
        with self.engine.begin() as db:
            run = (
                db.execute(select(inputs).where(inputs.c.id == run_id).with_for_update())
                .mappings()
                .one()
            )
            session = (
                db.execute(
                    select(sessions).where(sessions.c.id == run["session_id"]).with_for_update()
                )
                .mappings()
                .one()
            )
            if session["cursor"] + 1 != run["seq"] or run["lease_until"] > time.time():
                return None
            token, epoch = uuid4().hex, run["epoch"] + 1
            db.execute(
                update(inputs)
                .where(inputs.c.id == run_id)
                .values(
                    token=token,
                    epoch=epoch,
                    lease_until=time.time() + lease_seconds,
                    status="RUNNING",
                )
            )
            return token, epoch

    def owner(self, db, run_id, token, epoch):
        run = (
            db.execute(select(inputs).where(inputs.c.id == run_id).with_for_update())
            .mappings()
            .one()
        )
        if run["token"] != token or run["epoch"] != epoch or run["lease_until"] <= time.time():
            raise StaleOwner(run_id)
        return run

    def revoke(self, run_id):
        with self.engine.begin() as db:
            db.exec_driver_sql("SET SESSION innodb_lock_wait_timeout=2")
            run = (
                db.execute(select(inputs).where(inputs.c.id == run_id).with_for_update())
                .mappings()
                .one()
            )
            db.execute(
                update(inputs)
                .where(inputs.c.id == run_id)
                .values(token=None, epoch=run["epoch"] + 1, lease_until=0, status="RETRY_WAIT")
            )

    def command_in_transaction(self, db, run_id, token, epoch, *, amount="800.00", fail_at=0):
        """Caller owns transaction; service never commits a partial combination."""
        run = self.owner(db, run_id, token, epoch)
        payload_hash = digest({"amount": amount, "input": run["payload_hash"]})
        old = db.execute(select(receipts).where(receipts.c.source == run_id)).mappings().first()
        if old:
            if old["payload_hash"] != payload_hash:
                raise ValueError("action payload conflict")
            return old["result"]
        session = (
            db.execute(select(sessions).where(sessions.c.id == run["session_id"]).with_for_update())
            .mappings()
            .one()
        )
        evaluation = evaluate_offer(
            terms=OfferTerms(Decimal(amount), ShippingPayer.BUYER),
            policy=PricingPolicy(Decimal("750"), Decimal("850")),
        )
        if not evaluation.can_request_approval:
            raise ValueError("P0 fixture command only supports approval zone")
        result = {"source": run_id, "status": "WAITING_APPROVAL", "amount": amount}
        operations = [
            insert(offers).values(source=run_id, amount=Decimal(amount)),
            insert(approvals).values(source=run_id, status="PENDING"),
            update(sessions)
            .where(sessions.c.id == run["session_id"])
            .values(rounds=session["rounds"] + 1, version=session["version"] + 1),
            insert(receipts).values(source=run_id, payload_hash=payload_hash, result=result),
            insert(notifications).values(source=run_id, status="PENDING"),
            insert(outbox).values(source=run_id, event="APPROVAL_REQUESTED"),
        ]
        for index, operation in enumerate(operations, start=1):
            db.execute(operation)
            if fail_at == index:
                raise InjectedFailure(f"after write {index}")
        return result

    def command(self, run_id, token, epoch, **kwargs):
        with self.engine.begin() as db:
            return self.command_in_transaction(db, run_id, token, epoch, **kwargs)

    def model_artifact(self, run_id, token, epoch, call, *, config_hash="p0-config-v1"):
        run = self.read_run(run_id)
        key = digest([run_id, "semantic", run["payload_hash"], config_hash])
        with self.engine.begin() as db:
            self.owner(db, run_id, token, epoch)
            old = db.execute(select(artifacts).where(artifacts.c.key == key)).mappings().first()
            if old:
                if digest(old["value"]) != old["value_hash"]:
                    raise ValueError("artifact content mismatch")
                return old["value"]
        value = call()  # No MySQL transaction held during an external call.
        accepted = False
        try:
            with self.engine.begin() as db:
                self.owner(db, run_id, token, epoch)
                db.execute(
                    insert(artifacts).values(
                        key=key,
                        run_id=run_id,
                        input_hash=run["payload_hash"],
                        config_hash=config_hash,
                        value=value,
                        value_hash=digest(value),
                    )
                )
                accepted = True
        finally:
            with self.engine.begin() as db:
                db.execute(
                    insert(attempts).values(id=uuid4().hex, run_id=run_id, accepted=int(accepted))
                )
        return value

    def publish(self, run_id, token, epoch):
        with self.engine.begin() as db:
            self.owner(db, run_id, token, epoch)
            if not db.execute(select(messages).where(messages.c.source == run_id)).first():
                db.execute(insert(messages).values(source=run_id, body="P0 verified result"))

    def finalize(self, run_id, token, epoch, values, pending):
        if values.get("run_id") != run_id or pending:
            raise ValueError("END evidence does not match run")
        with self.engine.begin() as db:
            run = self.owner(db, run_id, token, epoch)
            if not db.execute(select(messages).where(messages.c.source == run_id)).first():
                raise ValueError("missing unique public result")
            session = (
                db.execute(
                    select(sessions).where(sessions.c.id == run["session_id"]).with_for_update()
                )
                .mappings()
                .one()
            )
            if session["cursor"] == run["seq"]:
                return
            if session["cursor"] + 1 != run["seq"]:
                raise ValueError("historical task conflict")
            db.execute(
                update(sessions).where(sessions.c.id == run["session_id"]).values(cursor=run["seq"])
            )
            db.execute(update(inputs).where(inputs.c.id == run_id).values(status="COMPLETED"))
