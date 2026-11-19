"""Pure-stdlib OOXML (.docx) text reader and structured Markdown-to-Word writer.

Uses only Python standard-library ``zipfile`` and ``xml.etree.ElementTree`` so
SayInk ships zero external document dependencies while supporting:
1. Importing ``.docx`` meeting transcripts (paragraphs, headings, and tables)
   with zip-bomb uncompressed byte limits and clean error messages.
2. Exporting structured meeting minutes / full meeting records to native ``.docx``
   files with Microsoft YaHei / Segoe UI styles, metadata table, headings,
   bullet/checkbox lists, and inline bold formatting.
"""

from __future__ import annotations

from collections.abc import Sequence
import io
from pathlib import Path
import re
import xml.etree.ElementTree as ET
import zipfile

from sayink.atomic_io import atomic_write_bytes

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W = f"{{{_W_NS}}}"
_XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"

MAX_DOCX_UNCOMPRESSED_BYTES = 32 * 1024 * 1024

_HEADING_STYLE_MAP: dict[str, str] = {
    "heading1": "# ",
    "heading 1": "# ",
    "1": "# ",
    "标题1": "# ",
    "标题 1": "# ",
    "heading2": "## ",
    "heading 2": "## ",
    "2": "## ",
    "标题2": "## ",
    "标题 2": "## ",
    "heading3": "### ",
    "heading 3": "### ",
    "3": "### ",
    "标题3": "### ",
    "标题 3": "### ",
}

_BOLD_SPLIT_RE = re.compile(r"(\*\*[^*\n]+\*\*)")
_ORDERED_ITEM_RE = re.compile(r"^\d+[.)、]\s+(.+)$")

_CONTENT_TYPES_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml"
            ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml"
            ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>
"""

_ROOT_RELS_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1"
                Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
                Target="word/document.xml"/>
</Relationships>
"""

_DOC_RELS_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1"
                Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles"
                Target="styles.xml"/>
</Relationships>
"""

_STYLES_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:docDefaults>
    <w:rPrDefault>
      <w:rPr>
        <w:rFonts w:ascii="Segoe UI" w:hAnsi="Segoe UI" w:eastAsia="Microsoft YaHei" w:cs="Segoe UI"/>
        <w:sz w:val="21"/>
        <w:szCs w:val="21"/>
        <w:color w:val="111827"/>
      </w:rPr>
    </w:rPrDefault>
    <w:pPrDefault>
      <w:pPr>
        <w:spacing w:after="120" w:line="320" w:lineRule="auto"/>
      </w:pPr>
    </w:pPrDefault>
  </w:docDefaults>
  <w:style w:type="paragraph" w:default="1" w:styleId="Normal">
    <w:name w:val="Normal"/>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading1">
    <w:name w:val="heading 1"/>
    <w:basedOn w:val="Normal"/>
    <w:pPr><w:keepNext/><w:spacing w:before="240" w:after="120"/></w:pPr>
    <w:rPr><w:b/><w:sz w:val="36"/><w:szCs w:val="36"/><w:color w:val="0F172A"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading2">
    <w:name w:val="heading 2"/>
    <w:basedOn w:val="Normal"/>
    <w:pPr><w:keepNext/><w:spacing w:before="200" w:after="80"/></w:pPr>
    <w:rPr><w:b/><w:sz w:val="28"/><w:szCs w:val="28"/><w:color w:val="1E293B"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading3">
    <w:name w:val="heading 3"/>
    <w:basedOn w:val="Normal"/>
    <w:pPr><w:keepNext/><w:spacing w:before="140" w:after="60"/></w:pPr>
    <w:rPr><w:b/><w:sz w:val="24"/><w:szCs w:val="24"/><w:color w:val="334155"/></w:rPr>
  </w:style>
  <w:style w:type="paragraph" w:styleId="ListBullet">
    <w:name w:val="List Bullet"/>
    <w:basedOn w:val="Normal"/>
    <w:pPr><w:ind w:left="360" w:hanging="240"/><w:spacing w:after="80"/></w:pPr>
  </w:style>
</w:styles>
"""


class DocxFormatError(ValueError):
    """Raised when a ``.docx`` archive is unreadable, damaged, or empty."""


def _paragraph_heading_prefix(p_elem: ET.Element) -> str:
    p_pr = p_elem.find(f"{_W}pPr")
    if p_pr is None:
        return ""
    p_style = p_pr.find(f"{_W}pStyle")
    if p_style is None:
        return ""
    raw_val = (p_style.attrib.get(f"{_W}val") or "").strip().casefold()
    return _HEADING_STYLE_MAP.get(raw_val, "")


def _extract_paragraph_text(p_elem: ET.Element) -> str:
    chunks: list[str] = []
    for node in p_elem.iter():
        tag = node.tag
        if tag == f"{_W}t" and node.text:
            chunks.append(node.text)
        elif tag == f"{_W}tab":
            chunks.append("\t")
        elif tag in (f"{_W}br", f"{_W}cr"):
            chunks.append("\n")
    raw = "".join(chunks).replace("\x00", "").strip()
    if not raw:
        return ""
    prefix = _paragraph_heading_prefix(p_elem)
    if prefix and "\n" not in raw and not raw.startswith("#"):
        return f"{prefix}{raw}"
    return raw


