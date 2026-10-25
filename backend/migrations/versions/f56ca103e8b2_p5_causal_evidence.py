"""P5 transaction-bound causal evidence, retained with business receipts."""

import sqlalchemy as sa
from alembic import op

revision = "f56ca103e8b2"
down_revision = "e45b9c02d7a1"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "business_causal_events",
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column("action_id", sa.String(64), nullable=False, index=True),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("stage", sa.String(32), nullable=False),
        sa.Column("parent_id", sa.String(80)),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.UniqueConstraint("action_id", "sequence"),
    )


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT COUNT(*) FROM business_causal_events")):
        raise RuntimeError("P5 causal evidence exists; refusing to erase authorization evidence")
    op.drop_table("business_causal_events")
