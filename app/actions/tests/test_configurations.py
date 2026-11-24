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
    assert cfg.minimum_confidence.value == "nominal"
    assert cfg.reported_confidence_levels == {"nominal", "high"}
    assert cfg.alert_window_hours == 24 and cfg.run_on_schedule is True
    assert cfg.fallback_area is None
    assert (cfg.priority_low.value, cfg.priority_nominal.value, cfg.priority_high.value) == ("gray", "green", "amber")
    assert cfg.red_frp_threshold_mw == 50 and isinstance(cfg.red_frp_threshold_mw.value, int)


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
        {"spatial_feature_group": "g1", "minimum_confidence": "certain"},
        {"spatial_feature_group": "g1", "minimum_confidence": ""},
        {"spatial_feature_group": "g1", "priority_high": "purple"},
        {"spatial_feature_group": "g1", "red_frp_threshold_mw": "strong"},
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
        "minimum_confidence",
        "alert_window_hours",
        "priority_low",
        "priority_nominal",
        "priority_high",
        "red_frp_threshold_mw",
        "run_on_schedule",
    ]
    for name in ("priority_low", "priority_nominal", "priority_high"):
        assert ui[name]["ui:widget"] == "select"


def test_frp_threshold_is_a_slider_over_fixed_values():
    schema = PullFireDetectionsConfig.schema()
    assert schema["definitions"]["RedFrpThreshold"]["enum"] == [50, 75, 100, 200]
    assert schema["definitions"]["RedFrpThreshold"]["type"] == "integer"
    ui = PullFireDetectionsConfig.ui_schema()["red_frp_threshold_mw"]
    assert ui["ui:widget"] == "slider"
    assert ui["ui:enumNames"] == ["50 MW", "75 MW", "100 MW", "200 MW"]


@pytest.mark.parametrize("value", [50, 75, 100, 200, 75.0, "100"])
def test_frp_threshold_accepts_the_slider_values(value):
    cfg = PullFireDetectionsConfig(region="Europe", spatial_feature_group="g1", red_frp_threshold_mw=value)
    assert cfg.red_frp_threshold_mw == int(float(value))


@pytest.mark.parametrize(
    "stored,read_as",
    [(None, 200), ("", 200), (60, 50), (62.5, 50), (63, 75), (90, 100), (150, 100), (151, 200), (5000, 200), (1, 50)],
)
def test_a_threshold_saved_before_the_slider_reads_as_the_nearest_value(stored, read_as):
    """An empty threshold used to turn the rule off; the highest value is the closest there is to that now."""
    cfg = PullFireDetectionsConfig(region="Europe", spatial_feature_group="g1", red_frp_threshold_mw=stored)
    assert cfg.red_frp_threshold_mw == read_as


def test_priority_choices_are_the_four_colours():
    schema = PullFireDetectionsConfig.schema()
    assert schema["definitions"]["EventPriority"]["enum"] == ["gray", "green", "amber", "red"]


@pytest.mark.parametrize(
    "minimum,reported",
    [("low", {"low", "nominal", "high"}), ("nominal", {"nominal", "high"}), ("high", {"high"})],
)
def test_minimum_confidence_reports_that_level_and_above(minimum, reported):
    cfg = PullFireDetectionsConfig(region="Europe", spatial_feature_group="g1", minimum_confidence=minimum)
    assert cfg.reported_confidence_levels == reported


def test_minimum_confidence_is_a_labelled_slider():
    ui = PullFireDetectionsConfig.ui_schema()["minimum_confidence"]
    assert ui["ui:widget"] == "slider" and ui["ui:enumNames"] == ["Low", "Nominal", "High"]
    schema = PullFireDetectionsConfig.schema()
    assert schema["definitions"]["ConfidenceLevel"]["enum"] == ["low", "nominal", "high"]
    assert "confidence_alert_levels" not in schema["properties"]


@pytest.mark.parametrize(
    "stored,minimum",
    [(["nominal", "high"], "nominal"), (["high", "low"], "low"), (["high"], "high"), (["low", "high"], "low")],
)
def test_a_configuration_saved_with_the_old_level_list_reads_as_its_lowest_level(stored, minimum):
    cfg = PullFireDetectionsConfig(region="Europe", spatial_feature_group="g1", confidence_alert_levels=stored)
    assert cfg.minimum_confidence.value == minimum
    assert "confidence_alert_levels" not in cfg.dict()


def test_new_field_wins_over_the_old_level_list():
    cfg = PullFireDetectionsConfig(
        region="Europe", spatial_feature_group="g1", confidence_alert_levels=["low"], minimum_confidence="high"
    )
    assert cfg.minimum_confidence.value == "high"


@pytest.mark.parametrize("stored", [[], None, ["certain"], "nominal"])
def test_an_unusable_old_level_list_falls_back_to_the_default(stored):
    cfg = PullFireDetectionsConfig(region="Europe", spatial_feature_group="g1", confidence_alert_levels=stored)
    assert cfg.minimum_confidence.value == "nominal"
