import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "validate_job_links.py"
SPEC = importlib.util.spec_from_file_location("validate_job_links", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def workday_lead(**changes):
    lead = {
        "company": "UserTesting",
        "role": "Senior Content Marketing Manager, AI & Thought Leadership",
        "link": (
            "https://usertesting.wd12.myworkdayjobs.com/UserTesting/job/Remote---US/"
            "Senior-Content-Marketing-Manager--AI---Thought-Leadership_R-101234-1"
        ),
    }
    lead.update(changes)
    return lead


def workday_payload(lead, **changes):
    info = {
        "title": lead["role"],
        "jobPostingId": "Senior-Content-Marketing-Manager--AI---Thought-Leadership_R-101234-1",
        "jobPostingSiteId": "UserTesting",
        "posted": True,
        "canApply": True,
        "externalUrl": lead["link"],
    }
    info.update(changes)
    return {"jobPostingInfo": info}


def zoom_lead(**changes):
    lead = {
        "company": "Zoom",
        "role": "Integrated Marketing Manager",
        "link": "https://careers.zoom.us/jobs/integrated-marketing-manager-san-jose-california-united-states",
    }
    lead.update(changes)
    return lead


ZOOM_JOB_UID = "02b04ade7648eabcbeecda6fa3eb00dc"
ZOOM_FRAGMENT = (
    "https://careers.zoom.us/pages/6398d0d23c60ced13ca1e3c893ccbe32/"
    "blocks/d7fdcc51b4ac3e90115ed5cc7e762613?job_uid=" + ZOOM_JOB_UID + "&postfix=2_1"
)


def zoom_detail(fragment=ZOOM_FRAGMENT):
    return (
        "<title>Zoom — Integrated Marketing Manager</title>"
        f'<turbo-frame id="apply" src="{fragment}"></turbo-frame>'
    )


def zoom_form(*, action_host="careers.zoom.us", job_uid=ZOOM_JOB_UID, include_button=True):
    button = '<button type="submit"><span>Apply</span></button>' if include_button else ""
    return (
        f'<form data-action="turbo:submit-start-&gt;controller#start" method="post" '
        f'action="https://{action_host}/call_to_actions/cta/form_submissions'
        f'?job_id={job_uid}&amp;page_id=page">{button}</form>'
    )


class ValidateJobLinksTests(unittest.TestCase):
    def test_private_address_is_rejected_before_connection(self):
        private = [(2, 1, 6, "", ("127.0.0.1", 443))]
        with patch.object(MODULE.socket, "getaddrinfo", return_value=private), patch.object(
            MODULE.socket, "socket"
        ) as connection:
            with self.assertRaisesRegex(MODULE.VerificationError, "non-public"):
                MODULE._fetch("https://example.com/jobs/123")
            connection.assert_not_called()

    def test_redirect_to_private_address_is_rejected(self):
        original = MODULE._request_public_once
        calls = []

        def first_then_real(url, *, expect_json):
            calls.append(url)
            if len(calls) == 1:
                return 302, "https://localhost/admin", ""
            return original(url, expect_json=expect_json)

        private = [(2, 1, 6, "", ("127.0.0.1", 443))]
        with patch.object(MODULE, "_request_public_once", side_effect=first_then_real), patch.object(
            MODULE.socket, "getaddrinfo", return_value=private
        ), patch.object(MODULE.socket, "socket") as connection:
            with self.assertRaisesRegex(MODULE.VerificationError, "non-public"):
                MODULE._fetch("https://example.com/jobs/123")
            self.assertEqual(["https://example.com/jobs/123", "https://localhost/admin"], calls)
            connection.assert_not_called()

    def test_redirect_to_plain_http_is_rejected(self):
        with self.assertRaisesRegex(MODULE.VerificationError, "public HTTPS"):
            MODULE._public_target("http://example.com/jobs/123")

    def test_dead_ashby_job_fails_closed(self):
        payload = {
            "leads": [
                {
                    "company": "Atropos Health",
                    "role": "Senior Marketing Manager",
                    "link": "https://jobs.ashbyhq.com/atroposhealth/dead-id",
                }
            ]
        }

        def fetch(url, **_kwargs):
            return {
                "status": 200,
                "final_url": url,
                "text": json.dumps(
                    {
                        "jobs": [
                            {
                                "id": "other-id",
                                "title": "Future Openings",
                                "isListed": True,
                                "applyUrl": "https://example.com/application",
                            }
                        ]
                    }
                ),
            }

        result = MODULE.validate_leads(payload, fetch=fetch)
        self.assertEqual(result["status"], "failed")
        self.assertIn("not listed", result["errors"][0])
        self.assertEqual(result["leads"][0]["reason_code"], "unavailable")

    def test_live_ashby_job_passes_with_board_api(self):
        payload = {
            "leads": [
                {
                    "company": "Example Health",
                    "role": "Senior Marketing Manager",
                    "link": "https://jobs.ashbyhq.com/example/job-id",
                }
            ]
        }

        def fetch(url, **_kwargs):
            return {
                "status": 200,
                "final_url": url,
                "text": json.dumps(
                    {
                        "jobs": [
                            {
                                "id": "job-id",
                                "title": "Senior Marketing Manager",
                                "isListed": True,
                                "jobUrl": payload["leads"][0]["link"],
                                "applyUrl": payload["leads"][0]["link"] + "/application",
                            }
                        ]
                    }
                ),
            }

        result = MODULE.validate_leads(payload, fetch=fetch)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["leads"][0]["method"], "ashby-public-board-api")

    def test_live_workday_job_passes_with_exact_cxs_record(self):
        lead = workday_lead()

        def fetch(url, **kwargs):
            self.assertIn("/wday/cxs/usertesting/UserTesting/job/", url)
            self.assertTrue(kwargs["expect_json"])
            return {"status": 200, "final_url": url, "text": json.dumps(workday_payload(lead))}

        result = MODULE.validate_leads({"leads": [lead]}, fetch=fetch)
        evidence = result["leads"][0]
        self.assertEqual(result["status"], "passed")
        self.assertEqual(evidence["method"], "workday-public-cxs")
        self.assertTrue(evidence["posted"])
        self.assertTrue(evidence["can_apply"])
        self.assertTrue(evidence["apply_action"].endswith("/apply/autofillWithResume"))

    def test_closed_workday_job_fails_closed(self):
        lead = workday_lead()

        def fetch(url, **_kwargs):
            return {
                "status": 200,
                "final_url": url,
                "text": json.dumps(workday_payload(lead, posted=False, canApply=False)),
            }

        result = MODULE.validate_leads({"leads": [lead]}, fetch=fetch)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["leads"][0]["reason_code"], "unavailable")
        self.assertTrue(result["leads"][0]["definitive"])

    def test_workday_mismatched_job_id_fails_closed(self):
        lead = workday_lead()

        def fetch(url, **_kwargs):
            return {
                "status": 200,
                "final_url": url,
                "text": json.dumps(workday_payload(lead, jobPostingId="other-job")),
            }

        result = MODULE.validate_leads({"leads": [lead]}, fetch=fetch)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["leads"][0]["reason_code"], "job_id_mismatch")

    def test_workday_malformed_response_fails_closed(self):
        lead = workday_lead()

        def fetch(url, **_kwargs):
            return {"status": 200, "final_url": url, "text": "not-json"}

        result = MODULE.validate_leads({"leads": [lead]}, fetch=fetch)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["leads"][0]["reason_code"], "malformed_response")

    def test_workday_foreign_external_url_fails_closed(self):
        lead = workday_lead()

        def fetch(url, **_kwargs):
            return {
                "status": 200,
                "final_url": url,
                "text": json.dumps(
                    workday_payload(
                        lead,
                        externalUrl="https://evil.example/UserTesting/job/Remote---US/"
                        "Senior-Content-Marketing-Manager--AI---Thought-Leadership_R-101234-1",
                    )
                ),
            }

        result = MODULE.validate_leads({"leads": [lead]}, fetch=fetch)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["leads"][0]["reason_code"], "external_url_mismatch")

    def test_zoom_job_passes_with_same_origin_lazy_apply_form(self):
        lead = zoom_lead()

        def fetch(url, **_kwargs):
            if url == lead["link"]:
                return {"status": 200, "final_url": url, "text": zoom_detail()}
            self.assertEqual(url, ZOOM_FRAGMENT)
            return {"status": 200, "final_url": url, "text": zoom_form()}

        result = MODULE.validate_leads({"leads": [lead]}, fetch=fetch)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["leads"][0]["method"], "zoom-same-origin-apply-fragment")
        self.assertEqual(result["leads"][0]["job_id"], ZOOM_JOB_UID)

    def test_zoom_closed_fragment_fails_closed(self):
        lead = zoom_lead()

        def fetch(url, **_kwargs):
            text = zoom_detail() if url == lead["link"] else "Job is no longer accepting applications"
            return {"status": 200, "final_url": url, "text": text}

        result = MODULE.validate_leads({"leads": [lead]}, fetch=fetch)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["leads"][0]["reason_code"], "unavailable")

    def test_zoom_mismatched_fragment_job_id_fails_closed(self):
        lead = zoom_lead()

        def fetch(url, **_kwargs):
            text = zoom_detail() if url == lead["link"] else zoom_form(job_uid="other")
            return {"status": 200, "final_url": url, "text": text}

        result = MODULE.validate_leads({"leads": [lead]}, fetch=fetch)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["leads"][0]["reason_code"], "job_id_mismatch")

    def test_zoom_foreign_origin_fragment_fails_before_fetch(self):
        lead = zoom_lead()
        foreign_fragment = ZOOM_FRAGMENT.replace("careers.zoom.us", "evil.example")
        calls = []

        def fetch(url, **_kwargs):
            calls.append(url)
            return {"status": 200, "final_url": url, "text": zoom_detail(foreign_fragment)}

        result = MODULE.validate_leads({"leads": [lead]}, fetch=fetch)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["leads"][0]["reason_code"], "foreign_origin")
        self.assertEqual(calls, [lead["link"]])

    def test_zoom_foreign_origin_application_form_fails_closed(self):
        lead = zoom_lead()

        def fetch(url, **_kwargs):
            text = zoom_detail() if url == lead["link"] else zoom_form(action_host="evil.example")
            return {"status": 200, "final_url": url, "text": text}

        result = MODULE.validate_leads({"leads": [lead]}, fetch=fetch)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["leads"][0]["reason_code"], "foreign_origin")

    def test_live_direct_page_requires_role_company_and_apply(self):
        payload = {
            "leads": [
                {
                    "company": "Authentic8",
                    "role": "Sr. Content Marketing Manager",
                    "link": "https://jobs.lever.co/authentic8/job-id",
                }
            ]
        }

        def fetch(url, **_kwargs):
            return {
                "status": 200,
                "final_url": url,
                "text": "<title>Authentic8 - Sr. Content Marketing Manager</title><a>Apply for this job</a>",
            }

        self.assertEqual(MODULE.validate_leads(payload, fetch=fetch)["status"], "passed")

    def test_unavailable_marker_rejects_page(self):
        payload = {
            "leads": [
                {
                    "company": "Example",
                    "role": "Content Lead",
                    "link": "https://jobs.example.com/jobs/123",
                }
            ]
        }

        def fetch(url, **_kwargs):
            return {"status": 200, "final_url": url, "text": "Example Content Lead — Job not found — Apply now"}

        self.assertEqual(MODULE.validate_leads(payload, fetch=fetch)["status"], "failed")

    def test_generic_board_redirect_fails(self):
        payload = {
            "leads": [
                {
                    "company": "Example",
                    "role": "Content Lead",
                    "link": "https://careers.example.com/jobs/123",
                }
            ]
        }

        def fetch(_url, **_kwargs):
            return {"status": 200, "final_url": "https://careers.example.com/", "text": "Example Content Lead Apply now"}

        self.assertEqual(MODULE.validate_leads(payload, fetch=fetch)["status"], "failed")

    def test_foreign_origin_generic_redirect_fails(self):
        payload = {
            "leads": [
                {
                    "company": "Example",
                    "role": "Content Lead",
                    "link": "https://careers.example.com/jobs/123",
                }
            ]
        }

        def fetch(_url, **_kwargs):
            return {
                "status": 200,
                "final_url": "https://evil.example/jobs/123",
                "text": "Example Content Lead Apply now",
            }

        result = MODULE.validate_leads(payload, fetch=fetch)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["leads"][0]["reason_code"], "foreign_origin")

    def test_candidate_pool_returns_passing_subset_without_weakening_strict_mode(self):
        passing = {
            "company": "Example",
            "role": "Content Lead",
            "link": "https://jobs.example.com/jobs/123",
        }
        rejected = {
            "company": "Closed Co",
            "role": "Marketing Lead",
            "link": "https://jobs.closed.example/jobs/456",
        }
        payload = {"profile_id": "person", "leads": [passing, rejected]}

        def fetch(url, **_kwargs):
            if "closed.example" in url:
                return {"status": 200, "final_url": url, "text": "Closed Co Marketing Lead Job not found Apply now"}
            return {"status": 200, "final_url": url, "text": "Example Content Lead Apply now"}

        strict = MODULE.validate_leads(payload, fetch=fetch)
        pool = MODULE.validate_leads(payload, fetch=fetch, mode=MODULE.CANDIDATE_POOL)
        self.assertEqual(strict["status"], "failed")
        self.assertEqual(pool["status"], "passed")
        self.assertEqual(pool["verified_count"], 1)
        self.assertEqual(pool["withheld_count"], 1)
        self.assertEqual(pool["pool_outcome"], "passing_subset_available")
        self.assertEqual(pool["passing_payload"], {"profile_id": "person", "leads": [passing]})
        self.assertEqual(pool["leads"][1]["classification"], "withheld")

    def test_pool_distinguishes_definitive_zero_from_incomplete_validation(self):
        closed = {
            "company": "Closed Co",
            "role": "Marketing Lead",
            "link": "https://jobs.closed.example/jobs/456",
        }

        def closed_fetch(url, **_kwargs):
            return {"status": 200, "final_url": url, "text": "Closed Co Marketing Lead Job not found Apply now"}

        definitive = MODULE.validate_leads(
            {"leads": [closed]}, fetch=closed_fetch, mode=MODULE.CANDIDATE_POOL
        )
        incomplete = MODULE.validate_leads(
            {"leads": [{"company": "Broken", "role": "Lead", "link": "not-https"}]},
            mode=MODULE.CANDIDATE_POOL,
        )
        self.assertEqual(definitive["pool_outcome"], "no_deliverable_leads")
        self.assertEqual(definitive["indeterminate_count"], 0)
        self.assertEqual(incomplete["pool_outcome"], "validation_incomplete")
        self.assertEqual(incomplete["indeterminate_count"], 1)


if __name__ == "__main__":
    unittest.main()
