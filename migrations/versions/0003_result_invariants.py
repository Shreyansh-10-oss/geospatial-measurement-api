"""Enforce upload and measurement invariants on fresh and adopted schemas."""
from alembic import op
import sqlalchemy as sa
revision = "0003"
down_revision = "0002"
branch_labels = depends_on = None
CHECKS = {'uploaded_files': [('upload_nonnegative', 'size_bytes >= 0 AND feature_count >= 0 AND discovered_feature_count >= 0 AND measured_count >= 0 AND skipped_count >= 0 AND failed_count >= 0'), ('upload_counts', 'feature_count = measured_count + skipped_count + failed_count'), ('upload_status', "status IN ('PENDING','PROCESSING','COMPLETED','FAILED')")], 'feature_results': [('feature_index_nonnegative', 'feature_index >= 0'), ('feature_measurement', "(status = 'MEASURED' AND geometry_type IS NOT NULL AND measurement_type IS NOT NULL AND unit IS NOT NULL AND value IS NOT NULL AND value >= 0 AND value <= 1.7976931348623157e308 AND measurement_crs IS NOT NULL AND projection_strategy IS NOT NULL AND ((geometry_type = 'Polygon' AND measurement_type = 'area' AND unit = 'square_meters') OR (geometry_type = 'LineString' AND measurement_type = 'length' AND unit = 'meters'))) OR (status != 'MEASURED' AND value IS NULL AND measurement_type IS NULL AND unit IS NULL)"), ('feature_status', "status IN ('MEASURED','NOT_REQUIRED','UNSUPPORTED','EMPTY_GEOMETRY','INVALID_GEOMETRY','FAILED')")]}


def upgrade():
    for table, checks in CHECKS.items():
        existing = {constraint["name"] for constraint in sa.inspect(op.get_bind()).get_check_constraints(table)}
        with op.batch_alter_table(table, recreate="always") as batch:
            for name, expression in checks:
                if name not in existing:
                    batch.create_check_constraint(name, expression)


def downgrade():
    raise RuntimeError("Destructive downgrade is not supported")
