"""Publisher routing table and direct download strategies.

Supports Nature direct download and MDPI open access.
Other publishers fall through to Crossref/Unpaywall/OpenAlex.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import requests

from .crossref import try_crossref
from .nature import try_nature_direct
from .openalex import try_openalex_oa
from .semantic_scholar import try_semanticscholar
from .unpaywall import try_unpaywall

# ============================================================
# Publisher URL templates: (name, pdf_template, match_patterns, gdpr, bibtex_template)
# {doi} = full DOI, {doi_suffix} = part after prefix, {base} = resolved URL base
# ============================================================
# Only publishers with working direct download (no subscription required)
PUBLISHERS: list[tuple[str, str | None, list[str], bool, str | None]] = [
    ("Nature",     "https://www.nature.com/articles/{doi_suffix}.pdf",
     ["nature.com", "10.1038/"], False, None),
    ("Frontiers",  None,
     ["frontiersin.org", "10.3389/"], False, None),
    ("BMC",        None,
     ["biomedcentral.com", "springeropen.com", "10.1186/"], False, None),
]

DOI_PREFIX_TO_PUBLISHER: dict[str, str] = {
    "10.1038/": "Nature",
    "10.1016/": "Elsevier",
    "10.1002/": "Wiley",
    "10.1126/": "Science",
    "10.1073/": "PNAS",
    "10.1093/": "Oxford",
    "10.1021/": "ACS",
    "10.1007/": "Springer",
    "10.1103/": "APS",
    "10.1088/": "IOP",
    "10.1101/": "bioRxiv",
    "10.3390/": "MDPI",
    "10.1371/": "PLOS",
    "10.3389/": "Frontiers",
    "10.1186/": "BMC",
    "10.15252/": "EMBO",
    "10.1039/": "RSC",
    "10.1063/": "AIP",
    "10.1111/": "Wiley",
    "10.1145/": "ACM",
    "10.1109/": "IEEE",
    "10.31219/": "Research Square",
    "10.21203/": "Research Square",
    "10.20944/": "Preprints.org",
    "10.26434/": "chemRxiv",
    "10.48550/": "arXiv",
}

PREPRINT_PREFIXES: dict[str, str] = {
    "10.1101/": "bioRxiv/medRxiv",
    "10.48550/": "arXiv",
    "10.31219/": "Research Square",
    "10.21203/": "Research Square",
    "10.26434/": "chemRxiv",
    "10.20944/": "Preprints.org",
}

# Fast sources per publisher (used by tiered racing)
# Only Nature has working direct download; others use Crossref/Unpaywall/OpenAlex
PUBLISHER_TOOL_MAP: dict[str, list[str]] = {
    "Nature": ["NatureDirect", "PublisherDirect", "Crossref", "Unpaywall"],
    "MDPI": ["MDPIDirect", "Crossref", "Unpaywall"],
    "arXiv": ["arXiv"],
    "bioRxiv": ["arXiv", "Unpaywall"],
    "Elsevier": ["Crossref", "Unpaywall"],
    "Wiley": ["Crossref", "Unpaywall"],
    "Science": ["ScienceDirect", "Crossref", "Unpaywall"],
    "PNAS": ["PNASDirect", "Crossref", "Unpaywall"],
    "Oxford": ["Crossref", "Unpaywall"],
    "ACS": ["Crossref", "Unpaywall"],
    "Springer": ["Crossref", "Unpaywall"],
    "APS": ["Crossref", "Unpaywall"],
    "IOP": ["Crossref", "Unpaywall"],
    "PLOS": ["PLOSDirect", "Crossref", "Unpaywall"],
    "Frontiers": ["FrontiersDirect", "Crossref", "Unpaywall"],
    "BMC": ["BMCDirect", "Crossref", "Unpaywall"],
    "RSC": ["Crossref", "Unpaywall"],
    "AIP": ["Crossref", "Unpaywall"],
    "ACM": ["Crossref", "Unpaywall"],
    "IEEE": ["Crossref", "Unpaywall"],
    "Research Square": ["Crossref", "Unpaywall"],
    "chemRxiv": ["Crossref", "Unpaywall"],
    "Preprints.org": ["Crossref", "Unpaywall"],
    "AnnualReviews": ["Crossref", "Unpaywall"],
    "WorldScientific": ["Crossref", "Unpaywall"],
}

_FN_MAP: dict[str, Any] = {}

from ..log import get_logger
log = get_logger()


def _write_pdf_atomic(output_path: Path, first: bytes, iterator: Any) -> bool:
    """Write PDF to .part file then atomically rename. Cleans up on failure."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_suffix(output_path.suffix + ".part")
    try:
        with tmp_path.open("wb") as fh:
            fh.write(first)
            for chunk in iterator:
                if chunk:
                    fh.write(chunk)
        tmp_path.replace(output_path)
        return True
    except Exception:
        tmp_path.unlink(missing_ok=True)
        return False


