"""CSV/Excel connector via pandas: list sheets/tables + fetch rows.

FileConnector confines file_path to DATA_DIR/qb_sources/, so these tests place
their files there (via an upload dir under a temp DATA_DIR)."""
import json
import pytest
from sqlalchemy.types import Float, String

import src.datasource_files as files
import core.constants as constants
from core.querybuilder_models import DataSource

pd = pytest.importorskip("pandas")


@pytest.fixture
def upload_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(files, "DATA_DIR", str(tmp_path), raising=False)
    d = tmp_path / "qb_sources" / "s1"
    d.mkdir(parents=True)
    return d


def test_csv_fetch(upload_dir):
    p = upload_dir / "data.csv"
    p.write_text("id,name\n1,Acme\n2,Globex\n", encoding="utf-8")
    ds = DataSource(id="s1", name="f", source_type="csv",
                    options=json.dumps({"file_path": str(p), "kind": "csv"}))
    conn = files.FileConnector(ds)
    assert conn.list_tables() == ["data"]
    st = conn.fetch_table("data")
    assert {c.name for c in st.columns} == {"id", "name"}
    assert {r["name"] for r in st.rows} == {"Acme", "Globex"}


def test_excel_sheets(upload_dir):
    pytest.importorskip("openpyxl")
    p = upload_dir / "book.xlsx"
    pd.DataFrame({"a": [1, 2]}).to_excel(p, sheet_name="Sheet1", index=False)
    ds = DataSource(id="s2", name="x", source_type="excel",
                    options=json.dumps({"file_path": str(p), "kind": "excel"}))
    conn = files.FileConnector(ds)
    assert "Sheet1" in conn.list_tables()
    st = conn.fetch_table("Sheet1")
    assert [c.name for c in st.columns] == ["a"]
    assert [r["a"] for r in st.rows] == [1, 2]


# --- QB-17: fetch_table streams (generator, no whole-file materialization)
# within a row+byte budget instead of loading the whole file into RAM ---

def test_csv_fetch_rows_is_a_generator(upload_dir):
    p = upload_dir / "data.csv"
    p.write_text("id,name\n1,Acme\n2,Globex\n", encoding="utf-8")
    ds = DataSource(id="s1", name="f", source_type="csv",
                    options=json.dumps({"file_path": str(p), "kind": "csv"}))
    conn = files.FileConnector(ds)
    st = conn.fetch_table("data")
    assert not hasattr(st.rows, "__len__")  # a lazy generator, not a materialized list
    rows = list(st.rows)
    assert {r["name"] for r in rows} == {"Acme", "Globex"}
    assert st.truncated is False


def test_csv_dtype_pinned_prevents_per_chunk_drift(upload_dir, monkeypatch):
    """Force a tiny chunksize so the CSV splits into two chunks whose 'val'
    column would otherwise independently infer DIFFERENT per-chunk dtypes
    (chunk 1: [1, 2] -> int64; chunk 2: [3.5, 4] -> float64, RESEARCH
    Pitfall 4). Pinning dtype= from the resolved (here: freshly-inferred,
    since no QB-10 override/prior applies on a first sync) column type must
    make every chunk parse 'val' consistently as float."""
    monkeypatch.setattr(files, "CSV_CHUNK_ROWS", 2)
    p = upload_dir / "drift.csv"
    p.write_text("id,val\n1,1\n2,2\n3,3.5\n4,4\n", encoding="utf-8")
    ds = DataSource(id="s1", name="f", source_type="csv",
                    options=json.dumps({"file_path": str(p), "kind": "csv"}))
    conn = files.FileConnector(ds)
    st = conn.fetch_table("data")
    rows = list(st.rows)
    assert len(rows) == 4
    assert all(isinstance(r["val"], float) for r in rows), (
        "every 'val' cell must be a float across BOTH chunks -- not int64 "
        "for chunk 1 and float64 for chunk 2"
    )
    assert [r["val"] for r in rows] == [1.0, 2.0, 3.5, 4.0]


def test_csv_dtype_drift_past_sample_widens_instead_of_crashing(upload_dir, monkeypatch, caplog):
    """CR-03 (HIGH): a column that looks numeric within the bounded
    CSV_SAMPLE_ROWS sample but drifts to a non-numeric value LATER in the
    file (a very plausible real-world shape -- e.g. an occasional "N/A")
    must NOT crash the whole table sync with a raw pandas ValueError (a
    regression vs the pre-phase whole-file pd.read_csv(), which tolerated
    this by inferring `object`). The sync must complete, widening the
    drifted column to text instead."""
    monkeypatch.setattr(files, "CSV_SAMPLE_ROWS", 3)
    monkeypatch.setattr(files, "CSV_CHUNK_ROWS", 2)
    p = upload_dir / "drift2.csv"
    # Sample (first 3 rows) sees only numeric 'val' -> pinned Int64. Row 5
    # (past the sample, and past the first successfully-parsed chunk) holds
    # a non-numeric value that violates the pin.
    p.write_text("id,val\n1,1\n2,2\n3,3\n4,4\n5,abc\n6,6\n", encoding="utf-8")
    ds = DataSource(id="s1", name="f", source_type="csv",
                    options=json.dumps({"file_path": str(p), "kind": "csv"}))
    conn = files.FileConnector(ds)
    st = conn.fetch_table("data")
    import logging as _logging
    with caplog.at_level(_logging.WARNING):
        rows = list(st.rows)  # must not raise
    assert len(rows) == 6
    # Rows 1-4 already parsed successfully (as Int64) in the two chunks that
    # ran BEFORE the drift was hit; rows 5-6 come from the retry with every
    # pinned column (including 'id') widened to text, per-chunk atomicity
    # (a chunk succeeds or fails as a whole, never partially).
    assert [r["id"] for r in rows] == [1, 2, 3, 4, "5", "6"]
    assert [r["val"] for r in rows] == [1, 2, 3, 4, "abc", "6"]
    assert any("dtype pin violated" in rec.message for rec in caplog.records)


