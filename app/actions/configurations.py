import json
from enum import Enum
from typing import List, Optional

from pydantic import SecretStr, root_validator, validator

from app.services.utils import FieldWithUIOptions, GlobalUISchemaOptions, UIOptions

from . import boundaries
from .client import FIRMS_REGIONS
from .core import AuthActionConfiguration, ExecutableActionMixin, PullActionConfiguration

FirmsRegion = Enum("FirmsRegion", {region: region for region in FIRMS_REGIONS}, type=str)
FirmsRegion.__doc__ = "A region NASA publishes FIRMS near-real-time day files for."


class FirmsSatellite(str, Enum):
    """The VIIRS satellites whose detections can be read. Suomi NPP stopped delivering in November 2026."""

    NOAA_20 = "noaa-20"
    NOAA_21 = "noaa-21"


class ConfidenceLevel(str, Enum):
    LOW = "low"
    NOMINAL = "nominal"
    HIGH = "high"


class AuthenticateConfig(AuthActionConfiguration, ExecutableActionMixin):
    use_own_key: bool = FieldWithUIOptions(
        False,
        title="Use your own key",
        description="Leave off to use the shared NASA Earthdata app key. Turn on to enter your own below.",
    )
    app_key: Optional[SecretStr] = FieldWithUIOptions(
        None,
        title="NASA Earthdata app key",
        description=(
            "Only needed when you use your own key. Sign in at https://nrt3.modaps.eosdis.nasa.gov/, "
            "open the Profile menu and generate an app key."
        ),
        format="password",
        ui_options=UIOptions(widget="password"),
    )

    ui_global_options: GlobalUISchemaOptions = GlobalUISchemaOptions(order=["use_own_key", "app_key"])

    @validator("app_key", pre=True)
    def _blank_key_is_unset(cls, value):
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @root_validator(skip_on_failure=True)
    def _own_key_needs_a_key(cls, values):
        if values.get("use_own_key") and values.get("app_key") is None:
            raise ValueError("Enter your NASA Earthdata app key, or turn off 'Use your own key'.")
        return values


class PullFireDetectionsConfig(PullActionConfiguration):
    region: FirmsRegion = FieldWithUIOptions(
        ...,
        title="FIRMS region",
        description="The NASA FIRMS region that covers your area.",
        ui_options=UIOptions(widget="select"),
    )
    satellite: FirmsSatellite = FieldWithUIOptions(
        FirmsSatellite.NOAA_20,
        title="Satellite",
        description="The VIIRS satellite to read fire detections from.",
        ui_options=UIOptions(widget="select"),
    )
    spatial_feature_group: Optional[str] = FieldWithUIOptions(
        None,
        title="Spatial feature group",
        description=(
            "The EarthRanger spatial feature group whose boundaries fire detections are filtered by. "
            "Only detections inside these boundaries are reported."
        ),
    )
    # A string, not an object: the portal's polygon widget stores a GeoJSON Polygon as JSON text in a string field.
    fallback_area: Optional[str] = FieldWithUIOptions(
        None,
        title="Fallback area",
        description=(
            "Draw an area, or import a GeoJSON polygon. It is used when the spatial feature group "
            "can't be read from EarthRanger, or when none is selected."
        ),
        ui_options=UIOptions(widget="polygon"),
    )
    confidence_alert_levels: List[ConfidenceLevel] = FieldWithUIOptions(
        [ConfidenceLevel.NOMINAL, ConfidenceLevel.HIGH],
        title="Confidence levels to report",
        description="Only detections with one of these FIRMS confidence values are reported.",
        unique_items=True,
        min_items=1,
        ui_options=UIOptions(widget="select"),
    )
    alert_window_hours: int = FieldWithUIOptions(
        24,
        title="Alert window (hours)",
        description="Detections older than this are not reported.",
        ge=1,
        le=168,
    )

    ui_global_options: GlobalUISchemaOptions = GlobalUISchemaOptions(
        order=[
            "region",
            "satellite",
            "spatial_feature_group",
            "fallback_area",
            "confidence_alert_levels",
            "alert_window_hours",
            "run_on_schedule",
        ],
    )

    @validator("spatial_feature_group", pre=True)
    def _blank_group_is_unset(cls, value):
        if isinstance(value, str):
            return value.strip() or None
        return value

    @validator("fallback_area", pre=True)
    def _area_as_json_text(cls, value):
        if isinstance(value, dict):
            return json.dumps(value)
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @validator("fallback_area")
    def _area_is_a_polygon(cls, value):
        if value is not None:
            try:
                boundaries.geometry_from_polygon(value)
            except boundaries.BoundaryError as exc:
                raise ValueError(str(exc)) from exc
        return value

    @root_validator(skip_on_failure=True)
    def _needs_a_boundary(cls, values):
        if not values.get("spatial_feature_group") and not values.get("fallback_area"):
            raise ValueError("Select a spatial feature group or set a fallback area.")
        return values

    @classmethod
    def ui_schema(cls, *args, **kwargs):
        """Feed the group field from the EarthRanger destination's `list_spatial_feature_groups` reference action.

        Deliberately no ui:widget: portals without reference support, and integrations with no destination yet,
        keep a plain text input that takes a group id.
        """
        base = super().ui_schema(*args, **kwargs)
        base.setdefault("spatial_feature_group", {})["gundi:reference"] = {
            "action": "list_spatial_feature_groups",
            "target": "destination",
            "params": {},
            "allow_free_text": True,
        }
        return base
