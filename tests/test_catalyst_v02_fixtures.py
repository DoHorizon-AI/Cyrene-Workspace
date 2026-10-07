"""Verify the fixed Catalyst v0.2 synthetic mixed-corpus fixtures.

The tests inspect real PDF, image, Office ZIP/XML, and source records, then
rebuild the corpus to prove the manifest signatures are deterministic.
测试检查真实 PDF、图像、Office ZIP/XML 和源记录，并重建语料验证签名可重复。
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from zipfile import ZipFile

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "catalyst-v02"
MANIFEST_PATH = FIXTURE_DIR / "manifest.json"
XML_NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "c": "http://schemas.openxmlformats.org/drawingml/2006/chart",
    "m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}


def _manifest() -> dict[str, object]:
    """Load the checked-in fixed signature and expectation manifest."""
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _zip_xml(path: Path, member: str) -> ET.Element:
    """Read and parse an XML part from a real OOXML package."""
    with ZipFile(path) as archive:
        assert archive.testzip() is None
        return ET.fromstring(archive.read(member))


def _pdf_page_content(payload: bytes) -> bytes:
    """Return the uncompressed content stream referenced by a PDF page."""
    objects = {
        int(match.group(1)): match.group(2)
        for match in re.finditer(rb"(\d+)\s+0\s+obj\s*(.*?)\s*endobj", payload, re.DOTALL)
    }
    page_body = next(
        body for body in objects.values() if b"/Type /Page" in body and b"/Type /Pages" not in body
    )
    contents_ref = re.search(rb"/Contents\s+(\d+)\s+0\s+R", page_body)
    assert contents_ref is not None
    content_body = objects[int(contents_ref.group(1))]
    stream = re.search(rb"stream\r?\n(.*?)\r?\nendstream", content_body, re.DOTALL)
    assert stream is not None
    return stream.group(1)


def _jpeg_dimensions(payload: bytes) -> tuple[int, int]:
    """Read dimensions from a baseline JPEG start-of-frame marker."""
    assert payload.startswith(b"\xff\xd8") and payload.endswith(b"\xff\xd9")
    position = 2
    frame_markers = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB}
    while position < len(payload):
        if payload[position] != 0xFF:
            position += 1
            continue
        while payload[position] == 0xFF:
            position += 1
        marker = payload[position]
        position += 1
        if marker in {0xD8, 0xD9, 0x01} or 0xD0 <= marker <= 0xD7:
            continue
        segment_length = int.from_bytes(payload[position : position + 2], "big")
        if marker in frame_markers:
            height = int.from_bytes(payload[position + 3 : position + 5], "big")
            width = int.from_bytes(payload[position + 5 : position + 7], "big")
            return width, height
        position += segment_length
    raise AssertionError("JPEG has no supported start-of-frame marker")


def test_manifest_signatures_cover_every_corpus_input() -> None:
    """Every fixed asset must have the declared bytes, size, structure, and warnings."""
    manifest = _manifest()
    files = manifest["files"]
    assert manifest["synthetic"] is True
    assert manifest["personalOrCustomerData"] is False
    assert set(files) == {
        "handoff-native.pdf",
        "handoff-scanned.pdf",
        "handoff.docx",
        "handoff.pptx",
        "handoff.xlsx",
        "handoff-ocr-clear.png",
        "handoff-ocr-clear.jpg",
        "handoff-ocr-low-quality.jpg",
        "handoff.md",
        "handoff.txt",
        "handoff.csv",
        "source-conversations.jsonl",
        "malformed.pdf",
        "unsupported.rtf",
    }
    for name, record in files.items():
        payload = (FIXTURE_DIR / name).read_bytes()
        assert hashlib.sha256(payload).hexdigest() == record["sha256"]
        assert len(payload) == record["sizeBytes"]
        assert "expectedStructure" in record
        assert "warningExpectation" in record


def test_native_and_scanned_pdfs_are_distinct_real_inputs() -> None:
    """The native PDF has selectable text; the scanned PDF contains only an image."""
    native = (FIXTURE_DIR / "handoff-native.pdf").read_bytes()
    scanned = (FIXTURE_DIR / "handoff-scanned.pdf").read_bytes()
    assert native.startswith(b"%PDF-") and scanned.startswith(b"%PDF-")
    assert b"/Count 1" in native and b"/Count 1" in scanned
    assert b"ORCHID-42" in native
    assert b"Tj" in _pdf_page_content(native)
    scanned_page = _pdf_page_content(scanned)
    assert b"Do" in scanned_page
    assert b"Tj" not in scanned_page
    assert b"/Subtype /Image" in scanned
    assert b"/Subtype /Image" not in native

    malformed = (FIXTURE_DIR / "malformed.pdf").read_bytes()
    assert malformed.startswith(b"%PDF-")
    assert b"\nxref\n" not in malformed and b"%%EOF" not in malformed


def test_docx_pptx_and_xlsx_contain_real_office_xml_structure() -> None:
    """Inspect semantic XML parts, not just the ZIP container signatures."""
    docx_path = FIXTURE_DIR / "handoff.docx"
    docx = _zip_xml(docx_path, "word/document.xml")
    docx_text = "".join(node.text or "" for node in docx.findall(".//w:t", XML_NS))
    assert "ORCHID-42" in docx_text and "SP-204" in docx_text
    assert len(docx.findall(".//w:tbl", XML_NS)) == 1
    assert len(docx.findall(".//w:p", XML_NS)) >= 5

    pptx_path = FIXTURE_DIR / "handoff.pptx"
    with ZipFile(pptx_path) as archive:
        assert archive.testzip() is None
        slide_names = sorted(
            name for name in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)
        )
        notes_names = [
            name
            for name in archive.namelist()
            if re.fullmatch(r"ppt/notesSlides/notesSlide\d+\.xml", name)
        ]
        chart_names = [name for name in archive.namelist() if name.startswith("ppt/charts/chart")]
        assert len(slide_names) == 2
        assert len(notes_names) == 1
        assert len(chart_names) == 1

        slide_one = ET.fromstring(archive.read("ppt/slides/slide1.xml"))
        text_shape = next(
            shape
            for shape in slide_one.findall(".//p:sp", XML_NS)
            if "ORCHID-42 | IT EQUIPMENT HANDOFF"
            in "".join(node.text or "" for node in shape.findall(".//a:t", XML_NS))
        )
        offset = text_shape.find("./p:spPr/a:xfrm/a:off", XML_NS)
        assert offset is not None
        pptx_expectation = _manifest()["files"]["handoff.pptx"]["expectedStructure"]
        expected_position = pptx_expectation["positionedText"]
        assert int(offset.attrib["x"]) == expected_position["leftEmu"]
        assert int(offset.attrib["y"]) == expected_position["topEmu"]

        slide_two_rels = ET.fromstring(archive.read("ppt/slides/_rels/slide2.xml.rels"))
        assert any(
            rel.attrib.get("Type", "").endswith("/chart")
            and rel.attrib.get("Target") == "../charts/chart1.xml"
            for rel in slide_two_rels.findall("rel:Relationship", XML_NS)
        )
        notes = ET.fromstring(archive.read(notes_names[0]))
        notes_text = "".join(node.text or "" for node in notes.findall(".//a:t", XML_NS))
        assert "Synthetic presenter note" in notes_text

    xlsx_path = FIXTURE_DIR / "handoff.xlsx"
    workbook = _zip_xml(xlsx_path, "xl/workbook.xml")
    sheet_names = [node.attrib["name"] for node in workbook.findall(".//m:sheet", XML_NS)]
    assert sheet_names == ["Handoff Register", "Recovery Lookup"]
    sheet_one = _zip_xml(xlsx_path, "xl/worksheets/sheet1.xml")
    formula_with_cache = sheet_one.find('.//m:c[@r="F2"]', XML_NS)
    formula_without_cache = sheet_one.find('.//m:c[@r="F3"]', XML_NS)
    assert formula_with_cache is not None and formula_without_cache is not None
    assert formula_with_cache.find("m:f", XML_NS).text == "SUM(C2:C3)"
    assert formula_with_cache.find("m:v", XML_NS).text == "42"
    assert formula_without_cache.find("m:f", XML_NS).text == "SUM(C3:C4)"
    cache = formula_without_cache.find("m:v", XML_NS)
    assert cache is None or not cache.text
    table = _zip_xml(xlsx_path, "xl/tables/table1.xml")
    assert table.attrib["name"] == "HandoffTable"
    table_rels = _zip_xml(xlsx_path, "xl/worksheets/_rels/sheet1.xml.rels")
    assert any(
        rel.attrib.get("Type", "").endswith("/table")
        for rel in table_rels.findall("rel:Relationship", XML_NS)
    )


def test_raster_markdown_text_csv_and_jsonl_corpus_structure() -> None:
    """Check image headers and the actual source-family/conversation records."""
    png = (FIXTURE_DIR / "handoff-ocr-clear.png").read_bytes()
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert int.from_bytes(png[16:20], "big") == 1600
    assert int.from_bytes(png[20:24], "big") == 500
    assert _jpeg_dimensions((FIXTURE_DIR / "handoff-ocr-clear.jpg").read_bytes()) == (1600, 500)
    low_quality = (FIXTURE_DIR / "handoff-ocr-low-quality.jpg").read_bytes()
    assert _jpeg_dimensions(low_quality) == (320, 100)
    assert len(low_quality) < len((FIXTURE_DIR / "handoff-ocr-clear.jpg").read_bytes())

    markdown = (FIXTURE_DIR / "handoff.md").read_text(encoding="utf-8")
    plain_text = (FIXTURE_DIR / "handoff.txt").read_text(encoding="utf-8")
    assert markdown.startswith("# IT 设备交接流程 / ORCHID-42")
    assert markdown.count("|") >= 12
    assert "SP-204" in plain_text and "Bay: 07" in plain_text
    with (FIXTURE_DIR / "handoff.csv").open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 3 and rows[0]["recovery_code"] == "ORCHID-42"

    records = [
        json.loads(line)
        for line in (FIXTURE_DIR / "source-conversations.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line
    ]
    assert len(records) == 24
    family_counts = Counter(record["sourceFamily"] for record in records)
    conversations = {record["conversationId"] for record in records}
    assert len(family_counts) == 12 and set(family_counts.values()) == {2}
    assert len(conversations) == 24
    sentinel = _manifest()["managementSentinel"]
    assert all(record["_reviewNote"] == sentinel for record in records)
    assert all("_acl" in record and "_operatorAnnotation" in record for record in records)
    assert all(
        sentinel not in " ".join(message["content"] for message in record["messages"])
        for record in records
    )

    unsupported = (FIXTURE_DIR / "unsupported.rtf").read_text(encoding="ascii")
    assert unsupported.startswith(r"{\rtf1")


def test_builder_recreates_identical_hashes(tmp_path: Path) -> None:
    """Regenerate in isolation and compare the complete fixed signature map."""
    regenerated = tmp_path / "catalyst-v02"
    subprocess.run(
        [
            sys.executable,
            str(FIXTURE_DIR / "build_fixtures.py"),
            "--output",
            str(regenerated),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    original = _manifest()
    rebuilt = json.loads((regenerated / "manifest.json").read_text(encoding="utf-8"))
    assert rebuilt == original
    for name, record in original["files"].items():
        assert hashlib.sha256((regenerated / name).read_bytes()).hexdigest() == record["sha256"]
