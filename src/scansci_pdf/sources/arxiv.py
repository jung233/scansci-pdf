"""arXiv direct PDF download."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..identifiers import normalize_arxiv_id
from ..network import fetch
from ..pdf_utils import _response_looks_pdf, is_pdf_file, success


def try_arxiv(identifier: str, output_path: Path, config: dict[str, Any]) -> dict[str, Any] | None:
    arxiv_id = normalize_arxiv_id(identifier)
    if not arxiv_id:
        return None
    url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
    return download_arxiv_pdf(url, output_path, config)


def download_arxiv_pdf(url: str, output_path: Path, config: dict[str, Any]) -> dict[str, Any] | None:
    try:
        resp = fetch(url, config, stream=True)
        if resp.status_code >= 400:
            return None

        iterator = resp.iter_content(chunk_size=8192)
        first_chunk = next(iterator, b"")
        if not _response_looks_pdf(resp, first_chunk):
            return None

        output_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = output_path.with_suffix(output_path.suffix + ".part")
        try:
            with tmp_path.open("wb") as fh:
                fh.write(first_chunk)
                for chunk in iterator:
                    if chunk:
                        fh.write(chunk)
            tmp_path.replace(output_path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

        if is_pdf_file(output_path):
            return success(output_path.stem, output_path, "arXiv")
        else:
            try:
                output_path.unlink(missing_ok=True)
            except OSError:
                pass
    except Exception:
        return None
    return None
