#!/usr/bin/env python3
"""Fail-closed live validation for job leads and candidate lead pools."""

from __future__ import annotations

import argparse
import html
import http.client
import ipaddress
import json
import re
import socket
import ssl
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urljoin, urlparse, urlunparse


USER_AGENT = "Mozilla/5.0 (compatible; CodexJobLeadVerifier/1.0)"
FAIL_MARKERS = (
    "job not found",
    "position not found",
    "job is no longer available",
    "job is no longer accepting applications",
    "no longer accepting applications",
    "position has been filled",
    "this job has closed",
    "job posting has expired",
)
APPLY_MARKERS = (
    "apply for this job",
    "apply for this position",
    "apply now",
    "submit application",
    "/application",
)
STRICT_FINAL = "strict-final"
CANDIDATE_POOL = "candidate-pool"


class VerificationError(RuntimeError):
    """A classified failure to prove that a lead is currently deliverable."""

    def __init__(self, message: str, *, reason_code: str = "ambiguous", definitive: bool = False):
        super().__init__(message)
        self.reason_code = reason_code
        self.definitive = definitive


def _error(message: str, *, reason_code: str = "ambiguous", definitive: bool = False) -> VerificationError:
    return VerificationError(message, reason_code=reason_code, definitive=definitive)


def _normalize(value: str) -> str:
    value = html.unescape(value).lower()
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def _contains_expected(haystack: str, expected: str) -> bool:
    wanted = _normalize(expected)
    actual = _normalize(haystack)
    return bool(wanted) and wanted in actual


def _same_origin(first: str, second: str) -> bool:
    first_parsed = urlparse(first)
    second_parsed = urlparse(second)
    return (
        first_parsed.scheme.lower() == second_parsed.scheme.lower() == "https"
        and (first_parsed.hostname or "").lower() == (second_parsed.hostname or "").lower()
        and (first_parsed.port or 443) == (second_parsed.port or 443)
    )


def _public_target(url: str) -> tuple[object, tuple[int, int, int, tuple]]:
    """Resolve and pin a public HTTPS address; never trust a later DNS lookup."""
    parsed = urlparse(url)
    try:
        port = parsed.port or 443
    except ValueError as exc:
        raise _error("invalid HTTPS port", reason_code="invalid_url") from exc
    if (parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username
            or parsed.password or port != 443):
        raise _error("job link must be public HTTPS on port 443", reason_code="invalid_url")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
            raise _error("job link resolves to a non-public address", reason_code="invalid_url")
    except (OSError, ValueError) as exc:
        raise _error("could not resolve a public job-link address", reason_code="request_failed") from exc
    return parsed, addresses[0]


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, hostname: str, address: tuple[int, int, int, tuple]):
        super().__init__(hostname, port=443, timeout=20, context=ssl.create_default_context())
        self._pinned_address = address

    def connect(self) -> None:
        family, socktype, protocol, sockaddr = self._pinned_address
        sock = socket.socket(family, socktype, protocol)
        try:
            sock.settimeout(self.timeout)
            sock.connect(sockaddr)
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


def _request_public_once(url: str, *, expect_json: bool) -> tuple[int, str | None, str]:
    parsed, address = _public_target(url)
    connection = _PinnedHTTPSConnection(parsed.hostname, address)
    target = urlunparse(("", "", parsed.path or "/", parsed.params, parsed.query, ""))
    try:
        connection.request("GET", target, headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json" if expect_json else "text/html,application/xhtml+xml",
        })
        response = connection.getresponse()
        if response.status in {301, 302, 303, 307, 308}:
            return response.status, response.getheader("Location"), ""
        raw = response.read(8_000_001)
        if len(raw) > 8_000_000:
            raise _error("job page exceeds the response limit", reason_code="request_failed")
        charset = response.headers.get_content_charset() or "utf-8"
        return response.status, None, raw.decode(charset, errors="replace")
    finally:
        connection.close()


def _fetch(url: str, *, expect_json: bool = False) -> dict[str, object]:
    """Fetch with DNS/IP validation at every hop, including redirects."""
    current = url
    try:
        for _ in range(6):
            status, location, body = _request_public_once(current, expect_json=expect_json)
            if status in {301, 302, 303, 307, 308}:
                if not location:
                    raise _error("redirect without Location", reason_code="request_failed")
                current = urljoin(current, location)
                continue
            if status in {404, 410}:
                raise _error(f"HTTP {status}", reason_code="unavailable", definitive=True)
            return {"status": status, "final_url": current, "text": body}
        raise _error("too many job-link redirects", reason_code="request_failed")
    except (TimeoutError, OSError, ssl.SSLError) as exc:
        raise _error(f"request failed: {exc}", reason_code="request_failed") from exc


