#!/usr/bin/env python3
"""Extract canonical raw source text and a compact source manifest from a PDF."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from common import (
    clean_pdf_line,
    emit,
    enrich_metadata,
    ensure_parent,
    extract_appendix_index,
    extract_caption_lines,
    fitz,
    match_section_heading,
    maybe_load_json_record,
    normalize_heading,
    normalize_whitespace,
    paper_id_for_record,
    pdf_coverage_summary,
    resolve_reference,
    stop_section_reason,
)
from mineru_v4 import run_mineru_v4_parse

MATH_SIGNAL_RE = re.compile(
    r"(?:\\(?:frac|sum|log|exp|argmax|argmin|mathbb|mathbf)|[$=]|[<>]=?|[∑∏≤≥≈∈]|O\([^)]+\))"
)
MARKDOWN_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__ or "extract source text")
    p.add_argument(
        "--input",
        required=True,
        help="Fetch JSON path, metadata JSON path, local PDF path, JSON string, or reference.",
    )
    p.add_argument("--output", default="", help="Source manifest JSON output path.")
    p.add_argument(
        "--raw-sections-output",
        default="",
        help="Canonical raw sections JSONL output path.",
    )
    p.add_argument("--full-text-output", default="", help="Optional derived Markdown output path.")
    p.add_argument("--paper-id", default="", help="Canonical paper id if already known.")
    p.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Optional page limit. Omit for all pages.",
    )
    return p


def ensure_record(input_value: str) -> dict[str, Any]:
    record = maybe_load_json_record(input_value)
    if record is not None:
        return dict(record)
    path = Path(input_value).expanduser()
    if path.exists() and path.is_file() and path.suffix.lower() == ".pdf":
        return {
            "paper_id": f"local:{path.stem}",
            "title": path.stem,
            "pdf_path": str(path.resolve()),
            "source_type": "local_pdf",
        }
    return enrich_metadata(resolve_reference(input_value))


def resolve_pdf_path(record: dict[str, Any]) -> Path | None:
    for key in ("pdf_path", "local_pdf_path"):
        value = normalize_whitespace(str(record.get(key, "")))
        if not value:
            continue
        path = Path(value).expanduser()
        if path.exists() and path.is_file():
            return path.resolve()
    return None


def section_id(base: str, seen: dict[str, int]) -> str:
    safe = re.sub(r"[^a-z0-9]+", "-", normalize_heading(base)).strip("-") or "section"
    seen[safe] = seen.get(safe, 0) + 1
    suffix = "" if seen[safe] == 1 else f"-{seen[safe]}"
    return f"sec:{safe}{suffix}"


def text_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def language_hint_for_text(text: str) -> str:
    cjk_chars = len(re.findall(r"[\u3400-\u9fff]", text or ""))
    latin_chars = len(re.findall(r"[A-Za-z]", text or ""))
    total = cjk_chars + latin_chars
    if total == 0:
        return "unknown"
    if cjk_chars / total >= 0.6:
        return "zh"
    if latin_chars / total >= 0.6:
        return "en"
    return "mixed"


def new_record(kind: str, title: str, page_number: int, seen: dict[str, int]) -> dict[str, Any]:
    sid = section_id(title or kind, seen)
    return {
        "record_type": "section",
        "section_id": sid,
        "kind": kind,
        "title": normalize_whitespace(title) or kind,
        "page_start": page_number,
        "page_end": page_number,
        "_lines": [],
    }


def finalize_section(record: dict[str, Any]) -> dict[str, Any] | None:
    lines = [line for line in record.pop("_lines", []) if normalize_whitespace(str(line))]
    text = "\n".join(lines).strip()
    if not text:
        return None
    record["text"] = text
    record["char_count"] = len(text)
    record["text_hash_sha256"] = text_hash(text)
    return record


def extract_page_texts(pdf_path: Path, max_pages: int | None) -> list[dict[str, Any]]:
    if fitz is None:
        raise RuntimeError("PyMuPDF is required for source text extraction.")
    doc = fitz.open(pdf_path)
    try:
        page_limit = len(doc) if max_pages is None else min(len(doc), max_pages)
        return [
            {"page": page_index + 1, "text": doc[page_index].get_text("text")}
            for page_index in range(page_limit)
        ]
    finally:
        doc.close()


def extract_raw_sections(page_texts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[str, int] = {}
    sections: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    for page in page_texts:
        page_number = int(page["page"])
        if current is None:
            current = new_record("preamble", "preamble", page_number, seen)
        current["page_end"] = page_number

        for raw_line in str(page.get("text", "")).splitlines():
            line = clean_pdf_line(raw_line)
            if not line:
                continue
            stop_reason = stop_section_reason(line, allow_prefix=True)
            heading = match_section_heading(line)
            if stop_reason or (heading and heading != "stop"):
                finalized = finalize_section(current)
                if finalized is not None:
                    sections.append(finalized)
                kind = stop_reason or str(heading)
                current = new_record(kind, line, page_number, seen)
                continue
            current.setdefault("_lines", []).append(line)

    if current is not None:
        finalized = finalize_section(current)
        if finalized is not None:
            sections.append(finalized)
    return sections


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def use_pymupdf_fallback() -> bool:
    return env_bool("DEEPPAPERNOTE_CUSTOM_ALLOW_PYMUPDF_FALLBACK", False)


def mineru_workdir(output_path: str, pdf_path: Path) -> Path:
    if output_path:
        base = Path(output_path).expanduser().resolve().parent
    else:
        base = Path.cwd().resolve() / "tmp" / "DeepPaperNote_mineru"
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", pdf_path.stem).strip("._-") or "paper"
    return base / f"{safe}_mineru"


def kind_for_heading(title: str) -> str:
    lowered = normalize_whitespace(title).lower()
    if "abstract" in lowered:
        return "abstract"
    if "introduction" in lowered:
        return "introduction"
    if "related" in lowered:
        return "introduction"
    if any(token in lowered for token in ["method", "model", "approach", "algorithm", "framework"]):
        return "method"
    if any(token in lowered for token in ["data", "dataset", "material", "task"]):
        return "data"
    if any(token in lowered for token in ["experiment", "result", "evaluation", "analysis"]):
        return "experiment"
    if any(token in lowered for token in ["discussion", "conclusion", "future"]):
        return "conclusion"
    if "appendix" in lowered:
        return "appendix"
    if "reference" in lowered:
        return "references"
    numeric = re.match(r"^\d+(?:\.\d+)*\s+(.+)$", lowered)
    if numeric:
        return kind_for_heading(numeric.group(1))
    return "section"


def extract_raw_sections_from_markdown(markdown: str, pages: dict[int, str]) -> list[dict[str, Any]]:
    seen: dict[str, int] = {}
    sections: list[dict[str, Any]] = []
    current = new_record("preamble", "preamble", 1, seen)
    line_to_page: dict[str, int] = {}
    for page_number, page_text in pages.items():
        for line in str(page_text).splitlines():
            cleaned = normalize_whitespace(line)
            if cleaned and cleaned not in line_to_page:
                line_to_page[cleaned] = int(page_number)

    for raw_line in markdown.splitlines():
        line = raw_line.rstrip()
        cleaned = normalize_whitespace(line)
        if not cleaned:
            if current.get("_lines"):
                current.setdefault("_lines", []).append("")
            continue
        heading_match = MARKDOWN_HEADING_RE.match(line.strip())
        plain_heading = match_section_heading(cleaned) if len(cleaned) <= 160 else ""
        if heading_match or plain_heading:
            title = heading_match.group(2).strip() if heading_match else cleaned
            page_number = line_to_page.get(cleaned, int(current.get("page_end", 1) or 1))
            finalized = finalize_section(current)
            if finalized is not None:
                sections.append(finalized)
            current = new_record(kind_for_heading(title), title, page_number, seen)
            current["page_end"] = page_number
            continue
        page_number = line_to_page.get(cleaned)
        if page_number:
            current["page_end"] = max(int(current.get("page_end", 1) or 1), page_number)
        current.setdefault("_lines", []).append(line)

    finalized = finalize_section(current)
    if finalized is not None:
        sections.append(finalized)
    return sections


def page_texts_from_mineru_pages(pages: dict[int, str]) -> list[dict[str, Any]]:
    return [
        {"page": int(page), "text": text}
        for page, text in sorted(pages.items())
        if normalize_whitespace(str(text))
    ] or [{"page": 1, "text": ""}]


def section_ids_for_page(sections: list[dict[str, Any]], page_number: int) -> list[str]:
    ids = [
        str(section.get("section_id", ""))
        for section in sections
        if int(section.get("page_start", 0) or 0)
        <= page_number
        <= int(section.get("page_end", 0) or 0)
    ]
    return [sid for sid in ids if sid]


def build_pages(
    page_texts: list[dict[str, Any]],
    sections: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    for page in page_texts:
        page_number = int(page["page"])
        text = str(page.get("text", ""))
        pages.append(
            {
                "page": page_number,
                "char_count": len(normalize_whitespace(text)),
                "section_ids": section_ids_for_page(sections, page_number),
            }
        )
    return pages


def primary_section_for_page(sections: list[dict[str, Any]], page_number: int) -> str:
    ids = section_ids_for_page(sections, page_number)
    return ids[0] if ids else ""


def caption_manifest(
    page_texts: list[dict[str, Any]],
    sections: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    captions = {"figures": [], "tables": []}
    for page in page_texts:
        page_number = int(page["page"])
        section = primary_section_for_page(sections, page_number)
        for key, kind in (("figures", "figure"), ("tables", "table")):
            for item in extract_caption_lines(str(page.get("text", "")), kind):
                captions[key].append(
                    {
                        **item,
                        "page": page_number,
                        "pages": [page_number],
                        "section_id": section,
                    }
                )
    return captions


def math_index(sections: list[dict[str, Any]], *, max_items: int = 200) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for section in sections:
        for line in str(section.get("text", "")).splitlines():
            cleaned = normalize_whitespace(line)
            if not cleaned or len(cleaned) > 240 or not MATH_SIGNAL_RE.search(cleaned):
                continue
            items.append(
                {
                    "text": cleaned,
                    "section_id": section.get("section_id", ""),
                    "page_start": section.get("page_start"),
                    "page_end": section.get("page_end"),
                }
            )
            if len(items) >= max_items:
                return items
    return items


def write_jsonl(records: list[dict[str, Any]], output_path: str) -> None:
    ensure_parent(output_path)
    path = Path(output_path).expanduser().resolve()
    path.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
        encoding="utf-8",
    )


def write_full_text_markdown(records: list[dict[str, Any]], output_path: str, title: str) -> None:
    ensure_parent(output_path)
    lines = [f"# {title or 'Full Source Text'}", ""]
    for record in records:
        lines.extend(
            [
                f"## {record.get('section_id', '')} {record.get('title', '')}".strip(),
                f"_Pages {record.get('page_start')}-{record.get('page_end')}_",
                "",
                str(record.get("text", "")).strip(),
                "",
            ]
        )
    Path(output_path).expanduser().resolve().write_text(
        "\n".join(lines).rstrip() + "\n",
        encoding="utf-8",
    )


def build_manifest(
    *,
    record: dict[str, Any],
    pdf_path: Path,
    page_texts: list[dict[str, Any]],
    sections: list[dict[str, Any]],
    raw_sections_output: str,
    full_text_output: str,
    max_pages: int | None,
) -> dict[str, Any]:
    coverage = pdf_coverage_summary(pdf_path, max_pages=max_pages)
    total_pages = coverage.get("total_pages")
    extracted_pages = len(page_texts)
    full_text = "\n".join(str(page.get("text", "")) for page in page_texts)
    source_coverage = {
        "total_pages": total_pages,
        "text_max_pages": max_pages,
        "text_pages_extracted": extracted_pages,
        "text_pages_scanned": extracted_pages,
        "text_truncated": bool(coverage.get("truncated_due_to_page_limit")),
        "truncated_due_to_page_limit": bool(coverage.get("truncated_due_to_page_limit")),
        "appendix_detected": bool(coverage.get("appendix_detected")),
        "appendix_start_page": coverage.get("appendix_start_page"),
        "references_start_page": coverage.get("references_start_page"),
    }
    return {
        "status": "ok",
        "script": "extract_source_text.py",
        "schema_version": 1,
        "paper_id": record.get("paper_id") or paper_id_for_record(record),
        "title": record.get("title", ""),
        "source_kind": "pdf_text",
        "raw_sections_path": (
            str(Path(raw_sections_output).expanduser().resolve()) if raw_sections_output else ""
        ),
        "full_text_md_path": (
            str(Path(full_text_output).expanduser().resolve()) if full_text_output else ""
        ),
        "pdf": {
            "path": str(pdf_path),
            "total_pages": total_pages,
            "text_pages_extracted": extracted_pages,
            "text_max_pages": max_pages,
            "text_truncated": bool(coverage.get("truncated_due_to_page_limit")),
        },
        "coverage": source_coverage,
        "sections": [
            {
                key: section.get(key)
                for key in (
                    "section_id",
                    "kind",
                    "title",
                    "page_start",
                    "page_end",
                    "char_count",
                    "text_hash_sha256",
                )
            }
            for section in sections
        ],
        "pages": build_pages(page_texts, sections),
        "captions": caption_manifest(page_texts, sections),
        "math_index": math_index(sections),
        "appendix_index": extract_appendix_index(pdf_path, coverage),
        "language_hint": language_hint_for_text(full_text),
        "text_hash_sha256": text_hash(full_text),
    }


def build_mineru_manifest(
    *,
    record: dict[str, Any],
    pdf_path: Path,
    mineru_result: dict[str, Any],
    page_texts: list[dict[str, Any]],
    sections: list[dict[str, Any]],
    raw_sections_output: str,
    full_text_output: str,
    max_pages: int | None,
) -> dict[str, Any]:
    total_pages = max([int(page.get("page", 1) or 1) for page in page_texts] or [1])
    full_text = "\n".join(str(page.get("text", "")) for page in page_texts)
    source_coverage = {
        "total_pages": total_pages,
        "text_max_pages": max_pages,
        "text_pages_extracted": len(page_texts),
        "text_pages_scanned": len(page_texts),
        "text_truncated": False,
        "truncated_due_to_page_limit": False,
        "appendix_detected": any(section.get("kind") == "appendix" for section in sections),
        "appendix_start_page": next((section.get("page_start") for section in sections if section.get("kind") == "appendix"), None),
        "references_start_page": next((section.get("page_start") for section in sections if section.get("kind") == "references"), None),
    }
    return {
        "status": "ok",
        "script": "extract_source_text.py",
        "schema_version": 1,
        "paper_id": record.get("paper_id") or paper_id_for_record(record),
        "title": record.get("title", ""),
        "source_kind": "mineru_v4_ocr",
        "raw_sections_path": str(Path(raw_sections_output).expanduser().resolve()) if raw_sections_output else "",
        "full_text_md_path": str(Path(full_text_output).expanduser().resolve()) if full_text_output else "",
        "mineru": {
            "api_version": "v4",
            "api_mode": "precise",
            "model_version": mineru_result.get("model_version", ""),
            "is_ocr": mineru_result.get("is_ocr", True),
            "enable_formula": mineru_result.get("enable_formula", True),
            "enable_table": mineru_result.get("enable_table", True),
            "language": mineru_result.get("language", ""),
            "batch_id": mineru_result.get("batch_id", ""),
            "data_id": mineru_result.get("data_id", ""),
            "zip_path": mineru_result.get("zip_path", ""),
            "extract_dir": mineru_result.get("extract_dir", ""),
            "full_zip_url": mineru_result.get("full_zip_url", ""),
        },
        "pdf": {
            "path": str(pdf_path),
            "total_pages": total_pages,
            "text_pages_extracted": len(page_texts),
            "text_max_pages": max_pages,
            "text_truncated": False,
        },
        "coverage": source_coverage,
        "sections": [
            {
                key: section.get(key)
                for key in (
                    "section_id",
                    "kind",
                    "title",
                    "page_start",
                    "page_end",
                    "char_count",
                    "text_hash_sha256",
                )
            }
            for section in sections
        ],
        "pages": build_pages(page_texts, sections),
        "captions": caption_manifest(page_texts, sections),
        "math_index": math_index(sections),
        "appendix_index": {},
        "language_hint": language_hint_for_text(full_text),
        "text_hash_sha256": text_hash(full_text),
    }


def main() -> None:
    args = parser().parse_args()
    record = ensure_record(args.input)
    record["paper_id"] = args.paper_id or record.get("paper_id") or paper_id_for_record(record)
    pdf_path = resolve_pdf_path(record)
    if pdf_path is None:
        raise SystemExit("extract_source_text.py requires a resolvable local PDF path.")

    raw_sections_output = args.raw_sections_output
    if not raw_sections_output and args.output:
        raw_sections_output = str(
            Path(args.output).with_name(
                Path(args.output).stem.replace("_source_manifest", "") + "_raw_sections.jsonl"
            )
        )

    mineru_required = not use_pymupdf_fallback()
    try:
        mineru_result = run_mineru_v4_parse(
            pdf_path=pdf_path,
            workdir=mineru_workdir(args.output, pdf_path),
            model_version=os.environ.get("MINERU_MODEL_VERSION", os.environ.get("DEEPPAPERNOTE_MINERU_MODEL_VERSION", "vlm")).strip() or "vlm",
            is_ocr=env_bool("MINERU_IS_OCR", env_bool("DEEPPAPERNOTE_MINERU_IS_OCR", True)),
            enable_formula=env_bool("MINERU_ENABLE_FORMULA", env_bool("DEEPPAPERNOTE_MINERU_ENABLE_FORMULA", True)),
            enable_table=env_bool("MINERU_ENABLE_TABLE", env_bool("DEEPPAPERNOTE_MINERU_ENABLE_TABLE", True)),
            language=os.environ.get("MINERU_LANGUAGE", os.environ.get("DEEPPAPERNOTE_MINERU_LANGUAGE", "ch")).strip() or "ch",
            page_ranges=os.environ.get("MINERU_PAGE_RANGES", os.environ.get("DEEPPAPERNOTE_MINERU_PAGE_RANGES", "")).strip(),
            no_cache=env_bool("MINERU_NO_CACHE", env_bool("DEEPPAPERNOTE_MINERU_NO_CACHE", False)),
            timeout_seconds=env_int("MINERU_TIMEOUT_SECONDS", env_int("DEEPPAPERNOTE_MINERU_TIMEOUT_SECONDS", 900)),
            poll_interval_seconds=env_int("MINERU_POLL_INTERVAL_SECONDS", env_int("DEEPPAPERNOTE_MINERU_POLL_INTERVAL_SECONDS", 5)),
        )
        page_texts = page_texts_from_mineru_pages(mineru_result.get("pages", {}))
        sections = extract_raw_sections_from_markdown(str(mineru_result.get("markdown", "")), mineru_result.get("pages", {}))
        manifest_builder = "mineru"
    except Exception as exc:
        if mineru_required:
            raise SystemExit(f"MinerU v4 source extraction failed: {exc}") from exc
        page_texts = extract_page_texts(pdf_path, args.max_pages)
        sections = extract_raw_sections(page_texts)
        mineru_result = {}
        manifest_builder = "pymupdf_fallback"

    if raw_sections_output:
        write_jsonl(sections, raw_sections_output)
    if args.full_text_output:
        write_full_text_markdown(sections, args.full_text_output, str(record.get("title", "")))

    if manifest_builder == "mineru":
        manifest = build_mineru_manifest(
            record=record,
            pdf_path=pdf_path,
            mineru_result=mineru_result,
            page_texts=page_texts,
            sections=sections,
            raw_sections_output=raw_sections_output,
            full_text_output=args.full_text_output,
            max_pages=args.max_pages,
        )
    else:
        manifest = build_manifest(
            record=record,
            pdf_path=pdf_path,
            page_texts=page_texts,
            sections=sections,
            raw_sections_output=raw_sections_output,
            full_text_output=args.full_text_output,
            max_pages=args.max_pages,
        )
    emit(manifest, args.output)


if __name__ == "__main__":
    main()
