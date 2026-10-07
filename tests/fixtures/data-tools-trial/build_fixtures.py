"""
┌─────────────────────────────────────────────────────────────────────┐
│  📄 build_fixtures.py                                                │
│  Module: tests.fixtures.data_tools_trial.build_fixtures              │
│  Role: Build deterministic, synthetic data-tools acceptance inputs.  │
│                                                                      │
│  模块职责：生成固定、合成的 data-tools 验收输入。                       │
└─────────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parent
PDF_LINES = (
    "Cyrene Data Tools Trial",
    "来源键：trial-handbook-shared-bytes",
    "恢复代码：ORCHID-42",
    "此段用于验收知识检索和训练用途权限分离。",
    "表格行：步骤 | 样本 | 用途",
    "1 | alpha | 知识检索",
)
PRIVATE_MARKER = "TRIAL_INTERNAL_REVIEW_NOTE_DO_NOT_TRAIN_7f4a"


def _pdf_object_stream(objects: list[bytes]) -> bytes:
    """Serialize numbered PDF objects and a deterministic cross-reference table.

    将编号 PDF 对象和确定性的交叉引用表序列化。
    """
    chunks = [b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n"]
    offsets = [0]
    for number, payload in enumerate(objects, start=1):
        offsets.append(sum(map(len, chunks)))
        chunks.append(f"{number} 0 obj\n".encode("ascii"))
        chunks.append(payload)
        chunks.append(b"\nendobj\n")
    xref_offset = sum(map(len, chunks))
    chunks.append(f"xref\n0 {len(offsets)}\n".encode("ascii"))
    chunks.append(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        chunks.append(f"{offset:010d} 00000 n \n".encode("ascii"))
    chunks.append(
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n".encode("ascii")
    )
    return b"".join(chunks)


def _make_pdf(lines: tuple[str, ...]) -> bytes:
    """Create a text-extractable single-page PDF using a ToUnicode CMap.

    使用 ToUnicode CMap 创建可抽取文本的单页 PDF。
    """
    characters = sorted({char for line in lines for char in line if ord(char) <= 0xFFFF})
    cmap_rows = "\n".join(
        f"<{ord(char):04X}> <{ord(char):04X}>" for char in characters
    )
    cmap = (
        "/CIDInit /ProcSet findresource begin\n"
        "12 dict begin\n"
        "begincmap\n"
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
        "/CMapName /TrialUnicode def\n/CMapType 2 def\n"
        "1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n"
        f"{len(characters)} beginbfchar\n{cmap_rows}\nendbfchar\n"
        "endcmap\nCMapName currentdict /CMap defineresource pop\nend\nend\n"
    ).encode("ascii")
    commands = ["BT", "/F1 12 Tf", "72 720 Td"]
    for index, line in enumerate(lines):
        if index:
            commands.append("0 -24 Td")
        commands.append(f"<{line.encode('utf-16-be').hex().upper()}> Tj")
    commands.append("ET")
    content = "\n".join(commands).encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        ),
        b"<< /Length " + str(len(content)).encode("ascii") + b" >>\nstream\n"
        + content
        + b"\nendstream",
        (
            b"<< /Type /Font /Subtype /Type0 /BaseFont /TrialUnicode "
            b"/Encoding /Identity-H /DescendantFonts [6 0 R] /ToUnicode 8 0 R >>"
        ),
        (
            b"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /TrialUnicode "
            b"/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> "
            b"/FontDescriptor 7 0 R /DW 1000 /CIDToGIDMap /Identity >>"
        ),
        (
            b"<< /Type /FontDescriptor /FontName /TrialUnicode /Flags 4 "
            b"/FontBBox [0 -200 1000 900] /ItalicAngle 0 /Ascent 800 "
            b"/Descent -200 /CapHeight 700 /StemV 80 >>"
        ),
        b"<< /Length " + str(len(cmap)).encode("ascii") + b" >>\nstream\n"
        + cmap
        + b"endstream",
    ]
    return _pdf_object_stream(objects)


def _paragraph(text: str, style: str | None = None) -> str:
    """Return one WordprocessingML paragraph with optional style.

    返回一段可选带样式的 WordprocessingML 段落。
    """
    from xml.sax.saxutils import escape

    style_xml = f'<w:pPr><w:pStyle w:val="{escape(style)}"/></w:pPr>' if style else ""
    return f"<w:p>{style_xml}<w:r><w:t xml:space=\"preserve\">{escape(text)}</w:t></w:r></w:p>"


def _table(rows: tuple[tuple[str, ...], ...]) -> str:
    """Return a simple table whose cells preserve Unicode text.

    返回保留 Unicode 文本的简单表格。
    """
    from xml.sax.saxutils import escape

    row_xml = []
    for row in rows:
        cells = "".join(
            "<w:tc><w:tcPr><w:tcW w:w=\"2400\" w:type=\"dxa\"/></w:tcPr>"
            f"<w:p><w:r><w:t xml:space=\"preserve\">{escape(value)}</w:t></w:r></w:p></w:tc>"
            for value in row
        )
        row_xml.append(f"<w:tr>{cells}</w:tr>")
    return "<w:tbl><w:tblPr/><w:tblGrid/>" + "".join(row_xml) + "</w:tbl>"


def _write_docx(path: Path) -> None:
    """Write a small valid DOCX with paragraphs, emoji and a real table.

    写入包含段落、emoji 和真实表格的有效小型 DOCX。
    """
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>"
        + _paragraph("Cyrene Data Tools 文档验收", "Title")
        + _paragraph("中文段落：本样例用于验证来源定位、人工复核与双出口。🌱🧭")
        + _paragraph("表格中的行列关系应保留；权限按 source 和 purpose 分别检查。")
        + _table(
            (
                ("主题", "知识输出", "训练输出"),
                ("恢复代码", "ORCHID-42", "样本答案不可包含审核备注"),
                ("访问用途", "knowledge_retrieval", "model_training"),
            )
        )
        + _paragraph(f"内部审核标记：{PRIVATE_MARKER}")
        + "<w:sectPr><w:pgSz w:w=\"12240\" w:h=\"15840\"/></w:sectPr>"
        + "</w:body></w:document>"
    ).encode("utf-8")
    content_types = (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        b'<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        b'<Default Extension="xml" ContentType="application/xml"/>'
        b'<Override PartName="/word/document.xml" '
        b'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        b"</Types>"
    )
    relationships = (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        b'<Relationship Id="rId1" '
        b'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        b'Target="word/document.xml"/>'
        b"</Relationships>"
    )
    entries = {
        "[Content_Types].xml": content_types,
        "_rels/.rels": relationships,
        "word/document.xml": document,
    }
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        for name, payload in entries.items():
            info = ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            archive.writestr(info, payload)


def _training_records() -> list[dict[str, object]]:
    """Return deterministic grouped instruction records and a private marker.

    返回按 source family 分组的固定指令记录，并附带不可进入 SFT 的内部标记。
    """
    records: list[dict[str, object]] = []
    family_indexes = (*range(1, 10), 32)
    for family_index in family_indexes:
        family = f"family-{family_index:02d}"
        for example_index in range(1, 3):
            sample_id = f"{family}-sample-{example_index}"
            records.append(
                {
                    "sampleId": sample_id,
                    "sourceFamily": family,
                    "conversationId": family,
                    "instruction": "回答 Cyrene 试用样例中的已知问题。",
                    "input": f"如何恢复试用数据？样本 {family_index}-{example_index} 🌱",
                    "output": f"使用恢复代码 ORCHID-42；来源组 {family}。",
                    "_reviewNote": PRIVATE_MARKER,
                    "_acl": ["org:trial-alpha"],
                    "_operatorAnnotation": "仅用于管理，禁止进入模型答案。",
                }
            )
    return records


def build() -> None:
    """Build all checked-in trial inputs and their checksum manifest.

    生成全部固定试用输入及其摘要清单。
    """
    ROOT.mkdir(parents=True, exist_ok=True)
    pdf_path = ROOT / "trial-handbook.pdf"
    pdf_path.write_bytes(_make_pdf(PDF_LINES))
    _write_docx(ROOT / "trial-handbook.docx")
    (ROOT / "malformed.pdf").write_bytes(b"%PDF-1.7\n1 0 obj << /Type /Page >>\nnot-an-xref\n")
    (ROOT / "malformed.jsonl").write_text(
        '{"sampleId":"valid-before-error","reference":"是","actual":"是"}\n'
        '{"sampleId":"broken","reference":"缺少结束引号}\n',
        encoding="utf-8",
    )
    training = _training_records()
    (ROOT / "trial-training.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in training),
        encoding="utf-8",
    )
    echo_rows = [
        {"sampleId": "echo-01", "reference": "恢复代码是 ORCHID-42。🌱", "actual": "恢复代码是 ORCHID-42。🌱"},
        {"sampleId": "echo-02", "reference": "试用版本为一。", "actual": "试用版本为二。"},
        {"sampleId": "echo-03", "reference": None, "actual": "没有参考答案时应跳过。"},
        {"sampleId": "echo-04", "reference": "有参考答案但预测为空时应跳过。", "actual": None},
    ]
    (ROOT / "echo-reference-actual.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in echo_rows),
        encoding="utf-8",
    )
    duplicate_echo_rows = [
        {"sampleId": "duplicate-sample", "reference": "one", "actual": "one"},
        {"sampleId": "duplicate-sample", "reference": "two", "actual": "two"},
    ]
    (ROOT / "echo-duplicate-sample-id.jsonl").write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
            for row in duplicate_echo_rows
        ),
        encoding="utf-8",
    )
    digest = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
    fixed_files = (
        "trial-handbook.pdf",
        "trial-handbook.docx",
        "trial-training.jsonl",
        "echo-reference-actual.jsonl",
        "echo-duplicate-sample-id.jsonl",
        "malformed.pdf",
        "malformed.jsonl",
    )
    manifest = {
        "schemaVersion": 1,
        "purpose": "Synthetic fixed acceptance corpus; no personal or customer data.",
        "files": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in fixed_files
        },
        "pdfSentinels": list(PDF_LINES),
        "docxSentinels": ["中文段落", "🌱🧭", "知识输出", "训练输出", PRIVATE_MARKER],
        "duplicateSourceScenario": {
            "path": "trial-handbook.pdf",
            "sha256": digest,
            "uploads": [
                {
                    "alias": "knowledge-only-copy",
                    "policy": {
                        "allowKnowledge": True,
                        "allowTraining": False,
                        "allowedPrincipalRefs": ["org:trial-alpha"],
                        "allowedUsePurposes": ["knowledge_retrieval"],
                    },
                },
                {
                    "alias": "training-only-copy",
                    "policy": {
                        "allowKnowledge": False,
                        "allowTraining": True,
                        "allowedPrincipalRefs": ["org:trial-beta"],
                        "allowedUsePurposes": ["model_training"],
                    },
                },
            ],
        },
        "training": {
            "path": "trial-training.jsonl",
            "records": len(training),
            "sourceFamilies": 10,
            "recordsPerFamily": 2,
            "privateMarker": PRIVATE_MARKER,
            "expectedSplitByFamily": {
                **{
                    f"family-{index:02d}": "train"
                    for index in (1, 2, 3, 4, 6, 7, 8, 9)
                },
                "family-05": "validation",
                "family-32": "test",
            },
        },
        "echo": {
            "path": "echo-reference-actual.jsonl",
            "total": 4,
            "expectedEvaluated": 2,
            "expectedSkipped": 2,
            "expectedMatched": 1,
        },
        "invalidInputs": ["malformed.pdf", "malformed.jsonl", "echo-duplicate-sample-id.jsonl"],
    }
    (ROOT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    build()