def _specific_detail_url(original: str, final: str) -> bool:
    original_parts = [part for part in urlparse(original).path.split("/") if part]
    final_parsed = urlparse(final)
    final_parts = [part for part in final_parsed.path.split("/") if part]
    final_query = parse_qs(final_parsed.query)
    if len(final_parts) >= 2 or any(
        key.lower() in {"job", "jobid", "gh_jid", "id"} for key in final_query
    ):
        return True
    return len(original_parts) <= len(final_parts) and len(final_parts) >= 2


def _valid_json(response: dict[str, object], source_name: str) -> object:
    try:
        return json.loads(str(response["text"]))
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise _error(f"{source_name} did not return valid JSON", reason_code="malformed_response") from exc


def _validate_ashby(lead: dict[str, object], fetch: Callable[..., dict[str, object]]) -> dict[str, object]:
    link = str(lead["link"])
    parts = [part for part in urlparse(link).path.split("/") if part]
    if len(parts) < 2:
        raise _error("Ashby URL is not an exact job-detail URL", reason_code="invalid_url")
    board, job_id = parts[0], parts[1]
    api_url = f"https://api.ashbyhq.com/posting-api/job-board/{board}"
    response = fetch(api_url, expect_json=True)
    if response["status"] != 200:
        raise _error(f"Ashby board API returned HTTP {response['status']}", reason_code="request_failed")
    payload = _valid_json(response, "Ashby board API")
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    if not isinstance(jobs, list):
        raise _error("Ashby board API omitted its jobs list", reason_code="malformed_response")
    job = next((item for item in jobs if isinstance(item, dict) and item.get("id") == job_id), None)
    if job is None:
        raise _error(
            "exact Ashby job is not listed on the current employer board",
            reason_code="unavailable",
            definitive=True,
        )
    if job.get("isListed") is not True:
        raise _error("Ashby job is not currently listed", reason_code="unavailable", definitive=True)
    if not _contains_expected(str(job.get("title", "")), str(lead["role"])):
        raise _error("Ashby job title does not match the lead", reason_code="role_mismatch", definitive=True)
    apply_url = str(job.get("applyUrl", ""))
    if not apply_url.startswith("https://"):
        raise _error("Ashby job has no active HTTPS application URL", reason_code="no_apply_action")
    return {
        "method": "ashby-public-board-api",
        "final_url": str(job.get("jobUrl") or link),
        "http_status": int(response["status"]),
        "role_match": True,
        "company_or_board_match": True,
        "apply_action": apply_url,
    }


def _workday_route(link: str) -> tuple[str, str, str, str]:
    parsed = urlparse(link)
    parts = [part for part in parsed.path.split("/") if part]
    if "job" not in parts:
        raise _error("Workday URL is not an exact job-detail URL", reason_code="invalid_url")
    job_index = parts.index("job")
    if job_index < 1 or len(parts) < job_index + 3:
        raise _error("Workday URL is not an exact job-detail URL", reason_code="invalid_url")
    board = parts[job_index - 1]
    job_parts = parts[job_index + 1 :]
    job_posting_id = job_parts[-1]
    tenant = (parsed.hostname or "").split(".", maxsplit=1)[0]
    if not tenant or not board or not job_posting_id:
        raise _error("Workday URL has an invalid tenant or job id", reason_code="invalid_url")
    cxs_path = "/" + "/".join(["wday", "cxs", tenant, board, "job", *job_parts])
    cxs_url = urlunparse(("https", parsed.netloc, cxs_path, "", "", ""))
    return cxs_url, board, job_posting_id, "/".join(job_parts)


