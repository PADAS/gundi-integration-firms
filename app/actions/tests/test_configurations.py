import json

import pydantic
import pytest

from app.actions.configurations import AuthenticateConfig, PullFireDetectionsConfig

SQUARE = {"type": "Polygon", "coordinates": [[[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]]}


def test_auth_defaults_to_the_shared_key():
    config = AuthenticateConfig()
    assert config.use_own_key is False and config.app_key is None


def test_auth_key_is_secret():
    config = AuthenticateConfig(use_own_key=True, app_key="abc")
    assert "abc" not in repr(config) and config.app_key.get_secret_value() == "abc"


def test_own_key_is_required_when_using_your_own():
    for kwargs in ({"use_own_key": True}, {"use_own_key": True, "app_key": "  "}):
        with pytest.raises(pydantic.ValidationError):
            AuthenticateConfig(**kwargs)


def test_blank_key_from_the_portal_counts_as_unset():
    assert AuthenticateConfig(app_key="").app_key is None


def test_auth_form_layout():
    ui = AuthenticateConfig.ui_schema()
    assert ui["app_key"]["ui:widget"] == "password"
    assert ui["ui:order"] == ["use_own_key", "app_key"]
    schema = AuthenticateConfig.schema()
    assert schema["properties"]["app_key"]["format"] == "password"
    assert schema["properties"]["use_own_key"]["title"] == "Use your own key"
    assert "required" not in schema


def test_pull_defaults():
    cfg = PullFireDetectionsConfig(region="Southern_Africa", spatial_feature_group="g1")
    assert cfg.satellite.value == "noaa-20"
    assert [level.value for level in cfg.confidence_alert_levels] == ["nominal", "high"]
    assert cfg.alert_window_hours == 24 and cfg.run_on_schedule is True
    assert cfg.fallback_area is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"spatial_feature_group": "  "},
        {"fallback_area": "not json"},
        {"fallback_area": json.dumps({"type": "Point", "coordinates": [0, 0]})},
        {"spatial_feature_group": "g1", "fallback_area": "not json"},
        {"spatial_feature_group": "g1", "satellite": "suomi-npp"},
        {"spatial_feature_group": "g1", "alert_window_hours": 0},
        {"spatial_feature_group": "g1", "confidence_alert_levels": ["certain"]},
        {"spatial_feature_group": "g1", "confidence_alert_levels": []},
    ],
)
def test_pull_rejects(kwargs):
    with pytest.raises(pydantic.ValidationError):
        PullFireDetectionsConfig(region="Southern_Africa", **kwargs)


def test_region_must_be_known():
    with pytest.raises(pydantic.ValidationError):
        PullFireDetectionsConfig(region="Atlantis", spatial_feature_group="g1")


def test_fallback_area_accepts_dict_and_string():
    a = PullFireDetectionsConfig(region="Europe", fallback_area=SQUARE)
    b_ = PullFireDetectionsConfig(region="Europe", fallback_area=json.dumps(SQUARE))
    assert json.loads(a.fallback_area) == json.loads(b_.fallback_area) == SQUARE


def test_blank_values_from_the_portal_count_as_unset():
    cfg = PullFireDetectionsConfig(region="Europe", spatial_feature_group=" g1 ", fallback_area="")
    assert cfg.spatial_feature_group == "g1" and cfg.fallback_area is None


def test_region_dropdown_lists_all_regions():
    schema = PullFireDetectionsConfig.schema()
    assert len(schema["definitions"]["FirmsRegion"]["enum"]) == 13
    assert "ui_global_options" not in schema["properties"]


def test_ui_schema_reference_and_widget():
    ui = PullFireDetectionsConfig.ui_schema()
    assert ui["spatial_feature_group"]["gundi:reference"] == {
        "action": "list_spatial_feature_groups",
        "target": "destination",
        "params": {},
        "allow_free_text": True,
    }
    assert "ui:widget" not in ui["spatial_feature_group"]
    assert ui["fallback_area"]["ui:widget"] == "polygon"
    assert ui["ui:order"] == [
        "region",
        "satellite",
        "spatial_feature_group",
        "fallback_area",
        "confidence_alert_levels",
        "alert_window_hours",
        "run_on_schedule",
    ]