def test_csv_dtype_pinned_from_resolved_override_type(upload_dir):
    """The dtype pinning must read st.columns[i].type_ LAZILY at generator-
    consumption time -- i.e. it must reflect a type mutated in place AFTER
    fetch_table() returns (simulating Plan-03's resolve_effective_types()
    running between fetch_table() and materialize_synctable()), not the
    freshly-inferred sample type captured when fetch_table() was called."""
    p = upload_dir / "override.csv"
    p.write_text("id,ratio\n1,1.0\n2,2.0\n", encoding="utf-8")
    ds = DataSource(id="s1", name="f", source_type="csv",
                    options=json.dumps({"file_path": str(p), "kind": "csv"}))
    conn = files.FileConnector(ds)
    st = conn.fetch_table("data")
    ratio_col = next(c for c in st.columns if c.name == "ratio")
    assert isinstance(ratio_col.type_, Float)  # fresh sample inference
    # Simulate a post-fetch_table resolution (e.g. a QB-10 override) demoting
    # 'ratio' to text -- must be honored by the STREAM, proving dtype_map is
    # built lazily at consumption time, not captured eagerly at fetch_table().
    ratio_col.type_ = String()
    rows = list(st.rows)
    assert [r["ratio"] for r in rows] == ["1.0", "2.0"]


def test_csv_stream_stops_at_byte_budget_and_sets_truncated(upload_dir, monkeypatch):
    monkeypatch.setattr(files, "MAX_SYNC_BYTES", 1)  # force the cap to trip immediately
    monkeypatch.setattr(files, "CSV_CHUNK_ROWS", 2)
    p = upload_dir / "big.csv"
    p.write_text("id,name\n" + "".join(f"{i},row{i}\n" for i in range(10)), encoding="utf-8")
    ds = DataSource(id="s1", name="f", source_type="csv",
                    options=json.dumps({"file_path": str(p), "kind": "csv"}))
    conn = files.FileConnector(ds)
    st = conn.fetch_table("data")
    rows = list(st.rows)
    assert st.truncated is True
    assert len(rows) < 10  # stopped early -- never materialized the whole file


def test_excel_fetch_rows_is_a_generator(upload_dir):
    pytest.importorskip("openpyxl")
    p = upload_dir / "book2.xlsx"
    pd.DataFrame({"a": [1, 2, 3]}).to_excel(p, sheet_name="Sheet1", index=False)
    ds = DataSource(id="s4", name="x", source_type="excel",
                    options=json.dumps({"file_path": str(p), "kind": "excel"}))
    conn = files.FileConnector(ds)
    st = conn.fetch_table("Sheet1")
    assert not hasattr(st.rows, "__len__")
    assert [r["a"] for r in st.rows] == [1, 2, 3]
    assert st.truncated is False


def test_excel_logs_warning_for_row_with_extra_trailing_columns(upload_dir, caplog):
    """WR-03 (LOW): a row with MORE raw values than the header-derived
    column count silently drops the extra value(s) -- must at least log a
    warning the first time this happens (previously no signal anywhere)."""
    import logging
    openpyxl = pytest.importorskip("openpyxl")
    p = upload_dir / "ragged.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(["a"])          # 1-column header
    ws.append([1])            # normal row
    ws.append([2, "stray"])   # a stray extra trailing value past the header
    wb.save(p)

    ds = DataSource(id="s6", name="x", source_type="excel",
                    options=json.dumps({"file_path": str(p), "kind": "excel"}))
    conn = files.FileConnector(ds)
    st = conn.fetch_table("Sheet1")
    with caplog.at_level(logging.WARNING):
        rows = list(st.rows)
    assert [r["a"] for r in rows] == [1, 2]
    assert "stray" not in str(rows)  # the extra value is dropped, per D-17
    assert any("beyond the" in rec.message for rec in caplog.records)


def test_excel_stream_stops_at_byte_budget_and_sets_truncated(upload_dir, monkeypatch):
    pytest.importorskip("openpyxl")
    monkeypatch.setattr(files, "MAX_SYNC_BYTES", 1)  # force the cap to trip immediately
    p = upload_dir / "big.xlsx"
    pd.DataFrame({"a": [f"row-value-{i}" for i in range(20)]}).to_excel(
        p, sheet_name="Sheet1", index=False)
    ds = DataSource(id="s5", name="x", source_type="excel",
                    options=json.dumps({"file_path": str(p), "kind": "excel"}))
    conn = files.FileConnector(ds)
    st = conn.fetch_table("Sheet1")
    rows = list(st.rows)
    assert st.truncated is True
    assert len(rows) < 20


def test_excel_missing_openpyxl_raises_missing_driver_error(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "openpyxl":
            raise ImportError("No module named openpyxl")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(files.MissingDriverError) as exc:
        files._openpyxl()
    assert "openpyxl" in str(exc.value)