def _validate_workday(lead: dict[str, object], fetch: Callable[..., dict[str, object]]) -> dict[str, object]:
    link = str(lead["link"])
    cxs_url, board, job_posting_id, job_path = _workday_route(link)
    response = fetch(cxs_url, expect_json=True)
    if int(response["status"]) != 200:
        raise _error(f"Workday CXS returned HTTP {response['status']}", reason_code="request_failed")
    final_url = str(response.get("final_url", ""))
    if not _same_origin(cxs_url, final_url) or urlparse(final_url).path != urlparse(cxs_url).path:
        raise _error("Workday CXS request redirected away from the exact job endpoint")
    payload = _valid_json(response, "Workday CXS")
    info = payload.get("jobPostingInfo") if isinstance(payload, dict) else None
    if not isinstance(info, dict):
        raise _error("Workday CXS omitted jobPostingInfo", reason_code="malformed_response")
    if info.get("posted") is not True or info.get("canApply") is not True:
        raise _error(
            "Workday job is not posted and open for applications",
            reason_code="unavailable",
            definitive=True,
        )
    if str(info.get("jobPostingId", "")) != job_posting_id:
        raise _error(
            "Workday job id does not match the exact detail URL",
            reason_code="job_id_mismatch",
            definitive=True,
        )
    if str(info.get("jobPostingSiteId", "")) != board:
        raise _error(
            "Workday board does not match the exact detail URL",
            reason_code="board_mismatch",
            definitive=True,
        )
    if not _contains_expected(str(info.get("title", "")), str(lead["role"])):
        raise _error("Workday job title does not match the lead", reason_code="role_mismatch", definitive=True)
    board_identity = " ".join([(urlparse(link).hostname or "").split(".")[0], board])
    if not _contains_expected(board_identity, str(lead["company"])):
        raise _error(
            "Workday tenant or board does not match the expected company",
            reason_code="company_mismatch",
            definitive=True,
        )
    external_url = str(info.get("externalUrl", ""))
    external_parts = [part for part in urlparse(external_url).path.split("/") if part]
    if (
        not _same_origin(link, external_url)
        or len(external_parts) < 3
        or "job" not in external_parts
        or external_parts[-1] != job_posting_id
        or "/".join(external_parts[external_parts.index("job") + 1 :]) != job_path
    ):
        raise _error(
            "Workday CXS omitted a matching same-origin external job URL",
            reason_code="external_url_mismatch",
        )
    return {
        "method": "workday-public-cxs",
        "final_url": external_url,
        "http_status": int(response["status"]),
        "role_match": True,
        "company_or_board_match": True,
        "job_id": job_posting_id,
        "posted": True,
        "can_apply": True,
        "apply_action": external_url.rstrip("/") + "/apply/autofillWithResume",
    }