def _extract_table_lines(tbl_elem: ET.Element) -> list[str]:
    rows_out: list[str] = []
    for tr in tbl_elem.findall(f"{_W}tr"):
        cells: list[str] = []
        for tc in tr.findall(f"{_W}tc"):
            cell_parts = [
                _extract_paragraph_text(p)
                for p in tc.findall(f"{_W}p")
            ]
            cell_text = " ".join(part for part in cell_parts if part).strip()
            if cell_text:
                cells.append(cell_text)
        if cells:
            rows_out.append("  ".join(cells))
    return rows_out


def _read_document_xml_bytes(path: Path, max_uncompressed_bytes: int) -> bytes:
    try:
        with zipfile.ZipFile(path, "r") as zf:
            total_uncompressed = sum(max(0, info.file_size) for info in zf.infolist())
            if total_uncompressed > max_uncompressed_bytes:
                raise DocxFormatError("Word 文档解压体积超过安全上限，请拆分后再导入。")
            if "word/document.xml" not in zf.namelist():
                raise DocxFormatError("不是有效的 Word（.docx）文档，缺少正文内容。")
            info = zf.getinfo("word/document.xml")
            if info.file_size > max_uncompressed_bytes:
                raise DocxFormatError("Word 文档正文体积超过可导入上限，请拆分后再导入。")
            return zf.read("word/document.xml")
    except zipfile.BadZipFile as exc:
        if path.suffix.lower() == ".doc":
            raise DocxFormatError("暂不支持旧版二进制 .doc，请另存为 .docx 后导入。") from exc
        raise DocxFormatError("Word（.docx）文件已损坏或格式无效，无法读取。") from exc


def read_docx_text(
    path: str | Path,
    *,
    max_uncompressed_bytes: int = MAX_DOCX_UNCOMPRESSED_BYTES,
) -> str:
    """Extract clean plain/Markdown text from an OOXML ``.docx`` file."""
    xml_bytes = _read_document_xml_bytes(Path(path), max_uncompressed_bytes)
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise DocxFormatError("Word（.docx）内部 XML 已损坏，无法解析正文。") from exc

    body = root.find(f"{_W}body")
    container = body if body is not None else root
    blocks: list[str] = []
    for child in list(container):
        if child.tag == f"{_W}p":
            para = _extract_paragraph_text(child)
            if para:
                blocks.append(para)
        elif child.tag == f"{_W}tbl":
            blocks.extend(_extract_table_lines(child))

    result = "\n".join(blocks).strip()
    if not result:
        raise DocxFormatError("Word（.docx）文档里没有可导入的文字。")
    return result


def _sanitize_xml_text(text: str) -> str:
    """Strip control characters forbidden by XML 1.0 (preserving tab/LF/CR)."""
    return "".join(
        ch
        for ch in (text or "")
        if ch in ("\t", "\n", "\r")
        or (0x20 <= ord(ch) <= 0xD7FF)
        or (0xE000 <= ord(ch) <= 0xFFFD)
        or (0x10000 <= ord(ch) <= 0x10FFFF)
    )


def _append_run(p_elem: ET.Element, text: str, *, bold: bool = False, color: str = "") -> None:
    cleaned = _sanitize_xml_text(text)
    if not cleaned:
        return
    r_elem = ET.SubElement(p_elem, f"{_W}r")
    if bold or color:
        r_pr = ET.SubElement(r_elem, f"{_W}rPr")
        if bold:
            ET.SubElement(r_pr, f"{_W}b")
        if color:
            c_elem = ET.SubElement(r_pr, f"{_W}color")
            c_elem.set(f"{_W}val", color)
    t_elem = ET.SubElement(r_elem, f"{_W}t")
    if cleaned[:1].isspace() or cleaned[-1:].isspace():
        t_elem.set(_XML_SPACE, "preserve")
    t_elem.text = cleaned


def _append_inline_runs(p_elem: ET.Element, text: str) -> None:
    for token in _BOLD_SPLIT_RE.split(text):
        if not token:
            continue
        if token.startswith("**") and token.endswith("**") and len(token) > 4:
            _append_run(p_elem, token[2:-2], bold=True)
        else:
            _append_run(p_elem, token, bold=False)


def _append_styled_paragraph(body: ET.Element, text: str, *, style_id: str = "") -> ET.Element:
    p_elem = ET.SubElement(body, f"{_W}p")
    if style_id:
        p_pr = ET.SubElement(p_elem, f"{_W}pPr")
        p_style = ET.SubElement(p_pr, f"{_W}pStyle")
        p_style.set(f"{_W}val", style_id)
    _append_inline_runs(p_elem, text)
    return p_elem


