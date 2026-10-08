"""Public persisted feature-measurement response."""
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from app.services.measurements import MeasurementStatus


class FeatureMeasurement(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    feature_index: int = Field(ge=0)
    geometry_type: str | None
    properties: dict[str, Any]
    status: MeasurementStatus
    measurement_type: Literal["area", "length"] | None
    measurement_value: float | None = Field(validation_alias="value", ge=0, allow_inf_nan=False)
    measurement_unit: Literal["square_meters", "meters"] | None = Field(validation_alias="unit")
    source_crs: str | None
    measurement_crs: str | None
    projection_strategy: str | None
    warnings: list[str]
    error_code: str | None

    @model_validator(mode="after")
    def coherent_measurement(self):
        if self.status == MeasurementStatus.MEASURED:
            expected = {"Polygon": ("area", "square_meters"), "LineString": ("length", "meters")}.get(self.geometry_type)
            if (self.measurement_value is None or expected != (self.measurement_type, self.measurement_unit)
                or not self.measurement_crs or not self.projection_strategy):
                raise ValueError("Measured result requires a coherent value, unit and projected CRS metadata")
        elif any(value is not None for value in (self.measurement_value, self.measurement_type, self.measurement_unit)):
            raise ValueError("Unmeasured results cannot contain measurement values")
        return self