def _attribute(tag: str, name: str) -> str | None:
    match = re.search(
        rf"(?:^|\s){re.escape(name)}\s*=\s*(?:\"([^\"]*)\"|'([^']*)')",
        tag,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    return html.unescape(match.group(1) if match.group(1) is not None else match.group(2))


def _zoom_apply_fragment(page: str, base_url: str) -> tuple[str, str]:
    for tag_match in re.finditer(r"<turbo-frame\b[^>]*>", page, flags=re.IGNORECASE):
        src = _attribute(tag_match.group(0), "src")
        if not src:
            continue
        fragment_url = urljoin(base_url, src)
        parsed = urlparse(fragment_url)
        job_uid = parse_qs(parsed.query).get("job_uid", [""])[0]
        if re.fullmatch(r"/pages/[a-f0-9]+/blocks/[a-f0-9]+", parsed.path) and re.fullmatch(
            r"[a-f0-9]+", job_uid
        ):
            if not _same_origin(base_url, fragment_url):
                raise _error("Zoom apply fragment is not same-origin", reason_code="foreign_origin")
            return fragment_url, job_uid
    raise _error(
        "Zoom detail page has no authoritative lazy apply fragment", reason_code="no_apply_action"
    )


def _validate_zoom(lead: dict[str, object], fetch: Callable[..., dict[str, object]]) -> dict[str, object]:
    link = str(lead["link"])
    response = fetch(link, expect_json=False)
    status = int(response["status"])
    final_url = str(response["final_url"])
    page = str(response["text"])
    normalized = _normalize(page)
    if status != 200:
        raise _error(f"Zoom detail page returned HTTP {status}", reason_code="request_failed")
    if not _same_origin(link, final_url) or not _specific_detail_url(link, final_url):
        raise _error("Zoom detail URL redirected away from the exact job page")
    if any(marker in normalized for marker in FAIL_MARKERS):
        raise _error("Zoom detail page states that the job is unavailable", reason_code="unavailable", definitive=True)
    if not _contains_expected(page, str(lead["role"])):
        raise _error("Zoom detail page does not contain the expected role", reason_code="role_mismatch", definitive=True)
    if not _contains_expected(page, str(lead["company"])):
        raise _error("Zoom detail page does not contain the expected company", reason_code="company_mismatch", definitive=True)
    fragment_url, job_uid = _zoom_apply_fragment(page, final_url)
    fragment = fetch(fragment_url, expect_json=False)
    fragment_final = str(fragment["final_url"])
    fragment_page = str(fragment["text"])
    if int(fragment["status"]) != 200 or not _same_origin(final_url, fragment_final):
        raise _error("Zoom apply fragment did not load from the same origin", reason_code="request_failed")
    if any(marker in _normalize(fragment_page) for marker in FAIL_MARKERS):
        raise _error("Zoom apply fragment states that the job is unavailable", reason_code="unavailable", definitive=True)
    form_match = re.search(r"<form\b[^>]*>", fragment_page, flags=re.IGNORECASE)
    if not form_match:
        raise _error("Zoom apply fragment has no application form", reason_code="no_apply_action")
    action = _attribute(form_match.group(0), "action") or ""
    method = (_attribute(form_match.group(0), "method") or "").lower()
    action_url = urljoin(fragment_final, action)
    action_job_id = parse_qs(urlparse(action_url).query).get("job_id", [""])[0]
    if not _same_origin(final_url, action_url):
        raise _error("Zoom application form posts to a foreign origin", reason_code="foreign_origin")
    if method != "post" or action_job_id != job_uid:
        raise _error("Zoom application form does not bind to the exact job", reason_code="job_id_mismatch")
    if not re.search(r"<button\b[^>]*>.*?\bapply\b.*?</button>", fragment_page, re.I | re.S):
        raise _error("Zoom application form has no Apply submit action", reason_code="no_apply_action")
    return {
        "method": "zoom-same-origin-apply-fragment",
        "final_url": final_url,
        "http_status": status,
        "role_match": True,
        "company_or_board_match": True,
        "job_id": job_uid,
        "apply_fragment": fragment_url,
        "apply_action": action_url,
    }


def _validate_html(lead: dict[str, object], fetch: Callable[..., dict[str, object]]) -> dict[str, object]:
    link = str(lead["link"])
    response = fetch(link, expect_json=False)
    status = int(response["status"])
    final_url = str(response["final_url"])
    page = str(response["text"])
    normalized = _normalize(page)
    if status != 200:
        raise _error(f"detail page returned HTTP {status}", reason_code="request_failed")
    if any(marker in normalized for marker in FAIL_MARKERS):
        raise _error("detail page states that the job is unavailable", reason_code="unavailable", definitive=True)
    if not _same_origin(link, final_url):
        raise _error("detail URL redirected to a foreign origin", reason_code="foreign_origin")
    if not _specific_detail_url(link, final_url):
        raise _error("detail URL resolved to a generic page")
    if not _contains_expected(page, str(lead["role"])):
        raise _error("detail page does not contain the expected role", reason_code="role_mismatch", definitive=True)
    if not _contains_expected(page, str(lead["company"])):
        raise _error("detail page does not contain the expected company", reason_code="company_mismatch", definitive=True)
    apply_marker = next((marker for marker in APPLY_MARKERS if marker in normalized), None)
    if apply_marker is None:
        raise _error("detail page has no provable application action", reason_code="no_apply_action")
    return {
        "method": "direct-detail-page",
        "final_url": final_url,
        "http_status": status,
        "role_match": True,
        "company_or_board_match": True,
        "apply_action": apply_marker,
    }


def _validator_for(link: str) -> Callable[..., dict[str, object]]:
    hostname = (urlparse(link).hostname or "").lower()
    if hostname == "jobs.ashbyhq.com":
        return _validate_ashby
    if hostname == "careers.zoom.us":
        return _validate_zoom
    if hostname.endswith(".myworkdayjobs.com"):
        return _validate_workday
    return _validate_html


def _passing_payload(payload: object, passing_leads: list[dict[str, object]]) -> dict[str, object]:
    filtered = dict(payload) if isinstance(payload, dict) else {}
    filtered["leads"] = passing_leads
    return filtered


def validate_leads(
    payload: object,
    *,
    fetch: Callable[..., dict[str, object]] = _fetch,
    mode: str = STRICT_FINAL,
) -> dict[str, object]:
    if mode not in {STRICT_FINAL, CANDIDATE_POOL}:
        raise ValueError(f"unsupported validation mode: {mode}")
    checked_at = datetime.now(timezone.utc).isoformat()
    leads = payload.get("leads") if isinstance(payload, dict) else None
    if not isinstance(leads, list) or (mode == STRICT_FINAL and not leads):
        return {
            "status": "failed",
            "mode": mode,
            "checked_at": checked_at,
            "errors": [
                "final leads payload must contain at least one lead"
                if mode == STRICT_FINAL
                else "candidate pool payload must contain a leads list"
            ],
            "lead_count": len(leads) if isinstance(leads, list) else 0,
            "verified_count": 0,
            "withheld_count": 0,
            "leads": [],
            "passing_payload": _passing_payload(payload, []),
        }

    results: list[dict[str, object]] = []
    errors: list[str] = []
    passing_leads: list[dict[str, object]] = []
    indeterminate_count = 0
    for index, raw_lead in enumerate(leads, start=1):
        result: dict[str, object] = {"index": index}
        if not isinstance(raw_lead, dict):
            message = f"lead {index} is not an object"
            result.update(
                {
                    "status": "failed",
                    "classification": "withheld",
                    "reason_code": "invalid_input",
                    "definitive": False,
                    "error": message,
                }
            )
            errors.append(message)
            indeterminate_count += 1
            results.append(result)
            continue
        result.update({"company": raw_lead.get("company"), "role": raw_lead.get("role"), "url": raw_lead.get("link")})
        missing = [
            name
            for name in ("company", "role", "link")
            if not isinstance(raw_lead.get(name), str) or not str(raw_lead.get(name)).strip()
        ]
        try:
            if missing:
                raise _error(f"missing required fields: {', '.join(missing)}", reason_code="invalid_input")
            parsed = urlparse(str(raw_lead["link"]))
            if parsed.scheme != "https" or not parsed.netloc:
                raise _error("canonical link must be an absolute HTTPS URL", reason_code="invalid_url")
            evidence = _validator_for(str(raw_lead["link"]))(raw_lead, fetch)
            result.update(
                {
                    "status": "passed",
                    "classification": "deliverable",
                    "reason_code": "verified",
                    "definitive": True,
                    **evidence,
                }
            )
            passing_leads.append(raw_lead)
        except (VerificationError, KeyError, TypeError, ValueError) as exc:
            reason_code = exc.reason_code if isinstance(exc, VerificationError) else "malformed_response"
            definitive = exc.definitive if isinstance(exc, VerificationError) else False
            message = f"{raw_lead.get('company', 'unknown')} — {raw_lead.get('role', 'unknown')}: {exc}"
            result.update(
                {
                    "status": "failed",
                    "classification": "withheld",
                    "reason_code": reason_code,
                    "definitive": definitive,
                    "error": str(exc),
                }
            )
            errors.append(message)
            if not definitive:
                indeterminate_count += 1
        results.append(result)

    if passing_leads:
        pool_outcome = "passing_subset_available"
    elif indeterminate_count:
        pool_outcome = "validation_incomplete"
    else:
        pool_outcome = "no_deliverable_leads"
    return {
        "status": "passed" if mode == CANDIDATE_POOL or not errors else "failed",
        "mode": mode,
        "checked_at": checked_at,
        "lead_count": len(leads),
        "verified_count": len(passing_leads),
        "withheld_count": len(leads) - len(passing_leads),
        "indeterminate_count": indeterminate_count,
        "pool_outcome": pool_outcome,
        "errors": errors,
        "leads": results,
        "passing_payload": _passing_payload(payload, passing_leads),
    }


def _project_path(root: Path, value: Path, description: str) -> Path:
    path = value if value.is_absolute() else root / value
    path = path.resolve()
    if root not in path.parents:
        raise SystemExit(f"{description} must stay inside the exact project root")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project_root", type=Path)
    parser.add_argument("leads_json", type=Path)
    parser.add_argument("--mode", choices=(STRICT_FINAL, CANDIDATE_POOL), default=STRICT_FINAL)
    parser.add_argument("--evidence-out", type=Path)
    parser.add_argument(
        "--passing-out",
        type=Path,
        help="candidate-pool mode only: write a payload containing only verified leads",
    )
    args = parser.parse_args()
    if args.passing_out and args.mode != CANDIDATE_POOL:
        parser.error("--passing-out requires --mode candidate-pool")
    root = args.project_root.expanduser().resolve()
    leads_path = _project_path(root, args.leads_json, "leads JSON")
    payload = json.loads(leads_path.read_text(encoding="utf-8"))
    result = validate_leads(payload, mode=args.mode)
    rendered = json.dumps(result, indent=2) + "\n"
    if args.evidence_out:
        evidence_path = _project_path(root, args.evidence_out, "evidence output")
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        evidence_path.write_text(rendered, encoding="utf-8")
    if args.passing_out:
        passing_path = _project_path(root, args.passing_out, "passing output")
        passing_path.parent.mkdir(parents=True, exist_ok=True)
        passing_path.write_text(json.dumps(result["passing_payload"], indent=2) + "\n", encoding="utf-8")
    print(rendered, end="")
    return 0 if result["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