# ============================================================
# Publisher matching
# ============================================================

def _match_publisher(doi_or_url: str, patterns: list[str]) -> bool:
    lower = doi_or_url.lower()
    return any(p.lower() in lower for p in patterns)


def _format_pdf_url(template: str, doi: str, url: str) -> str | None:
    if not template:
        return None
    doi_suffix = doi.split("/", 1)[1] if "/" in doi else doi
    base = ""
    if url:
        m = re.match(r"(https?://[^/]+)", url)
        if m:
            base = m.group(1)
    return (template
            .replace("{doi}", doi)
            .replace("{doi_suffix}", doi_suffix)
            .replace("{base}", base))


# ============================================================
# DOI resolution
# ============================================================

def resolve_doi(doi: str, config: dict[str, Any]) -> str | None:
    """Resolve DOI to publisher URL via doi.org redirect."""
    from ..network import USER_AGENT
    try:
        s = requests.Session()
        s.trust_env = False
        resp = s.head(f"https://doi.org/{doi}", allow_redirects=True,
                      timeout=10, headers={"User-Agent": USER_AGENT})
        resolved = resp.url.lower()
        known_patterns = []
        for pub in PUBLISHERS:
            known_patterns.extend(pub[2])
        if any(p.lower() in resolved for p in known_patterns):
            return resp.url
    except Exception:
        pass
    return None


# ============================================================
# Publisher direct download (with GDPR cookie bypass)
# ============================================================

def try_publisher_direct(doi: str, output_path: Path, config: dict[str, Any]) -> dict[str, Any] | None:
    """Try publisher direct download (Nature only, others fall through to OA sources)."""
    from ..network import USER_AGENT, polite_delay
    from ..pdf_utils import is_pdf_file, success, _response_looks_pdf

    log.info(f"   [PublisherDirect] Starting for {doi}")
    resolved_url = resolve_doi(doi, config)
    log.info(f"   [PublisherDirect] Resolved URL: {resolved_url}")

    for pub in PUBLISHERS:
        name, template, matches, gdpr, _ = pub

        if not _match_publisher(doi, matches) and not _match_publisher(resolved_url or "", matches):
            continue

        pdf_url = _format_pdf_url(template or "", doi, resolved_url or "")
        if not pdf_url:
            continue

        log.info(f"   [Publisher] {name}: {pdf_url[:80]}...")
        polite_delay(config)

        try:
            s = requests.Session()
            s.trust_env = False
            s.headers.update({"User-Agent": USER_AGENT})
            resp = s.get(pdf_url, timeout=15, stream=True,
                         headers={"Accept": "application/pdf,*/*"},
                         allow_redirects=True)

            if resp.status_code >= 400:
                log.info(f"   [Publisher] {name}: HTTP {resp.status_code}")
                continue

            iterator = resp.iter_content(chunk_size=8192)
            first = next(iterator, b"")
            if not _response_looks_pdf(resp, first):
                continue

            if not _write_pdf_atomic(output_path, first, iterator):
                continue
            if is_pdf_file(output_path):
                return success(doi, output_path, f"Publisher({name})")

        except Exception as e:
            log.info(f"   [Publisher] {name}: {e}")

    return None


