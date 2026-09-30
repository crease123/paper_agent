#!/usr/bin/env python3
"""Download a document and convert it with MinerU's authenticated precise API."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path, PurePosixPath


API_ROOT = "https://mineru.net/api/v4"
DEFAULT_TOKEN_FILE = Path("~/.config/mineru/token").expanduser()
USER_AGENT = "mineru-fetch-markdown/1.0"
MAX_SOURCE_BYTES = 210 * 1024 * 1024
MAX_RESULT_ZIP_BYTES = 1024 * 1024 * 1024
MAX_EXTRACTED_BYTES = 5 * 1024 * 1024 * 1024


class MinerUError(RuntimeError):
    pass


def load_token(token_file: Path) -> str:
    token = os.environ.get("MINERU_API_TOKEN", "").strip()
    if token:
        return token
    if not token_file.is_file():
        raise MinerUError(
            "MinerU Token not found. Set MINERU_API_TOKEN or create "
            f"{token_file} with mode 600."
        )
    mode = stat.S_IMODE(token_file.stat().st_mode)
    if mode & 0o077:
        raise MinerUError(
            f"Token file permissions are too open ({mode:o}); run: chmod 600 {token_file}"
        )
    token = token_file.read_text(encoding="utf-8").strip()
    if not token or "\n" in token:
        raise MinerUError(f"Token file must contain one non-empty line: {token_file}")
    return token


def request_json(
    url: str,
    *,
    method: str = "GET",
    token: str | None = None,
    payload: dict | None = None,
    timeout: float = 60,
) -> dict:
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:2000]
        raise MinerUError(f"MinerU HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise MinerUError(f"MinerU request failed: {exc}") from exc
    if not isinstance(result, dict):
        raise MinerUError("MinerU returned a non-object JSON response")
    if result.get("code") not in (None, 0):
        trace = result.get("trace_id", "unknown")
        raise MinerUError(
            f"MinerU API error {result.get('code')}: {result.get('msg', 'unknown')} "
            f"(trace_id={trace})"
        )
    return result


def download(
    url: str,
    destination: Path,
    *,
    timeout: float = 120,
    max_bytes: int = MAX_SOURCE_BYTES,
) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response, destination.open("wb") as out:
            length = response.headers.get("Content-Length")
            if length and int(length) > max_bytes:
                raise MinerUError("Download exceeds the configured size limit")
            total = 0
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise MinerUError("Download exceeds the configured size limit")
                out.write(chunk)
    except (urllib.error.URLError, TimeoutError) as exc:
        raise MinerUError(f"Download failed for {url}: {exc}") from exc


def source_name(source: str, override: str | None) -> str:
    if override:
        name = Path(override).name
    elif urllib.parse.urlparse(source).scheme in {"http", "https"}:
        name = Path(urllib.parse.unquote(urllib.parse.urlparse(source).path)).name
    else:
        name = Path(source).name
    if not name or "." not in name:
        raise MinerUError("Cannot infer file type; provide --filename with an extension")
    return name


def prepare_source(source: str, staging: Path, filename: str | None) -> Path:
    name = source_name(source, filename)
    source_dir = staging / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    destination = source_dir / name
    scheme = urllib.parse.urlparse(source).scheme
    if scheme in {"http", "https"}:
        print(f"Downloading source: {source}")
        download(source, destination)
    else:
        local = Path(source).expanduser().resolve()
        if not local.is_file():
            raise MinerUError(f"Local source does not exist: {local}")
        if local.stat().st_size > MAX_SOURCE_BYTES:
            raise MinerUError("Source exceeds MinerU's 200 MB file limit")
        if local != destination.resolve():
            shutil.copy2(local, destination)
    return destination


def upload_file(url: str, path: Path, *, timeout: float = 300) -> None:
    data = path.read_bytes()
    request = urllib.request.Request(
        url,
        data=data,
        # MinerU's OSS upload signature is generated without a content type.
        # An explicit empty value also prevents urllib from adding its default.
        headers={"Content-Type": "", "User-Agent": USER_AGENT},
        method="PUT",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if not 200 <= response.status < 300:
                raise MinerUError(f"Signed upload failed with HTTP {response.status}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        raise MinerUError(f"Signed upload failed with HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise MinerUError(f"Signed upload failed: {exc}") from exc


def create_batch(args: argparse.Namespace, token: str, source: Path) -> tuple[str, str]:
    data_id = str(uuid.uuid4())
    payload: dict = {
        "files": [{"name": source.name, "data_id": data_id}],
        "model_version": args.model,
        "language": args.language,
        "enable_formula": not args.disable_formula,
        "enable_table": not args.disable_table,
    }
    if args.extra_format:
        payload["extra_formats"] = args.extra_format
    result = request_json(
        f"{API_ROOT}/file-urls/batch",
        method="POST",
        token=token,
        payload=payload,
    )
    data = result.get("data") or {}
    urls = data.get("file_urls") or []
    if not data.get("batch_id") or len(urls) != 1:
        raise MinerUError("MinerU did not return one signed upload URL")
    upload_file(urls[0], source)
    return str(data["batch_id"]), data_id


def poll_batch(args: argparse.Namespace, token: str, batch_id: str, data_id: str) -> str:
    deadline = time.monotonic() + args.timeout
    last_status = None
    while time.monotonic() < deadline:
        result = request_json(
            f"{API_ROOT}/extract-results/batch/{batch_id}", token=token
        )
        items = (result.get("data") or {}).get("extract_result") or []
        item = next((entry for entry in items if entry.get("data_id") == data_id), None)
        if item is None and len(items) == 1:
            item = items[0]
        if item is None:
            status = "waiting-file"
        else:
            status = str(item.get("state", "pending"))
            progress = item.get("extract_progress") or {}
            if progress.get("total_pages"):
                status += f" {progress.get('extracted_pages', 0)}/{progress['total_pages']} pages"
        if status != last_status:
            print(f"MinerU status: {status}")
            last_status = status
        base_status = status.split()[0]
        if base_status == "done":
            zip_url = item.get("full_zip_url")
            if not zip_url:
                raise MinerUError("MinerU finished without full_zip_url")
            return str(zip_url)
        if base_status == "failed":
            raise MinerUError(f"MinerU parsing failed: {item.get('err_msg', 'unknown')}")
        time.sleep(args.poll_interval)
    raise MinerUError(f"Timed out waiting for MinerU batch_id={batch_id}")


def safe_extract(zip_path: Path, output: Path) -> None:
    with zipfile.ZipFile(zip_path) as archive:
        total = sum(info.file_size for info in archive.infolist())
        if total > MAX_EXTRACTED_BYTES:
            raise MinerUError("MinerU result archive is unexpectedly large")
        output_root = output.resolve()
        for info in archive.infolist():
            relative = PurePosixPath(info.filename)
            if relative.is_absolute() or ".." in relative.parts:
                raise MinerUError(f"Unsafe path in MinerU ZIP: {info.filename}")
            destination = (output / Path(*relative.parts)).resolve()
            if output_root not in destination.parents and destination != output_root:
                raise MinerUError(f"Unsafe path in MinerU ZIP: {info.filename}")
        archive.extractall(output)


def flatten_single_directory(output: Path) -> None:
    entries = list(output.iterdir())
    if len(entries) != 1 or not entries[0].is_dir():
        return
    wrapper = entries[0]
    for child in list(wrapper.iterdir()):
        destination = output / child.name
        if destination.exists():
            return
        child.replace(destination)
    wrapper.rmdir()


def find_markdown(output: Path) -> Path:
    candidates = sorted(output.rglob("full.md"))
    if not candidates:
        candidates = sorted(path for path in output.rglob("*.md") if path.is_file())
    if not candidates:
        raise MinerUError("MinerU result does not contain Markdown")
    return candidates[0]


def missing_image_references(markdown: Path) -> list[str]:
    text = markdown.read_text(encoding="utf-8", errors="replace")
    targets = re.findall(r"!\[[^]]*\]\(([^)]+)\)", text)
    targets += re.findall(r"<img\s+[^>]*src=[\"']([^\"']+)", text, flags=re.I)
    missing = []
    for target in targets:
        target = target.strip().split()[0].strip("<>\"'")
        if urllib.parse.urlparse(target).scheme or target.startswith("data:"):
            continue
        clean = urllib.parse.unquote(target.split("#", 1)[0].split("?", 1)[0])
        if clean and not (markdown.parent / clean).is_file():
            missing.append(target)
    return sorted(set(missing))


def write_final_result(parsed: Path, output: Path, source: Path) -> Path:
    """Copy only the named Markdown and local images to the final directory."""
    markdown = find_markdown(parsed)
    output.mkdir(parents=True, exist_ok=True)
    named_markdown = output / f"{source.stem}.md"
    shutil.copy2(markdown, named_markdown)

    image_sources = [path for path in parsed.rglob("images") if path.is_dir()]
    image_source = next(
        (path for path in image_sources if any(path.iterdir())),
        image_sources[0] if image_sources else None,
    )
    image_output = output / "images"
    if image_output.exists():
        shutil.rmtree(image_output)
    image_output.mkdir(parents=True, exist_ok=True)
    if image_source is not None:
        for item in image_source.iterdir():
            destination = image_output / item.name
            if item.is_dir():
                shutil.copytree(item, destination)
            else:
                shutil.copy2(item, destination)

    missing = missing_image_references(named_markdown)
    if missing:
        raise MinerUError(
            "Final Markdown has missing local image references: " + ", ".join(missing[:10])
        )
    return named_markdown


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download/upload a document and save MinerU Markdown with local images."
    )
    parser.add_argument("source", help="HTTP(S) URL or local document path")
    parser.add_argument("--output", required=True, type=Path, help="output directory")
    parser.add_argument("--filename", help="override downloaded filename, including extension")
    parser.add_argument("--model", choices=["vlm", "pipeline"], default="vlm")
    parser.add_argument("--language", default="en")
    parser.add_argument("--disable-formula", action="store_true")
    parser.add_argument("--disable-table", action="store_true")
    parser.add_argument(
        "--extra-format", action="append", choices=["docx", "html", "latex"], default=[]
    )
    parser.add_argument("--token-file", type=Path, default=DEFAULT_TOKEN_FILE)
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument("--timeout", type=float, default=1800.0)
    parser.add_argument("--overwrite", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output = args.output.expanduser().resolve()
    try:
        token = load_token(args.token_file.expanduser())
        if output.exists() and any(output.iterdir()) and not args.overwrite:
            raise MinerUError(f"Output directory is not empty: {output}; use --overwrite")
        output.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="mineru-fetch-") as staging_dir:
            staging = Path(staging_dir)
            source = prepare_source(args.source, staging, args.filename)
            print(f"Submitting to MinerU ({args.model})")
            batch_id, data_id = create_batch(args, token, source)
            print(f"MinerU batch_id: {batch_id}")
            zip_url = poll_batch(args, token, batch_id, data_id)
            parsed = staging / "parsed"
            parsed.mkdir()
            archive = staging / "result.zip"
            download(
                zip_url,
                archive,
                timeout=300,
                max_bytes=MAX_RESULT_ZIP_BYTES,
            )
            safe_extract(archive, parsed)
            flatten_single_directory(parsed)
            named_markdown = write_final_result(parsed, output, source)
        print(f"Markdown: {named_markdown}")
        print(f"Images: {output / 'images'}")
        print("Image references: OK")
        return 0
    except MinerUError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
