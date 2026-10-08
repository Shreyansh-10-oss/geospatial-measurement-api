"""Thin upload and file-information endpoints."""
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.upload import UploadResponse
from app.schemas.file import FileInformation
from app.schemas.measurement import FeatureMeasurement
from app.schemas.analysis import DatasetAnalysis
from app.services.analysis import analyze_dataset
from app.services.files import FileNotFoundError, get_file_information, get_file_measurements
from app.services.processing import ProcessingError, upload_and_process
from app.services.errors import UploadError

router = APIRouter(prefix="/api/files", tags=["Files"])


@router.post("/", response_model=UploadResponse, status_code=201)
def upload_file(
    request: Request,
    file: Annotated[UploadFile, File()],
    session: Annotated[Session, Depends(get_db)],
) -> UploadResponse:
    try:
        record = upload_and_process(file.filename, file.file, request.app.state.settings, session)
    except UploadError as exc:
        status = {"UNSUPPORTED_FORMAT": 415, "UPLOAD_TOO_LARGE": 413, "RESOURCE_LIMIT": 413, "STORAGE_FULL": 507}.get(exc.code, 422)
        raise HTTPException(status, detail={"code": exc.code, "message": str(exc)}) from exc
    except ProcessingError as exc:
        status = 413 if exc.code == "RESOURCE_LIMIT" else (422 if exc.invalid_dataset else 500)
        raise HTTPException(status, detail={
            "id": exc.upload_id, "status": "FAILED", "code": exc.code, "message": str(exc),
        }) from exc
    finally:
        file.file.close()
    return UploadResponse.model_validate(record)


@router.get("/{file_id}/", response_model=FileInformation)
def file_information(
    file_id: UUID,
    session: Annotated[Session, Depends(get_db)],
) -> FileInformation:
    try:
        return FileInformation.model_validate(get_file_information(file_id, session))
    except FileNotFoundError as exc:
        raise HTTPException(404, detail="Uploaded file not found") from exc


@router.get("/{file_id}/measurements/", response_model=list[FeatureMeasurement])
def file_measurements(
    file_id: UUID,
    session: Annotated[Session, Depends(get_db)],
) -> list[FeatureMeasurement]:
    try:
        return [FeatureMeasurement.model_validate(feature)
                for feature in get_file_measurements(file_id, session)]
    except FileNotFoundError as exc:
        raise HTTPException(404, detail="Uploaded file not found") from exc


@router.get(
    "/{file_id}/analysis/", response_model=DatasetAnalysis,
    summary="Optional dataset analysis",
    description="Optional enhancement beyond assignment requirements. Aggregates persisted results without reprocessing files.",
)
def file_analysis(
    file_id: UUID,
    session: Annotated[Session, Depends(get_db)],
) -> DatasetAnalysis:
    try:
        return DatasetAnalysis.model_validate(analyze_dataset(file_id, session))
    except FileNotFoundError as exc:
        raise HTTPException(404, detail="Uploaded file not found") from exc
