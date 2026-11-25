#!/usr/bin/env python3
"""Check every URL in an xLights vendor XML file.

Requests each URL with HEAD, then GET if HEAD is not 200. Redirects are
followed. A link passes only when the final response status is 200.

A gilbertengineeringusa.com product URL that redirects to a different
/products/<handle> fails even though the final status is 200, because the
handle in the file is no longer the current product. Link fields written
without a scheme (``host/path``) fail as well.

xmodellink values that point at this GEUSA/xmodels repository must also
name a file that exists in the working tree. That check is independent of
GitHub, so a path typo fails even when the remote file has not been fetched.

Usage:
    python3 tools/check_links.py
    python3 tools/check_links.py path/to/geusa-xlights.xml --json /tmp/report.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

USER_AGENT = "GEUSA-xlights-link-check/1.0"
RETRY_STATUSES = {429, 500, 502, 503, 504}
GITHUB_OWNERS = {"raw.githubusercontent.com", "github.com"}
LINK_TAGS = {"weblink", "imagefile", "xmodellink", "website", "facebook", "logolink"}
SCHEMELESS_URL = re.compile(r"^(?:[a-z0-9-]+\.)+[a-z]{2,}(?:[/:?#]|$)", re.IGNORECASE)


def repo_root_from_script() -> Path:
    return Path(__file__).resolve().parent.parent


def encode_url(url: str) -> str:
    """Percent-encode the path without double-encoding an already-escaped URL."""
    parts = urllib.parse.urlsplit(url.strip())
    path = urllib.parse.quote(urllib.parse.unquote(parts.path), safe="/")
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


def local_repo_path(url: str, repo_root: Path) -> Path | None:
    """Return the working-tree file for a GEUSA/xmodels GitHub URL, else None."""
    parts = urllib.parse.urlsplit(url.strip())
    if parts.netloc not in GITHUB_OWNERS:
        return None
    segments = [urllib.parse.unquote(s) for s in parts.path.split("/") if s]
    if len(segments) < 4 or segments[0] != "GEUSA" or segments[1] != "xmodels":
        return None
    if segments[2] in {"blob", "raw"}:
        rel_parts = segments[4:]
    elif segments[2] == "refs" and len(segments) >= 6 and segments[3] == "heads":
        rel_parts = segments[5:]
    else:
        rel_parts = segments[3:]
    if not rel_parts:
        return None
    return repo_root.joinpath(*rel_parts)


def product_handle(url: str) -> str | None:
    """Product handle from a store /products/<handle> URL, ignoring the query."""
    parts = urllib.parse.urlsplit(url.strip())
    host = parts.netloc.lower().split(":")[0]
    if host not in {"gilbertengineeringusa.com", "www.gilbertengineeringusa.com"}:
        return None
    segments = [urllib.parse.unquote(s) for s in parts.path.split("/") if s]
    if len(segments) >= 2 and segments[0] == "products":
        return segments[1]
    return None


def collect_urls(root: ET.Element) -> list[dict]:
    """Every link text node, with the model that owns it when there is one.

    Absolute http(s) URLs are checked remotely. A link tag whose text looks
    like a host/path but has no scheme is reported too, because xLights will
    not fetch it.
    """
    found: list[dict] = []

    def add(el: ET.Element, model_id: str, model_name: str) -> None:
        text = (el.text or "").strip()
        if not text:
            return
        absolute = text.startswith(("http://", "https://"))
        schemeless = el.tag in LINK_TAGS and SCHEMELESS_URL.match(text) is not None
        if not absolute and not schemeless:
            return
        found.append(
            {
                "url": text,
                "tag": el.tag,
                "id": model_id,
                "name": model_name,
            }
        )

    vendor = root.find("vendor")
    if vendor is not None:
        for el in vendor.iter():
            add(el, "", "")
    for model in root.findall("./models/model"):
        model_id = (model.findtext("id") or "").strip()
        model_name = (model.findtext("name") or "").strip()
        for el in model.iter():
            add(el, model_id, model_name)
    return found


def fetch_once(url: str, method: str, timeout: float) -> tuple[int | None, str]:
    encoded = encode_url(url)
    req = urllib.request.Request(
        encoded,
        method=method,
        headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if method == "GET":
                resp.read(128)
            return resp.status, resp.geturl()
    except urllib.error.HTTPError as exc:
        return exc.code, encoded
    except Exception as exc:  # noqa: BLE001 - reported as a failed link
        return None, f"{type(exc).__name__}: {exc}"


class _Cooldown:
    """Shared pause so a burst of 429s backs every worker off together."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._until = 0.0

    def wait(self) -> None:
        with self._lock:
            delay = self._until - time.monotonic()
        if delay > 0:
            time.sleep(delay)

    def trip(self, seconds: float) -> None:
        with self._lock:
            self._until = max(self._until, time.monotonic() + seconds)


_COOLDOWN = _Cooldown()


