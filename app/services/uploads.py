"""Bounded storage and format validation without measurement calculations."""
import re
import shutil
import logging
from threading import RLock
import stat
import zipfile
from pathlib import Path, PurePosixPath
from uuid import uuid4

from sqlalchemy.orm import Session

from app.config import Settings
from app.models.upload import FileStatus, UploadedFile
from app.services.reader import ReaderError, ReaderLimits, inspect_shapefile, parse_kml
from app.services.errors import UploadError

logger = logging.getLogger(__name__)
STORAGE_LOCK = RLock()

CHUNK_SIZE = 64 * 1024
WINDOWS_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} | {
    f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10)
}


def is_reserved_path_component(value: str) -> bool:
    return value.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES
SHAPEFILE_EXTENSIONS = {".shp", ".shx", ".dbf", ".prj", ".cpg", ".sbn", ".sbx", ".qix"}


def validate_filename(filename: str | None) -> tuple[str, str]:
    if (
        not filename or len(filename) > 255 or filename in {".", ".."}
        or filename != filename.strip()
        or re.search(r'[<>:"/\\|?*\x00-\x1f\x7f]', filename)
        or filename.endswith(".") or is_reserved_path_component(filename)
    ):
        raise UploadError("INVALID_UPLOAD", "A safe, nonempty filename without path components is required")
    extension = Path(filename).suffix.lower()
    if extension not in {".kml", ".zip"}:
        raise UploadError("UNSUPPORTED_FORMAT", "Only .kml and .zip uploads are supported")
    return filename, extension


def validate_kml(path: Path, settings: Settings) -> None:
    try:
        parse_kml(path, ReaderLimits.from_settings(settings), retain_tree=False)
    except ReaderError as exc:
        raise UploadError("RESOURCE_LIMIT" if exc.code == "RESOURCE_LIMIT" else "INVALID_UPLOAD", str(exc)) from exc


def storage_size(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file() and not path.is_symlink())


def safe_cleanup(directory: Path, root: Path) -> None:
    """Validate the resolved child before recursive deletion; keep original errors."""
    with STORAGE_LOCK:
        try:
            resolved = directory.resolve()
            if resolved == root.resolve() or not resolved.is_relative_to(root.resolve()) or directory.is_symlink():
                logger.error("Refused cleanup outside owned upload storage")
                return
            if directory.exists():
                shutil.rmtree(directory)
        except OSError:
            logger.exception("Upload cleanup failed")