# ============================================================
# MDPI direct download
# ============================================================

def try_mdpi_direct(doi: str, output_path: Path, config: dict[str, Any]) -> dict[str, Any] | None:
    """Download MDPI open access papers directly."""
    if not doi.startswith("10.3390/"):
        return None

    from ..pdf_utils import is_pdf_file, success, _response_looks_pdf
    from ..network import USER_AGENT, polite_delay

    article_id = doi.split("10.3390/")[-1]

    urls = [
        f"https://www.mdpi.com/{article_id}/pdf",
    ]

    try:
        session = requests.Session()
        session.trust_env = False
        session.headers.update({"User-Agent": USER_AGENT})
        polite_delay(config)

        # First try landing page to get real PDF URL
        try:
            resp = session.get(f"https://www.mdpi.com/{article_id}", timeout=10, allow_redirects=True)
            if resp.status_code == 200:
                pdf_match = re.search(r'citation_pdf_url["\s]+content="([^"]+)"', resp.text[:5000], re.I)
                if not pdf_match:
                    pdf_match = re.search(r'href="(/[^"]*?/pdf[^"]*)"', resp.text[:10000], re.I)
                if pdf_match:
                    pdf_url = pdf_match.group(1)
                    if pdf_url.startswith("/"):
                        pdf_url = f"https://www.mdpi.com{pdf_url}"
                    urls.insert(0, pdf_url)
        except Exception:
            pass

        for pdf_url in urls:
            try:
                resp2 = session.get(pdf_url, timeout=15, stream=True,
                                    headers={"Accept": "application/pdf,*/*"})
                if resp2.status_code >= 400:
                    continue

                iterator = resp2.iter_content(chunk_size=8192)
                first = next(iterator, b"")
                if not _response_looks_pdf(resp2, first):
                    continue

                if not _write_pdf_atomic(output_path, first, iterator):
                    continue
                if is_pdf_file(output_path):
                    return success(doi, output_path, "MDPIDirect")
            except Exception:
                continue
    except Exception:
        pass
    return None


# ============================================================
# Science/AAAS direct download (Science Advances, Science, etc.)
# ============================================================

def try_science_direct(doi: str, output_path: Path, config: dict[str, Any]) -> dict[str, Any] | None:
    """Download Science/AAAS papers directly. Science Advances is OA."""
    if not doi.startswith("10.1126/"):
        return None

    from ..network import polite_delay
    from ..pdf_utils import is_pdf_file, success, _response_looks_pdf

    try:
        from curl_cffi import requests as cffi_requests
    except ImportError:
        return None

    # Extract article ID from DOI (e.g., sciadv.1600983 -> 1600983)
    doi_suffix = doi.split("10.1126/")[-1]

    # Try Science Advances PDF URL patterns
    pdf_urls = []
    if "sciadv" in doi_suffix:
        # Science Advances: https://advances.sciencemag.org/content/3/11/e1600983.full.pdf
        article_id = doi_suffix.split(".")[-1] if "." in doi_suffix else doi_suffix
        pdf_urls.append(f"https://advances.sciencemag.org/content/3/11/{article_id}.full.pdf")
        pdf_urls.append(f"https://advances.sciencemag.org/content/early/{article_id}.full.pdf")
    elif "science" in doi_suffix.lower():
        # Science magazine
        pdf_urls.append(f"https://www.science.org/doi/pdf/{doi}")

    for pdf_url in pdf_urls:
        try:
            polite_delay(config)
            resp = cffi_requests.get(pdf_url, impersonate="chrome", timeout=15, stream=True)

            if resp.status_code >= 400:
                continue

            iterator = resp.iter_content(chunk_size=8192)
            first = next(iterator, b"")
            if not _response_looks_pdf(resp, first):
                continue

            if not _write_pdf_atomic(output_path, first, iterator):
                continue
            if is_pdf_file(output_path):
                return success(doi, output_path, "ScienceDirect")
        except Exception:
            continue
    return None


