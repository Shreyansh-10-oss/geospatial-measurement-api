"""Synchronous orchestration of persisted upload processing."""
from datetime import datetime, timezone, timedelta
from uuid import UUID
import logging
import math

from shapely.geometry import mapping
from sqlalchemy import delete, update, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models.feature import FeatureResult
from app.models.upload import FileStatus, UploadedFile
from app.services.crs import CRSResolutionError, resolve_measurement_crs
from app.services.measurements import MeasurementResult, MeasurementStatus, measure_geometry
from app.services.reader import GeometryStatus, ReaderError, ReaderLimits, read_geospatial_file
from app.services.uploads import store_upload, safe_cleanup

logger = logging.getLogger(__name__)


class ProcessingError(Exception):
    def __init__(self, upload_id, code, message, invalid_dataset):
        self.upload_id = upload_id
        self.code = code
        self.invalid_dataset = invalid_dataset
        super().__init__(message)


def _json_safe(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _measure_feature(feature):
    if feature.geometry_status == GeometryStatus.PARSE_ERROR:
        return MeasurementResult(feature.feature_index, feature.geometry_type,
                                 MeasurementStatus.INVALID_GEOMETRY, error_code="GEOMETRY_PARSE_ERROR")
    if feature.geometry_status == GeometryStatus.INVALID:
        return MeasurementResult(feature.feature_index, feature.geometry_type,
                                 MeasurementStatus.INVALID_GEOMETRY, error_code="INVALID_GEOMETRY")
    if feature.geometry_status == GeometryStatus.UNSUPPORTED:
        return MeasurementResult(feature.feature_index, feature.geometry_type, MeasurementStatus.UNSUPPORTED)
    geometry = feature.geometry
    # Skip CRS selection for types that do not need a measurement or cannot be measured.
    if geometry is None or geometry.is_empty or not geometry.is_valid or geometry.geom_type == "Point":
        return measure_geometry(geometry, None, feature_index=feature.feature_index)
    try:
        resolution = resolve_measurement_crs(geometry, feature.source_crs)
    except CRSResolutionError as exc:
        return MeasurementResult(feature.feature_index, feature.geometry_type, MeasurementStatus.FAILED,
                                 warnings=exc.warnings, error_code=exc.code)
    return measure_geometry(geometry, resolution, feature_index=feature.feature_index)


def process_upload(record: UploadedFile, settings: Settings, session: Session) -> UploadedFile:
    upload_id = record.id
    try:
        record.status = FileStatus.PROCESSING
        session.commit()
        directory = settings.uploads_dir.resolve() / upload_id
        path = directory / "extracted" if record.format == "zip" else directory / "source.kml"
        dataset = read_geospatial_file(path, limits=ReaderLimits.from_settings(settings))
        record.source_crs = dataset.source_crs.to_string() if dataset.source_crs else None
        record.discovered_feature_count = len(dataset.features)
        record.warnings = dataset.warnings
        session.commit()
        if dataset.features and dataset.source_crs is None:
            raise ReaderError("MISSING_CRS", "Source CRS is required to process this dataset")
        source_crs = dataset.source_crs.to_string() if dataset.source_crs else None
        measured = skipped = failed = 0
        for feature in dataset.features:
            measurement = _measure_feature(feature)
            feature_warnings = list(dict.fromkeys(feature.warnings + measurement.warnings))
            if measurement.status == MeasurementStatus.MEASURED:
                measured += 1
            elif measurement.status in {MeasurementStatus.NOT_REQUIRED, MeasurementStatus.UNSUPPORTED, MeasurementStatus.EMPTY_GEOMETRY}:
                skipped += 1
            else:
                failed += 1
            session.add(FeatureResult(
                file_id=upload_id, feature_index=feature.feature_index, source_feature_id=feature.source_feature_id,
                geometry_type=feature.geometry_type,
                geometry=_json_safe(mapping(feature.geometry)) if feature.geometry is not None else feature.raw_geometry,
                properties=_json_safe(feature.properties), source_crs=source_crs,
                measurement_crs=measurement.measurement_crs, projection_strategy=measurement.projection_strategy,
                status=measurement.status.value, measurement_type=measurement.measurement_type,
                value=measurement.value, unit=measurement.unit, warnings=feature_warnings,
                error_code=measurement.error_code,
            ))
        record.feature_count = len(dataset.features)
        record.measured_count, record.skipped_count, record.failed_count = measured, skipped, failed
        record.source_crs = source_crs
        record.warnings = dataset.warnings + (["FEATURE_FAILURES"] if failed else [])
        record.completed_at = datetime.now(timezone.utc)
        record.status = FileStatus.COMPLETED
        session.commit()
        return record
    except Exception as exc:
        session.rollback()
        known = isinstance(exc, ReaderError)
        code = exc.code if known else "PROCESSING_FAILED"
        message = "Source CRS is missing; dataset processing cannot continue" if code == "MISSING_CRS" else "Unable to process uploaded geospatial dataset"
        logger.exception("Upload processing failed: %s", upload_id)
        # Separate transaction also handles a failed ORM commit without leaving partial results.
        with session.get_bind().begin() as connection:
            connection.execute(delete(FeatureResult).where(FeatureResult.file_id == upload_id))
            connection.execute(update(UploadedFile).where(UploadedFile.id == upload_id).values(
                status=FileStatus.FAILED, completed_at=datetime.now(timezone.utc),
                feature_count=0, measured_count=0, skipped_count=0, failed_count=0,
                error_code=code, error_message=message,
            ))
        session.expire_all()
        raise ProcessingError(upload_id, code, message, known) from exc
    finally:
        safe_cleanup(settings.uploads_dir.resolve() / upload_id / "extracted", settings.uploads_dir)


def upload_and_process(filename, stream, settings: Settings, session: Session) -> UploadedFile:
    record = store_upload(filename, stream, settings, session)
    return process_upload(record, settings, session)


def recover_interrupted_processing(engine, settings: Settings) -> None:
    """Single-process startup recovery after an interrupted processing request."""
    with engine.begin() as connection:
        interrupted = connection.execute(
            update(UploadedFile).where(UploadedFile.status.in_([FileStatus.PENDING, FileStatus.PROCESSING])).values(
                status=FileStatus.FAILED, error_code="PROCESSING_INTERRUPTED",
                error_message="Processing was interrupted; upload the file again",
                completed_at=datetime.now(timezone.utc),
                feature_count=0, measured_count=0, skipped_count=0, failed_count=0,
            )
        )
        if interrupted.rowcount:
            logger.warning("Recovered %s interrupted uploads", interrupted.rowcount)

    # Exclusive ownership guarantees no active request owns an orphan at startup.
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.failed_upload_retention_days)
    with engine.begin() as connection:
        expired = connection.scalars(select(UploadedFile.id).where(
            UploadedFile.status == FileStatus.FAILED, UploadedFile.completed_at < cutoff
        )).all()
        if expired:
            connection.execute(delete(UploadedFile).where(UploadedFile.id.in_(expired)))
        retained = set(connection.scalars(select(UploadedFile.id)))
    for directory in settings.uploads_dir.iterdir():
        if not directory.is_dir() or directory.is_symlink():
            continue
        try:
            UUID(directory.name)
        except ValueError:
            continue
        if directory.name not in retained:
            safe_cleanup(directory, settings.uploads_dir)
        else:
            safe_cleanup(directory / "extracted", settings.uploads_dir)
