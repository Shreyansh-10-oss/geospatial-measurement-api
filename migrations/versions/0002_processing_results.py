"""Add processing metadata and results; adopt prior unversioned processed schema."""
from alembic import op
import sqlalchemy as sa
revision = "0002"
down_revision = "0001"
branch_labels = depends_on = None


def upgrade():
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("uploaded_files")}
    columns = [
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        *[sa.Column(name, sa.Integer(), nullable=False, server_default="0") for name in
          ("feature_count", "discovered_feature_count", "measured_count", "skipped_count", "failed_count")],
        sa.Column("source_crs", sa.Text()),
        sa.Column("warnings", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("error_code", sa.Text()), sa.Column("error_message", sa.Text()),
    ]
    for column in columns:
        if column.name not in existing:
            op.add_column("uploaded_files", column)
    op.execute("UPDATE uploaded_files SET discovered_feature_count=feature_count WHERE discovered_feature_count=0 AND feature_count>0")
    if not sa.inspect(op.get_bind()).has_table("feature_results"):
        op.create_table("feature_results",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("file_id", sa.String(36), sa.ForeignKey("uploaded_files.id", ondelete="CASCADE"), nullable=False),
            sa.Column("feature_index", sa.Integer(), nullable=False),
            *[sa.Column(name, sa.Text(), nullable=True) for name in
              ("source_feature_id", "geometry_type", "source_crs", "measurement_crs", "projection_strategy", "measurement_type", "unit", "error_code")],
            sa.Column("geometry", sa.JSON()), sa.Column("properties", sa.JSON(), nullable=False),
            sa.Column("status", sa.Text(), nullable=False), sa.Column("value", sa.Float()),
            sa.Column("warnings", sa.JSON(), nullable=False),
            sa.UniqueConstraint("file_id", "feature_index"))
        op.create_index("ix_feature_results_file_id", "feature_results", ["file_id"])


def downgrade():
    raise RuntimeError("Destructive downgrade is not supported")