def fetch(url: str, timeout: float, retries: int) -> dict:
    """HEAD, then GET when HEAD is not 200. Retry transient failures."""
    last: dict = {"status": None, "method": "", "detail": "", "final_url": ""}
    for method in ("HEAD", "GET"):
        for attempt in range(retries + 1):
            _COOLDOWN.wait()
            status, detail = fetch_once(url, method, timeout)
            last = {
                "status": status,
                "method": method,
                "detail": detail,
                "final_url": detail if isinstance(status, int) else "",
            }
            if status == 200:
                return last
            if status in RETRY_STATUSES or status is None:
                if attempt < retries:
                    # 429 needs a longer pause than a one-off 5xx.
                    pause = min(20.0, (2.0 if status == 429 else 0.5) * (2**attempt))
                    if status == 429:
                        _COOLDOWN.trip(pause)
                    time.sleep(pause)
                    continue
            break
        if status == 200:
            return last
    return last


def check(xml_path: Path, repo_root: Path, workers: int, timeout: float, retries: int) -> dict:
    try:
        tree = ET.parse(xml_path)
    except ET.ParseError as exc:
        return {"ok": False, "parse_error": str(exc), "failures": [], "urls": 0, "refs": 0}
    refs = collect_urls(tree.getroot())
    by_url: dict[str, list[dict]] = {}
    for ref in refs:
        by_url.setdefault(ref["url"], []).append(
            {"id": ref["id"], "name": ref["name"], "tag": ref["tag"]}
        )

    local_results: dict[str, str] = {}
    for url, owners in by_url.items():
        if not any(owner["tag"] == "xmodellink" for owner in owners):
            local_results[url] = "n/a"
            continue
        path = local_repo_path(url, repo_root)
        if path is None:
            local_results[url] = "not-this-repo"
        elif path.is_file():
            local_results[url] = "ok"
        else:
            local_results[url] = "missing"

    http_results: dict[str, dict] = {}
    urls = list(by_url)
    lock = threading.Lock()
    done = 0

    def run_one(url: str) -> tuple[str, dict]:
        if not url.startswith(("http://", "https://")):
            return url, {
                "status": None,
                "method": "",
                "detail": "missing URL scheme",
                "final_url": "",
            }
        return url, fetch(url, timeout, retries)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(run_one, url) for url in urls]
        for fut in as_completed(futures):
            url, result = fut.result()
            http_results[url] = result
            with lock:
                done += 1
                if done % 100 == 0 or done == len(urls):
                    print(f"checked {done}/{len(urls)}", file=sys.stderr, flush=True)

    failures = []
    for url, owners in by_url.items():
        http = http_results[url]
        local = local_results[url]
        requested_handle = product_handle(url)
        final_handle = product_handle(http.get("final_url") or "")
        # A store redirect to a different /products/<handle> still returns 200,
        # but the link in the file is no longer the current product.
        stale_handle = bool(requested_handle and final_handle and requested_handle != final_handle)
        http_ok = http["status"] == 200 and not stale_handle
        local_ok = local in {"ok", "n/a", "not-this-repo"}
        if http_ok and local_ok:
            continue
        detail = http["detail"]
        if stale_handle:
            detail = f"redirects to /products/{final_handle}"
        failures.append(
            {
                "url": url,
                "status": http["status"],
                "method": http["method"],
                "detail": detail,
                "final_url": http.get("final_url") or "",
                "local": local,
                "refs": owners,
            }
        )
    failures.sort(key=lambda item: (item["refs"][0]["id"] or "0", item["url"]))
    return {
        "ok": not failures,
        "xml": str(xml_path),
        "refs": len(refs),
        "urls": len(urls),
        "failures": failures,
    }


def print_report(report: dict) -> None:
    if report.get("parse_error"):
        print(f"XML parse error: {report['parse_error']}")
        return
    print(f"Checked {report['urls']} unique URLs ({report['refs']} references)")
    print(f"Failures: {len(report['failures'])}")
    for item in report["failures"]:
        owners = ", ".join(
            f"{ref['id'] or '-'} {ref['name'] or '(vendor)'} [{ref['tag']}]"
            for ref in item["refs"][:8]
        )
        extra = ""
        if len(item["refs"]) > 8:
            extra = f" +{len(item['refs']) - 8} more"
        print(
            f"- HTTP {item['status']} via {item['method'] or '-'} "
            f"local={item['local']} {item['url']}"
        )
        if item.get("detail"):
            print(f"  {item['detail']}")
        print(f"  {owners}{extra}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "xml",
        nargs="?",
        default=None,
        help="Vendor XML file (default: geusa-xlights.xml in the repo root)",
    )
    parser.add_argument("--repo", default=None, help="Repository root used for xmodellink files")
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--timeout", type=float, default=25)
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument("--json", dest="json_path", default=None, help="Write the full report as JSON")
    args = parser.parse_args(argv)

    root = Path(args.repo).resolve() if args.repo else repo_root_from_script()
    xml_path = Path(args.xml).resolve() if args.xml else root / "geusa-xlights.xml"
    report = check(xml_path, root, args.workers, args.timeout, args.retries)
    print_report(report)
    if args.json_path:
        Path(args.json_path).write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
