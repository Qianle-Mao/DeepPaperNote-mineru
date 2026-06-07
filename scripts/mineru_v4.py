#!/usr/bin/env python3
"""MinerU v4 precise API adapter for DeepPaperNote-mineru.

The adapter intentionally reads tokens from environment variables only:
MINERU_API_TOKEN or DEEPPAPERNOTE_MINERU_API_TOKEN.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import shutil
import ssl
import subprocess
import time
import urllib.parse
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any


BASE_URL = "https://mineru.net"
TOKEN_ENV_NAMES = ("MINERU_API_TOKEN", "DEEPPAPERNOTE_MINERU_API_TOKEN")
DONE_STATES = {"done"}
FAILED_STATES = {"failed"}
WAIT_STATES = {"waiting-file", "pending", "running", "converting"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
TRANSIENT_EXCEPTIONS = (
    TimeoutError,
    ConnectionError,
    urllib.error.URLError,
    http.client.HTTPException,
    ssl.SSLError,
)


def mineru_token() -> str:
    for name in TOKEN_ENV_NAMES:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def require_token() -> str:
    token = mineru_token()
    if not token:
        names = " or ".join(TOKEN_ENV_NAMES)
        raise RuntimeError(f"MinerU API token missing. Set {names}; do not hard-code tokens in skill files.")
    return token


def _headers(token: str, *, json_content: bool = True) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}", "Accept": "*/*"}
    if json_content:
        headers["Content-Type"] = "application/json"
    return headers


def _json_request(method: str, url: str, token: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=_headers(token), method=method)
    last_exc: Exception | None = None
    for attempt in range(1, 5):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                body = response.read().decode("utf-8")
            break
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"MinerU HTTP {exc.code}: {detail}") from exc
        except TRANSIENT_EXCEPTIONS as exc:
            last_exc = exc
            if attempt >= 4:
                raise
            time.sleep(min(2 * attempt, 8))
    else:  # pragma: no cover
        raise RuntimeError(f"MinerU request failed: {last_exc}")
    result = json.loads(body)
    if not isinstance(result, dict):
        raise RuntimeError("MinerU returned a non-object JSON payload.")
    if int(result.get("code", 0) or 0) != 0:
        raise RuntimeError(f"MinerU API error {result.get('code')}: {result.get('msg')}")
    return result


def _put_file(upload_url: str, pdf_path: Path) -> None:
    parsed = urllib.parse.urlparse(upload_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise RuntimeError(f"MinerU upload URL must be https: {upload_url}")
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    body = pdf_path.read_bytes()
    last_exc: Exception | None = None
    for attempt in range(1, 4):
        conn = http.client.HTTPSConnection(parsed.netloc, timeout=180)
        try:
            # Do not send Content-Type. OSS pre-signed URLs often bind the signed
            # header set, and urllib's default form content-type breaks the signature.
            conn.request("PUT", path, body=body, headers={"Content-Length": str(len(body))})
            response = conn.getresponse()
            detail = response.read().decode("utf-8", errors="replace")
            if response.status not in (200, 201):
                raise RuntimeError(f"MinerU upload HTTP {response.status}: {detail}")
            return
        except TRANSIENT_EXCEPTIONS as exc:
            last_exc = exc
            if attempt >= 3:
                raise
            time.sleep(min(3 * attempt, 9))
        finally:
            conn.close()
    raise RuntimeError(f"MinerU upload failed: {last_exc}")


def _download(url: str, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if shutil.which("curl"):
        _download_with_curl(url, output_path)
        return
    for attempt in range(1, 9):
        try:
            _download_once(url, output_path)
            return
        except TRANSIENT_EXCEPTIONS:
            if attempt >= 8:
                raise
            time.sleep(min(3 * attempt, 12))


def _download_with_curl(url: str, output_path: Path) -> None:
    temp_path = output_path.with_name(f"{output_path.name}.part")
    command = [
        "curl",
        "--doh-url",
        os.environ.get("MINERU_CURL_DOH_URL", "https://1.1.1.1/dns-query"),
        "--fail",
        "--location",
        "--retry",
        "8",
        "--retry-all-errors",
        "--connect-timeout",
        "30",
        "--max-time",
        "900",
        "--silent",
        "--show-error",
        "--output",
        str(temp_path),
        url,
    ]
    try:
        subprocess.run(command, check=True)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"curl failed while downloading MinerU result: exit {exc.returncode}") from exc
    if not temp_path.is_file() or temp_path.stat().st_size == 0:
        raise RuntimeError("curl downloaded an empty MinerU result file.")
    temp_path.replace(output_path)


def _download_once(url: str, output_path: Path, *, redirect_depth: int = 0) -> None:
    if redirect_depth > 3:
        raise RuntimeError(f"Too many redirects while downloading MinerU result: {url}")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise RuntimeError(f"MinerU result URL must be https: {url}")
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    temp_path = output_path.with_name(f"{output_path.name}.part")
    conn = http.client.HTTPSConnection(parsed.netloc, timeout=240)
    try:
        conn.request("GET", path, headers={"Accept": "*/*", "User-Agent": "DeepPaperNote-mineru/1.0"})
        response = conn.getresponse()
        if response.status in (301, 302, 303, 307, 308):
            location = response.getheader("Location")
            response.read()
            if not location:
                raise RuntimeError(f"MinerU result download redirect missing Location header: HTTP {response.status}")
            _download_once(urllib.parse.urljoin(url, location), output_path, redirect_depth=redirect_depth + 1)
            return
        detail_prefix = response.read(2048) if response.status != 200 else b""
        if response.status != 200:
            detail = detail_prefix.decode("utf-8", errors="replace")
            raise RuntimeError(f"MinerU result download HTTP {response.status}: {detail}")
        expected_size = int(response.getheader("Content-Length") or 0)
        written = 0
        with temp_path.open("wb") as handle:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
                written += len(chunk)
        if expected_size and written != expected_size:
            raise http.client.IncompleteRead(b"", expected_size - written)
        temp_path.replace(output_path)
    finally:
        conn.close()


def data_id_for_path(pdf_path: Path) -> str:
    digest = hashlib.sha1(str(pdf_path.resolve()).encode("utf-8")).hexdigest()[:16]
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", pdf_path.stem)[:80].strip("._-") or "paper"
    return f"{stem}_{digest}"[:128]


def create_local_batch_task(
    *,
    pdf_path: Path,
    token: str,
    model_version: str,
    is_ocr: bool,
    enable_formula: bool,
    enable_table: bool,
    language: str,
    page_ranges: str = "",
    no_cache: bool = False,
) -> dict[str, Any]:
    file_item: dict[str, Any] = {
        "name": pdf_path.name,
        "data_id": data_id_for_path(pdf_path),
        "is_ocr": is_ocr,
    }
    if page_ranges:
        file_item["page_ranges"] = page_ranges
    payload = {
        "files": [file_item],
        "model_version": model_version,
        "enable_formula": enable_formula,
        "enable_table": enable_table,
        "language": language,
        "no_cache": no_cache,
    }
    result = _json_request("POST", f"{BASE_URL}/api/v4/file-urls/batch", token, payload)
    data = result.get("data", {})
    urls = data.get("file_urls", [])
    if not isinstance(data, dict) or not data.get("batch_id") or not urls:
        raise RuntimeError(f"MinerU did not return batch_id/file_urls: {result}")
    upload_url = urls[0]
    if isinstance(upload_url, dict):
        upload_url = upload_url.get("url") or upload_url.get("file_url") or ""
    if not isinstance(upload_url, str) or not upload_url:
        raise RuntimeError(f"MinerU upload URL missing: {urls}")
    _put_file(upload_url, pdf_path)
    return {
        "batch_id": data["batch_id"],
        "data_id": file_item["data_id"],
        "upload_url_received": True,
    }


def poll_batch_result(
    *,
    batch_id: str,
    token: str,
    timeout_seconds: int,
    poll_interval_seconds: int,
) -> dict[str, Any]:
    start = time.time()
    last_result: dict[str, Any] = {}
    transient_failures = 0
    while time.time() - start <= timeout_seconds:
        try:
            result = _json_request("GET", f"{BASE_URL}/api/v4/extract-results/batch/{batch_id}", token)
            transient_failures = 0
        except TRANSIENT_EXCEPTIONS as exc:
            transient_failures += 1
            last_result = {"transient_error": str(exc), "transient_failures": transient_failures}
            time.sleep(min(poll_interval_seconds * max(transient_failures, 1), 30))
            continue
        last_result = result
        data = result.get("data", {})
        items = data.get("extract_result", []) if isinstance(data, dict) else []
        if isinstance(items, dict):
            items = [items]
        if not isinstance(items, list) or not items:
            time.sleep(poll_interval_seconds)
            continue
        item = items[0]
        state = str(item.get("state", "")).strip()
        if state in DONE_STATES:
            if not item.get("full_zip_url"):
                raise RuntimeError(f"MinerU task is done but full_zip_url is missing: {item}")
            return item
        if state in FAILED_STATES:
            raise RuntimeError(f"MinerU task failed: {item.get('err_msg') or item}")
        if state not in WAIT_STATES:
            raise RuntimeError(f"MinerU task entered unknown state {state!r}: {item}")
        time.sleep(poll_interval_seconds)
    raise TimeoutError(f"MinerU polling timed out after {timeout_seconds}s. Last result: {last_result}")


def unzip_result(zip_path: Path, extract_dir: Path) -> None:
    if extract_dir.exists():
        shutil.rmtree(extract_dir)
    extract_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(extract_dir)


def find_first(root: Path, filename: str) -> Path | None:
    for path in sorted(root.rglob(filename)):
        if path.is_file():
            return path
    return None


def find_content_list(root: Path) -> Path | None:
    candidates = sorted(root.rglob("*content_list*.json"))
    return candidates[0] if candidates else None


def load_json_path(path: Path | None) -> Any:
    if path is None or not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def markdown_text_from_result(extract_dir: Path) -> str:
    md_path = find_first(extract_dir, "full.md")
    if md_path and md_path.is_file():
        return md_path.read_text(encoding="utf-8")
    content_list = load_json_path(find_content_list(extract_dir))
    if isinstance(content_list, list):
        lines: list[str] = []
        for item in content_list:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or item.get("content") or "").strip()
            if text:
                lines.append(text)
        return "\n\n".join(lines)
    return ""


def content_list_pages(extract_dir: Path) -> dict[int, str]:
    content_list = load_json_path(find_content_list(extract_dir))
    pages: dict[int, list[str]] = {}
    if isinstance(content_list, list):
        for item in content_list:
            if not isinstance(item, dict):
                continue
            page_value = item.get("page_idx", item.get("page", item.get("page_number", 0)))
            try:
                page_number = int(page_value) + 1 if int(page_value) == 0 else int(page_value)
            except Exception:
                page_number = 1
            text = str(item.get("text") or item.get("content") or "").strip()
            if text:
                pages.setdefault(max(page_number, 1), []).append(text)
    return {page: "\n".join(parts) for page, parts in pages.items()}


def image_files(extract_dir: Path) -> list[Path]:
    return [path for path in sorted(extract_dir.rglob("*")) if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES]


def run_mineru_v4_parse(
    *,
    pdf_path: Path,
    workdir: Path,
    model_version: str = "vlm",
    is_ocr: bool = True,
    enable_formula: bool = True,
    enable_table: bool = True,
    language: str = "ch",
    page_ranges: str = "",
    no_cache: bool = False,
    timeout_seconds: int = 900,
    poll_interval_seconds: int = 5,
) -> dict[str, Any]:
    token = require_token()
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        batch = create_local_batch_task(
            pdf_path=pdf_path,
            token=token,
            model_version=model_version,
            is_ocr=is_ocr,
            enable_formula=enable_formula,
            enable_table=enable_table,
            language=language,
            page_ranges=page_ranges,
            no_cache=no_cache,
        )
    except Exception as exc:
        raise RuntimeError(f"MinerU create/upload phase failed: {exc}") from exc
    try:
        result = poll_batch_result(
            batch_id=batch["batch_id"],
            token=token,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )
    except Exception as exc:
        raise RuntimeError(f"MinerU poll phase failed for batch {batch['batch_id']}: {exc}") from exc
    zip_path = workdir / "mineru_result.zip"
    try:
        _download(str(result["full_zip_url"]), zip_path)
    except Exception as exc:
        raise RuntimeError(f"MinerU result download phase failed for batch {batch['batch_id']}: {exc}") from exc
    extract_dir = workdir / "mineru_result"
    unzip_result(zip_path, extract_dir)
    markdown = markdown_text_from_result(extract_dir)
    if not markdown.strip():
        raise RuntimeError("MinerU result did not contain usable full.md or content text.")
    pages = content_list_pages(extract_dir)
    return {
        "batch_id": batch["batch_id"],
        "data_id": batch["data_id"],
        "state": result.get("state", ""),
        "full_zip_url": result.get("full_zip_url", ""),
        "zip_path": str(zip_path),
        "extract_dir": str(extract_dir),
        "markdown": markdown,
        "pages": pages or {1: markdown},
        "model_version": model_version,
        "is_ocr": is_ocr,
        "enable_formula": enable_formula,
        "enable_table": enable_table,
        "language": language,
    }
