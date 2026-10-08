"""Baseline upload table; adopt the original scaffold without deleting records."""
from alembic import op
import sqlalchemy as sa
revision = "0001"
down_revision = None
branch_labels = depends_on = None


def upgrade():
    if not sa.inspect(op.get_bind()).has_table("uploaded_files"):
        op.create_table("uploaded_files",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("filename", sa.String(255), nullable=False),
            sa.Column("format", sa.String(10), nullable=False),
            sa.Column("size_bytes", sa.Integer(), nullable=False),
            sa.Column("storage_key", sa.String(100), nullable=False),
            sa.Column("status", sa.String(10), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))


def downgrade():
    raise RuntimeError("Destructive downgrade is not supported")