# ============================================================
# PNAS direct download
# ============================================================

def try_pnas_direct(doi: str, output_path: Path, config: dict[str, Any]) -> dict[str, Any] | None:
    """Download PNAS papers directly. PNAS is OA after 6 months."""
    if not doi.startswith("10.1073/"):
        return None

    from ..network import USER_AGENT, polite_delay
    from ..pdf_utils import is_pdf_file, success, _response_looks_pdf

    doi_suffix = doi.split("10.1073/")[-1]
    pdf_url = f"https://www.pnas.org/doi/epdf/{doi}"

    try:
        polite_delay(config)
        s = requests.Session()
        s.trust_env = False
        s.headers.update({"User-Agent": USER_AGENT})
        resp = s.get(pdf_url, timeout=15, stream=True,
                     headers={"Accept": "application/pdf,*/*"},
                     allow_redirects=True)

        if resp.status_code >= 400:
            return None

        iterator = resp.iter_content(chunk_size=8192)
        first = next(iterator, b"")
        if not _response_looks_pdf(resp, first):
            return None

        if not _write_pdf_atomic(output_path, first, iterator):
            return None
        if is_pdf_file(output_path):
            return success(doi, output_path, "PNASDirect")
    except Exception:
        pass
    return None


# ============================================================
# PLOS direct download
# ============================================================

def try_plos_direct(doi: str, output_path: Path, config: dict[str, Any]) -> dict[str, Any] | None:
    """Download PLOS papers directly. All PLOS journals are OA."""
    if not doi.startswith("10.1371/"):
        return None

    from ..network import USER_AGENT, polite_delay
    from ..pdf_utils import is_pdf_file, success, _response_looks_pdf

    # PLOS PDF URL: https://journals.plos.org/{journal}/article/file?id={doi}&type=printable
    # Determine journal from DOI: 10.1371/journal.pone.0123456 -> plosone
    doi_suffix = doi.split("10.1371/")[-1] if "10.1371/" in doi else doi
    journal_map = {
        "journal.pone": "plosone",
        "journal.pbio": "plosbiology",
        "journal.pmed": "plosmedicine",
        "journal.pgen": "plosgenetics",
        "journal.ppat": "plospathogens",
        "journal.pcbi": "ploscompbiol",
        "journal.pntd": "plosntds",
        "journal.pone": "plosone",
        "journal.pclm": "plosclimate",
        "journal.pdig": "plosdigitalhealth",
    }
    journal = "plosone"  # default
    for key, val in journal_map.items():
        if doi_suffix.startswith(key):
            journal = val
            break
    pdf_url = f"https://journals.plos.org/{journal}/article/file?id={doi}&type=printable"

    try:
        polite_delay(config)
        s = requests.Session()
        s.trust_env = False
        s.headers.update({"User-Agent": USER_AGENT})
        resp = s.get(pdf_url, timeout=15, stream=True,
                     headers={"Accept": "application/pdf,*/*"},
                     allow_redirects=True)

        if resp.status_code >= 400:
            return None

        iterator = resp.iter_content(chunk_size=8192)
        first = next(iterator, b"")
        if not _response_looks_pdf(resp, first):
            return None

        if not _write_pdf_atomic(output_path, first, iterator):
            return None
        if is_pdf_file(output_path):
            return success(doi, output_path, "PLOSDirect")
    except Exception:
        pass
    return None


# ============================================================
# Frontiers direct download
# ============================================================

