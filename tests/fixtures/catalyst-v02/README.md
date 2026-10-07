# Catalyst v0.2 mixed corpus / Catalyst v0.2 混合语料

This directory contains fixed synthetic business materials for the Catalyst
v0.2 parser trial. Every supported-looking input is a real file in its native
format; the corpus does not replace parsing with a mock response. It describes
an IT equipment handoff workflow using the synthetic recovery code
`ORCHID-42`. It contains no personal or customer data.

本目录为 Catalyst v0.2 解析器试用提供固定的合成业务资料。看似可支持的输入均为其原生
格式的真实文件；语料不会用 mock 解析结果替代文件解析。主题是 IT 设备交接流程，使用合成
恢复代码 `ORCHID-42`，不包含个人或客户数据。

| File | Contents | Warning expectation |
|---|---|---|
| `handoff-native.pdf` | Selectable native text and a four-row workflow table | No OCR warning expected |
| `handoff-scanned.pdf` | Image-only scan made from the clear raster sample | OCR is required because there is no native text layer |
| `handoff.docx` | Workflow prose and an actual Word table | No structural warning expected |
| `handoff.pptx` | Two slides, positioned text, speaker notes, and a real chart relationship | PPTX is supported, but chart object structure and data are not fully extracted, so an explicit warning is expected |
| `handoff.xlsx` | Two sheets, a table, one cached formula (`42`), and one uncached formula | The uncached `F3` formula should produce an explicit cache warning if evaluated |
| `handoff-ocr-clear.png` | High-resolution, high-contrast OCR sample | No OCR quality warning expected |
| `handoff-ocr-clear.jpg` | Clear JPEG variant of the same sample | No OCR quality warning expected |
| `handoff-ocr-low-quality.jpg` | Small, noisy, heavily compressed OCR sample | OCR confidence may be reduced |
| `handoff.md`, `handoff.txt`, `handoff.csv` | Native text and tabular variants of the same workflow | No warning expected |
| `source-conversations.jsonl` | 12 source families and 24 distinct conversations | `_reviewNote`, `_acl`, `_operatorAnnotation`, and `_ingestBatch` are management metadata, not answer text |
| `malformed.pdf` | Truncated PDF structure with no valid cross-reference table | Invalid-file failure, independent of format support |
| `unsupported.rtf` | Valid small RTF document | Unsupported-format failure because RTF is outside the supported v0.2 formats |

`manifest.json` fixes SHA-256 and size for every corpus input and records the
expected structure and warning expectation. The tests check those signatures,
inspect the actual PDF/PNG/JPEG bytes, parse the Office ZIP/XML parts, and verify
that the generator reproduces the checked-in signatures.

`manifest.json` 固定每个语料输入的 SHA-256 和文件大小，并记录预期结构与 warning。测试会
校验摘要，检查 PDF/PNG/JPEG 实际字节，读取 Office ZIP/XML 结构，并验证生成器可以复现已
签入的签名。

## Regeneration / 重新生成

Use the shared Catalyst v0.2 environment; do not install these generator
libraries into the legacy Echo environment:

```bash
/home/baijin/Dev/Cyrene/.worktrees/catalyst-v02-20261007/.venv/bin/python \
  tests/fixtures/catalyst-v02/build_fixtures.py
```

The generator uses Pillow, ReportLab, python-docx, python-pptx, and openpyxl from
that environment. It normalizes Office ZIP entry timestamps and order and
fixes document metadata before calculating the manifest.

请使用共享的 Catalyst v0.2 环境，不要将生成器依赖安装到旧 Echo 环境。生成器依赖该环境中
已有的 Pillow、ReportLab、python-docx、python-pptx 和 openpyxl；它会统一 Office ZIP 条目的
时间戳和顺序，并固定文档元数据后再计算 manifest。
