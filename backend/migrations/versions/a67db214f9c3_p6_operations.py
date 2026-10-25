"""P6 执行实例登记、清理审计与服务心跳。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import DATETIME

revision = "a67db214f9c3"
down_revision = "f56ca103e8b2"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "graph_execution_instances",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column(
            "input_id",
            sa.String(32),
            sa.ForeignKey("negotiation_inputs.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("lease_token", sa.String(64)),
        sa.Column("ownership_epoch", sa.Integer()),
        sa.Column("pg_pid", sa.Integer(), nullable=False),
        sa.Column("pg_backend_start", sa.String(64), nullable=False),
        sa.Column("application_name", sa.String(64), nullable=False),
        sa.Column("registered_at", DATETIME(fsp=6), nullable=False),
        sa.Column("checked_at", DATETIME(fsp=6)),
        sa.Column("state", sa.String(24), nullable=False, index=True),
        sa.Column("error_category", sa.String(64)),
    )
    op.create_table(
        "graph_service_heartbeats",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("service", sa.String(24), nullable=False, index=True),
        sa.Column("last_seen", DATETIME(fsp=6), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
    )


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT COUNT(*) FROM graph_execution_instances")):
        raise RuntimeError("P6 execution audit exists; refusing to erase recovery evidence")
    op.drop_table("graph_service_heartbeats")
    op.drop_table("graph_execution_instances")
