from datetime import datetime, timezone

import pytest

from app.actions import client as c

NOAA20 = c.FIRMS_PRODUCTS["noaa-20"]
HEADER = (
    "latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,confidence,version,bright_ti5,frp,daynight"
)
ROW = "-1.25031,35.12345,338.1,0.43,0.46,2026-11-23,00:42,N20,nominal,2.0NRT,281.5,1.9,N"


def test_products():
    assert (NOAA20.folder, NOAA20.filename_prefix, NOAA20.data_type_and_name) == (
        "noaa-20-viirs-c2",
        "J1_VIIRS_C2",
        "VJ114IMGTDL_NRT",
    )
    n21 = c.FIRMS_PRODUCTS["noaa-21"]
    assert (n21.folder, n21.filename_prefix, n21.data_type_and_name) == (
        "noaa-21-viirs-c2",
        "J2_VIIRS_C2",
        "VJ214IMGTDL_NRT",
    )
    assert set(c.FIRMS_PRODUCTS) == {"noaa-20", "noaa-21"}
    assert c.DEFAULT_FIRMS_PRODUCT == "noaa-20"
    assert len(c.FIRMS_REGIONS) == 13 and "Southern_Africa" in c.FIRMS_REGIONS


def test_date_index_and_window():
    now = datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc)
    assert c.date_index(now) == 2026001
    assert c.window_date_indexes(now) == [2025365, 2026001]


def test_file_url():
    assert c.file_url("Southern_Africa", NOAA20, 2026327) == (
        "https://nrt3.modaps.eosdis.nasa.gov/api/v2/content/archives/FIRMS/noaa-20-viirs-c2/"
        "Southern_Africa/J1_VIIRS_C2_Southern_Africa_VJ114IMGTDL_NRT_2026327.txt"
    )


def test_parse_line():
    rec = c.parse_line(ROW, c.parse_header(HEADER))
    assert rec["latitude"] == -1.25031 and rec["frp"] == 1.9 and rec["confidence"] == "nominal"
    assert rec["recorded_at"] == datetime(2026, 11, 23, 0, 42, tzinfo=timezone.utc)


def test_parse_line_accepts_hhmm():
    rec = c.parse_line(ROW.replace("00:42", "0042"), c.parse_header(HEADER))
    assert rec["recorded_at"].minute == 42


@pytest.mark.parametrize(
    "bad",
    [
        ROW + ",extra",  # wrong field count
        ROW.replace("-1.25031", "91.0"),  # latitude out of range
        ROW.replace("00:42", "0:42"),  # bad acq_time shape
        ROW.replace("00:42", "24:00"),  # hour out of range
        ROW.replace("338.1", "n/a"),  # non-numeric float column
    ],
)
def test_parse_line_rejects(bad):
    with pytest.raises(ValueError):
        c.parse_line(bad, c.parse_header(HEADER))


def test_parse_header_requires_columns():
    assert c.parse_header(" Latitude , LONGITUDE,acq_date,acq_time\r") == (
        "latitude",
        "longitude",
        "acq_date",
        "acq_time",
    )
    with pytest.raises(c.FirmsHeaderError):
        c.parse_header("latitude,longitude,acq_date")


@pytest.mark.parametrize(
    "version,level",
    [("2.0NRT", "NRT"), (" 2.0URT ", "URT"), ("1.0rt", "RT"), ("2.0", None), (None, None), ("", None)],
)
def test_processing_level(version, level):
    assert c.processing_level(version) == level