def validate_and_extract_zip(path: Path, destination: Path, settings: Settings) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if not entries or len(entries) > settings.max_zip_entries:
                raise UploadError("INVALID_UPLOAD", "Archive is empty or has too many entries")
            seen = set()
            planned = []
            declared_size = 0
            for entry in entries:
                name = entry.filename
                parts = PurePosixPath(name).parts
                if (
                    not name or "\\" in name or name.startswith("/")
                    or any(p in {".", ".."} or re.search(r'[<>:"|?*\x00-\x1f]', p)
                           or p.endswith((".", " ")) or is_reserved_path_component(p) for p in parts)
                    or stat.S_ISLNK(entry.external_attr >> 16)
                    or entry.flag_bits & 1
                ):
                    raise UploadError("INVALID_UPLOAD", "Unsafe or encrypted archive entry")
                target = destination.joinpath(*parts).resolve()
                if not target.is_relative_to(destination.resolve()):
                    raise UploadError("INVALID_UPLOAD", "Unsafe archive path")
                key = str(target).casefold()
                if key in seen:
                    raise UploadError("INVALID_UPLOAD", "Duplicate archive path")
                seen.add(key)
                if not entry.is_dir():
                    if target.suffix.lower() not in SHAPEFILE_EXTENSIONS:
                        raise UploadError("INVALID_UPLOAD", "Archive contains unsupported components")
                    declared_size += entry.file_size
                    if declared_size > settings.max_zip_uncompressed_bytes:
                        raise UploadError("UPLOAD_TOO_LARGE", "Expanded archive exceeds size limit")
                    if entry.file_size > max(entry.compress_size, 1) * settings.max_zip_compression_ratio:
                        raise UploadError("UPLOAD_TOO_LARGE", "Archive compression ratio exceeds limit")
                planned.append((entry, target))

            components = {}
            for entry, target in planned:
                if not entry.is_dir():
                    components.setdefault(str(target.with_suffix("")).casefold(), set()).add(target.suffix.lower())
            shape_sets = [key for key, extensions in components.items() if ".shp" in extensions]
            if len(shape_sets) != 1:
                raise UploadError("INVALID_UPLOAD", "Archive must contain exactly one Shapefile dataset")
            if not {".shp", ".shx", ".dbf"}.issubset(components[shape_sets[0]]):
                raise UploadError("INVALID_UPLOAD", "Shapefile requires matching .shp, .shx, and .dbf files")
            if len(components) != 1:
                raise UploadError("INVALID_UPLOAD", "Archive contains unmatched Shapefile components")

            if storage_size(settings.uploads_dir) + declared_size > settings.max_storage_bytes:
                raise UploadError("STORAGE_FULL", "Upload storage quota exceeded")
            extracted_size = 0
            for entry, target in planned:
                if entry.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(entry) as source, target.open("xb") as output:
                    while chunk := source.read(CHUNK_SIZE):
                        extracted_size += len(chunk)
                        if extracted_size > settings.max_zip_uncompressed_bytes:
                            raise UploadError("UPLOAD_TOO_LARGE", "Expanded archive exceeds size limit")
                        output.write(chunk)
            shapefile = next(target for entry, target in planned if target.suffix.lower() == ".shp")
            # Parsing belongs to the reader. Uploads do not persist features or measure them.
            try:
                inspect_shapefile(shapefile, ReaderLimits.from_settings(settings))
            except ReaderError as exc:
                raise UploadError("RESOURCE_LIMIT" if exc.code == "RESOURCE_LIMIT" else "INVALID_UPLOAD", "Shapefile dataset is not readable or exceeds resource limits") from exc
    except UploadError:
        raise
    except (zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError, EOFError, NotImplementedError) as exc:
        raise UploadError("INVALID_UPLOAD", "Malformed or unsupported ZIP archive") from exc
    except Exception as exc:
        # GDAL errors contain internal paths and must not be exposed to clients.
        from pyogrio.errors import DataSourceError, DataLayerError, FeatureError, GeometryError
        if isinstance(exc, (DataSourceError, DataLayerError, FeatureError, GeometryError, ValueError)):
            raise UploadError("INVALID_UPLOAD", "Shapefile dataset is not readable") from exc
        raise


def store_upload(filename: str | None, stream, settings: Settings, session: Session) -> UploadedFile:
    # Serialize quota accounting plus storage writes, not geospatial processing.
    with STORAGE_LOCK:
        return _store_upload(filename, stream, settings, session)


def _store_upload(filename, stream, settings: Settings, session: Session) -> UploadedFile:
    filename, extension = validate_filename(filename)
    upload_id = str(uuid4())
    directory = settings.uploads_dir.resolve() / upload_id
    path = directory / ("source" + extension)
    try:
        directory.mkdir(parents=True, exist_ok=False)
        occupied = storage_size(settings.uploads_dir)
        size = 0
        with path.open("xb") as output:
            while chunk := stream.read(CHUNK_SIZE):
                size += len(chunk)
                if size > settings.max_upload_bytes:
                    raise UploadError("UPLOAD_TOO_LARGE", "Upload exceeds size limit")
                if occupied + size > settings.max_storage_bytes:
                    raise UploadError("STORAGE_FULL", "Upload storage quota exceeded")
                output.write(chunk)
        if size == 0:
            raise UploadError("INVALID_UPLOAD", "Uploaded file is empty")
        if extension == ".kml":
            validate_kml(path, settings)
        else:
            validate_and_extract_zip(path, directory / "extracted", settings)
        record = UploadedFile(
            id=upload_id, filename=filename, format=extension[1:], size_bytes=size,
            storage_key=f"{upload_id}/source{extension}", status=FileStatus.PENDING,
        )
        session.add(record)
        session.commit()
        return record
    except Exception:
        session.rollback()
        # directory is a freshly generated child of the configured storage root.
        safe_cleanup(directory, settings.uploads_dir)
        raise