def _append_metadata_table(body: ET.Element, rows: Sequence[tuple[str, str]]) -> None:
    valid_rows = [(k.strip(), v.strip()) for k, v in rows if k.strip() and v.strip()]
    if not valid_rows:
        return
    tbl = ET.SubElement(body, f"{_W}tbl")
    tbl_pr = ET.SubElement(tbl, f"{_W}tblPr")
    borders = ET.SubElement(tbl_pr, f"{_W}tblBorders")
    for side in ("top", "left", "bottom", "right", "insideH", "insideV"):
        b_elem = ET.SubElement(borders, f"{_W}{side}")
        b_elem.set(f"{_W}val", "single")
        b_elem.set(f"{_W}sz", "4")
        b_elem.set(f"{_W}space", "0")
        b_elem.set(f"{_W}color", "E5E7EB")

    for key, val in valid_rows:
        tr = ET.SubElement(tbl, f"{_W}tr")
        tc_key = ET.SubElement(tr, f"{_W}tc")
        p_key = ET.SubElement(tc_key, f"{_W}p")
        _append_run(p_key, key, bold=True, color="4B5563")
        tc_val = ET.SubElement(tr, f"{_W}tc")
        p_val = ET.SubElement(tc_val, f"{_W}p")
        _append_inline_runs(p_val, val)
    ET.SubElement(body, f"{_W}p")


def _classify_markdown_line(stripped: str) -> tuple[str, str]:
    """Return ``(style_id, rendered_text)`` for a single non-empty Markdown line."""
    if stripped.startswith("### "):
        return "Heading3", stripped[4:].strip()
    if stripped.startswith("## "):
        return "Heading2", stripped[3:].strip()
    if stripped.startswith("# "):
        return "Heading1", stripped[2:].strip()
    if stripped.startswith(("- [ ] ", "* [ ] ")):
        return "ListBullet", f"☐ {stripped[6:].strip()}"
    if stripped.startswith(("- [x] ", "- [X] ", "* [x] ", "* [X] ")):
        return "ListBullet", f"☑ {stripped[6:].strip()}"
    if stripped.startswith(("- ", "* ", "• ")):
        return "ListBullet", f"• {stripped[2:].strip()}"
    ordered = _ORDERED_ITEM_RE.match(stripped)
    if ordered is not None:
        return "ListBullet", stripped
    return "", stripped


def _starts_with_matching_h1(lines: Sequence[str], clean_title: str) -> bool:
    if not clean_title:
        return False
    for raw in lines:
        s = raw.strip()
        if not s:
            continue
        return s.startswith("# ") and s[2:].strip() == clean_title
    return False


def _append_a4_section_props(body: ET.Element) -> None:
    sect_pr = ET.SubElement(body, f"{_W}sectPr")
    pg_sz = ET.SubElement(sect_pr, f"{_W}pgSz")
    pg_sz.set(f"{_W}w", "11906")  # A4 portrait
    pg_sz.set(f"{_W}h", "16838")
    pg_mar = ET.SubElement(sect_pr, f"{_W}pgMar")
    for attr, val in (("top", "1440"), ("right", "1440"), ("bottom", "1440"), ("left", "1440")):
        pg_mar.set(f"{_W}{attr}", val)


def _build_document_xml(
    *,
    title: str,
    metadata_rows: Sequence[tuple[str, str]],
    markdown_body: str,
) -> bytes:
    ET.register_namespace("w", _W_NS)
    doc = ET.Element(f"{_W}document")
    body = ET.SubElement(doc, f"{_W}body")

    clean_title = title.strip()
    body_lines = (markdown_body or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    skip_duplicate_h1 = _starts_with_matching_h1(body_lines, clean_title)

    if clean_title:
        _append_styled_paragraph(body, clean_title, style_id="Heading1")
    if metadata_rows:
        _append_metadata_table(body, metadata_rows)

    skipped_h1 = False
    for raw_line in body_lines:
        stripped = raw_line.strip()
        if not stripped:
            continue
        if skip_duplicate_h1 and not skipped_h1 and stripped.startswith("# "):
            skipped_h1 = True
            continue
        style_id, text = _classify_markdown_line(stripped)
        if text:
            _append_styled_paragraph(body, text, style_id=style_id)

    _append_a4_section_props(body)
    return ET.tostring(doc, encoding="utf-8", xml_declaration=True)


def write_docx_document(
    path: str | Path,
    *,
    title: str = "",
    metadata_rows: Sequence[tuple[str, str]] = (),
    markdown_body: str = "",
) -> None:
    """Write a valid OOXML ``.docx`` file from Markdown headings, lists, and paragraphs."""
    target = Path(path)
    doc_xml = _build_document_xml(
        title=title,
        metadata_rows=metadata_rows,
        markdown_body=markdown_body,
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES_XML)
        zf.writestr("_rels/.rels", _ROOT_RELS_XML)
        zf.writestr("word/_rels/document.xml.rels", _DOC_RELS_XML)
        zf.writestr("word/styles.xml", _STYLES_XML)
        zf.writestr("word/document.xml", doc_xml)
    atomic_write_bytes(target, buf.getvalue())


__all__ = [
    "DocxFormatError",
    "MAX_DOCX_UNCOMPRESSED_BYTES",
    "read_docx_text",
    "write_docx_document",
]
