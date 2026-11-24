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
    assert (cfg.priority_low.value, cfg.priority_nominal.value, cfg.priority_high.value) == ("gray", "green", "amber")
    assert cfg.red_frp_threshold_mw == 50


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
        {"spatial_feature_group": "g1", "priority_high": "purple"},
        {"spatial_feature_group": "g1", "red_frp_threshold_mw": 0},
        {"spatial_feature_group": "g1", "red_frp_threshold_mw": -5},
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
        "priority_low",
        "priority_nominal",
        "priority_high",
        "red_frp_threshold_mw",
        "run_on_schedule",
    ]
    for name in ("priority_low", "priority_nominal", "priority_high"):
        assert ui[name]["ui:widget"] == "select"


def test_frp_threshold_can_be_turned_off():
    for blank in (None, ""):
        cfg = PullFireDetectionsConfig(region="Europe", spatial_feature_group="g1", red_frp_threshold_mw=blank)
        assert cfg.red_frp_threshold_mw is None


def test_priority_choices_are_the_four_colours():
    schema = PullFireDetectionsConfig.schema()
    assert schema["definitions"]["EventPriority"]["enum"] == ["gray", "green", "amber", "red"]