def try_frontiers_direct(doi: str, output_path: Path, config: dict[str, Any]) -> dict[str, Any] | None:
    """Download Frontiers papers directly. All Frontiers journals are OA."""
    if not doi.startswith("10.3389/"):
        return None

    from ..network import USER_AGENT, polite_delay
    from ..pdf_utils import is_pdf_file, success, _response_looks_pdf

    # Frontiers PDF URL: https://www.frontiersin.org/articles/{doi}/pdf
    pdf_url = f"https://www.frontiersin.org/articles/{doi}/pdf"

    try:
        polite_delay(config)
        s = requests.Session()
        s.trust_env = False
        s.headers.update({"User-Agent": USER_AGENT})
        resp = s.get(pdf_url, timeout=15, stream=True,
                     headers={"Accept": "application/pdf,*/*"},
                     allow_redirects=True)

        if resp.status_code >= 400:
            return None

        iterator = resp.iter_content(chunk_size=8192)
        first = next(iterator, b"")
        if not _response_looks_pdf(resp, first):
            return None

        if not _write_pdf_atomic(output_path, first, iterator):
            return None
        if is_pdf_file(output_path):
            return success(doi, output_path, "FrontiersDirect")
    except Exception:
        pass
    return None


# ============================================================
# BMC/SpringerOpen direct download
# ============================================================

def try_bmc_direct(doi: str, output_path: Path, config: dict[str, Any]) -> dict[str, Any] | None:
    """Download BMC/SpringerOpen papers directly. All BMC journals are OA."""
    if not doi.startswith("10.1186/"):
        return None

    from ..network import USER_AGENT, polite_delay
    from ..pdf_utils import is_pdf_file, success, _response_looks_pdf

    # BMC/SpringerOpen PDF: try Springer link first, then direct biomedcentral
    pdf_urls = [
        f"https://link.springer.com/content/pdf/{doi}.pdf",
        f"https://link.springer.com/content/pdf/{doi}.pdf?pdf=button%20sticky",
    ]

    for pdf_url in pdf_urls:
        try:
            polite_delay(config)
            s = requests.Session()
            s.trust_env = False
            s.headers.update({"User-Agent": USER_AGENT})
            resp = s.get(pdf_url, timeout=15, stream=True,
                         headers={"Accept": "application/pdf,*/*"},
                         allow_redirects=True)

            if resp.status_code >= 400:
                continue

            iterator = resp.iter_content(chunk_size=8192)
            first = next(iterator, b"")
            if not _response_looks_pdf(resp, first):
                continue

            if not _write_pdf_atomic(output_path, first, iterator):
                continue
            if is_pdf_file(output_path):
                return success(doi, output_path, "BMCDirect")
        except Exception:
            continue
    return None


# ============================================================
# Helpers
# ============================================================

def get_publisher(doi: str) -> str:
    for prefix, publisher in DOI_PREFIX_TO_PUBLISHER.items():
        if doi.startswith(prefix):
            return publisher
    return ""


def get_publisher_fast_sources(doi: str) -> list[tuple[Any, str]]:
    publisher = get_publisher(doi)
    if not publisher:
        return []
    tools = PUBLISHER_TOOL_MAP.get(publisher, ["Crossref", "Unpaywall"])
    sources = []
    for tool in tools:
        fn = _FN_MAP.get(tool)
        if fn:
            sources.append((fn, tool))
    return sources


def is_preprint(doi: str) -> bool:
    for prefix in PREPRINT_PREFIXES:
        if doi.startswith(prefix):
            return True
    return False


# Populate function map
_FN_MAP.update({
    "NatureDirect": try_nature_direct,
    "PublisherDirect": try_publisher_direct,
    "Crossref": try_crossref,
    "Unpaywall": try_unpaywall,
    "OpenAlexOA": try_openalex_oa,
    "SemanticScholar": try_semanticscholar,
    "MDPIDirect": try_mdpi_direct,
    "ScienceDirect": try_science_direct,
    "PNASDirect": try_pnas_direct,
    "PLOSDirect": try_plos_direct,
    "FrontiersDirect": try_frontiers_direct,
    "BMCDirect": try_bmc_direct,
})
