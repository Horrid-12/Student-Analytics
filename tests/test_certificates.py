"""Tests for Certificate Registry, Student Upload Queue, and Admin Verification (Phase 19 / Row 78)."""

import io
import pytest
from starlette.testclient import TestClient

from app import auth, certificates, db, database
from app.main import app


@pytest.fixture(autouse=True)
def clean_certs(tmp_path, monkeypatch):
    """Use an isolated SQLite certificates database for testing."""
    test_db = tmp_path / "test_certificates.db"
    monkeypatch.setattr(certificates, "DB_PATH", test_db)
    # Ensure database.db_configured() returns False so tests exercise the clean fallback SQLite DB
    monkeypatch.setattr(database, "db_configured", lambda: False)
    yield


def _make_session(client: TestClient, email: str, role: str, name: str = "Test User"):
    from app import auth
    token = auth.create_session_token({"email": email, "role": role, "name": name})
    client.cookies.set(auth._COOKIE_NAME, token)


class TestCertificateValidation:
    def test_empty_attachment(self):
        assert certificates.validate_attachment("", b"") == "No file provided."
        assert certificates.validate_attachment("file.pdf", b"") == "No file provided."

    def test_file_too_large(self):
        huge = b"A" * (20 * 1024 * 1024 + 1)
        err = certificates.validate_attachment("doc.pdf", huge)
        assert err is not None
        assert "too large" in err.lower()

    def test_unsupported_extension(self):
        err = certificates.validate_attachment("script.exe", b"MZ\x90\x00")
        assert err is not None
        assert "invalid file format" in err.lower()

    def test_magic_bytes_detection(self):
        # Valid PDF header
        pdf_bytes = b"%PDF-1.4 sample content"
        assert certificates.validate_attachment("cert.pdf", pdf_bytes) is None

        # Fake PDF header
        assert "do not match" in certificates.validate_attachment("cert.pdf", b"NOTAPDF").lower()

        # Valid PNG header
        png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        assert certificates.validate_attachment("image.png", png_bytes) is None

        # Valid JPEG header
        jpg_bytes = b"\xff\xd8\xff\xe0\x00\x10JFIF"
        assert certificates.validate_attachment("image.jpg", jpg_bytes) is None

        # Valid WEBP header
        webp_bytes = b"RIFF\x1a\x00\x00\x00WEBPVP8 "
        assert certificates.validate_attachment("badge.webp", webp_bytes) is None


class TestCertificateStorage:
    def test_crud_lifecycle(self):
        # Create
        cert = certificates.create_certificate(
            student_email="alice@uni.edu",
            student_id="1011121314",
            student_name="Alice Wonder",
            title="Deep Learning Specialization",
            issuer="DeepLearning.AI",
            issue_date="2024-06",
            credential_url="https://coursera.org/verify/TEST1234",
            credential_id="TEST-1234",
            skills="Python, PyTorch, Neural Networks",
        )
        assert cert is not None
        cert_id = cert["id"]
        assert cert["status"] == "pending"
        assert cert["title"] == "Deep Learning Specialization"

        # Read
        fetched = certificates.get_certificate(cert_id)
        assert fetched is not None
        assert fetched["issuer"] == "DeepLearning.AI"
        assert fetched["skills"] == "Python, PyTorch, Neural Networks"

        # Counts
        counts = certificates.counts_by_status()
        assert counts["pending"] == 1
        assert counts["approved"] == 0
        assert counts["total"] == 1

        # Approve
        assert certificates.update_certificate_status(cert_id, "approved", verified_by="admin@uni.edu")
        approved = certificates.get_certificate(cert_id)
        assert approved["status"] == "approved"
        assert approved["verified_by"] == "admin@uni.edu"

        counts = certificates.counts_by_status()
        assert counts["pending"] == 0
        assert counts["approved"] == 1

        # Reject
        assert certificates.update_certificate_status(
            cert_id, "rejected", verified_by="admin@uni.edu", admin_notes="Please upload higher-res PDF."
        )
        rejected = certificates.get_certificate(cert_id)
        assert rejected["status"] == "rejected"
        assert rejected["admin_notes"] == "Please upload higher-res PDF."

        # Delete
        assert certificates.delete_certificate(cert_id)
        assert certificates.get_certificate(cert_id) is None


class TestCertificateEndpoints:
    def test_queue_requires_auth(self):
        client = TestClient(app)
        res = client.post("/certificates/queue", data={"title": "Test", "issuer": "Vendor"})
        assert res.status_code == 401

    def test_queue_missing_fields(self):
        client = TestClient(app)
        _make_session(client, "alice@uni.edu", "student", "Alice")
        res = client.post(
            "/certificates/queue",
            data={"title": "", "issuer": "", "return_to": "/me"},
            follow_redirects=False,
        )
        assert res.status_code == 303
        assert "error=missing_fields" in res.headers["location"]

    def test_queue_requires_file(self):
        client = TestClient(app)
        _make_session(client, "alice@uni.edu", "student", "Alice")
        res = client.post(
            "/certificates/queue",
            data={"title": "AWS Cloud Practitioner", "issuer": "AWS", "return_to": "/me"},
            follow_redirects=False,
        )
        assert res.status_code == 303
        assert "error=file_required" in res.headers["location"]

    def test_queue_success_with_image_attachment(self):
        client = TestClient(app)
        _make_session(client, "alice@uni.edu", "student", "Alice")
        # Valid PNG bytes
        png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
        png_file = io.BytesIO(png_bytes)
        res = client.post(
            "/certificates/queue",
            data={
                "title": "AWS Certified Architect",
                "issuer": "Amazon Web Services",
                "skills": "Cloud, AWS, S3",
                "issue_date": "2024-05",
                "return_to": "/me",
            },
            files={"attachment": ("aws_badge.png", png_file, "image/png")},
            follow_redirects=False,
        )
        assert res.status_code == 303
        assert "notice=queued" in res.headers["location"]

        # Check DB
        items = certificates.list_certificates_for_student(student_email="alice@uni.edu", approved_only=False)
        assert len(items) == 1
        assert items[0]["title"] == "AWS Certified Architect"
        assert items[0]["status"] == "pending"
        assert items[0]["is_image"] is True
        assert items[0]["is_pdf"] is False
        assert items[0]["skills"] == "Cloud, AWS, S3"

    def test_queue_success_with_pdf_attachment(self):
        client = TestClient(app)
        _make_session(client, "alice@uni.edu", "student", "Alice")
        pdf_file = io.BytesIO(b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF")
        res = client.post(
            "/certificates/queue",
            data={"title": "Docker Mastery", "issuer": "Udemy", "return_to": "/me"},
            files={"attachment": ("docker_cert.pdf", pdf_file, "application/pdf")},
            follow_redirects=False,
        )
        assert res.status_code == 303
        assert "notice=queued" in res.headers["location"]

        items = certificates.list_certificates_for_student(student_email="alice@uni.edu", approved_only=False)
        assert len(items) == 1
        assert items[0]["attachment_name"] == "docker_cert.pdf"

        # Download / view attachment
        cert_id = items[0]["id"]
        # Authenticated student downloads own attachment
        att_res = client.get(f"/certificates/{cert_id}/attachment")
        assert att_res.status_code == 200
        assert att_res.headers["content-type"] == "application/pdf"
        assert b"%PDF" in att_res.content

    def test_verification_rbac_and_admin_workflow(self):
        client = TestClient(app)

        # Seed certificate
        cert = certificates.create_certificate(
            student_email="bob@uni.edu",
            student_id="9988776655",
            student_name="Bob Builder",
            title="Kubernetes Certified Admin",
            issuer="CNCF",
            credential_url="https://cncf.io/verify/k8s",
        )
        cert_id = cert["id"]

        # Student cannot access /verification
        _make_session(client, "bob@uni.edu", "student", "Bob")
        assert client.get("/verification", follow_redirects=False).status_code in (303, 403)
        assert client.post(f"/verification/certificates/{cert_id}/approve").status_code == 403

        # Admin can access /verification and approve
        admin_client = TestClient(app)
        _make_session(admin_client, "admin@uni.edu", "admin", "Admin")
        page_res = admin_client.get("/verification")
        assert page_res.status_code == 200
        assert "Kubernetes Certified Admin" in page_res.text
        assert "Certificate Verification" in page_res.text

        # Admin approves
        appr_res = admin_client.post(
            f"/verification/certificates/{cert_id}/approve",
            data={"return_to": "/verification"},
            follow_redirects=False,
        )
        assert appr_res.status_code == 303
        assert "notice=approved" in appr_res.headers["location"]

        updated = certificates.get_certificate(cert_id)
        assert updated["status"] == "approved"
        assert updated["verified_by"] == "admin@uni.edu"

        # Admin rejects
        rej_res = admin_client.post(
            f"/verification/certificates/{cert_id}/reject",
            data={"reason": "Expired certificate", "return_to": "/verification"},
            follow_redirects=False,
        )
        assert rej_res.status_code == 303
        assert "notice=rejected" in rej_res.headers["location"]

        re_updated = certificates.get_certificate(cert_id)
        assert re_updated["status"] == "rejected"
        assert re_updated["admin_notes"] == "Expired certificate"

    def test_api_certificates_endpoint(self):
        client = TestClient(app)
        _make_session(client, "alice@uni.edu", "student", "Alice")

        certificates.create_certificate(
            student_email="alice@uni.edu",
            student_id="1122334455",
            student_name="Alice",
            title="React Basics",
            issuer="Meta",
            credential_url="https://coursera.org/verify/react",
        )
        res = client.get("/api/certificates?student_email=alice@uni.edu")
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "ok"
        assert len(data["certificates"]) == 1
        assert data["certificates"][0]["title"] == "React Basics"

    def test_delete_certificate_admin_only(self):
        client = TestClient(app)
        cert = certificates.create_certificate(
            student_email="alice@uni.edu",
            student_id="1122334455",
            student_name="Alice",
            title="TypeScript Core",
            issuer="Microsoft",
        )
        cert_id = cert["id"]

        # Student cannot delete (even own certificate)
        _make_session(client, "alice@uni.edu", "student", "Alice")
        res_stu = client.post(f"/certificates/{cert_id}/delete")
        assert res_stu.status_code == 403

        # Faculty cannot delete
        faculty_client = TestClient(app)
        _make_session(faculty_client, "prof@uni.edu", "faculty", "Professor")
        res_fac = faculty_client.post(f"/certificates/{cert_id}/delete")
        assert res_fac.status_code == 403

        # Admin can delete
        admin_client = TestClient(app)
        _make_session(admin_client, "admin@uni.edu", "admin", "Admin")
        res_adm = admin_client.post(f"/certificates/{cert_id}/delete", follow_redirects=False)
        assert res_adm.status_code == 303
        assert "notice=deleted" in res_adm.headers["location"]
        assert certificates.get_certificate(cert_id) is None


class TestSafeRedirectProtection:
    """Validate open redirect sanitization (CodeQL py/url-redirection CWE-601)."""

    def test_safe_redirect_helper_directly(self):
        from app.main import _safe_redirect_url

        # Legitimate internal paths
        assert _safe_redirect_url("/me") == "/me"
        assert _safe_redirect_url("/verification?status=pending") == "/verification?status=pending"
        assert _safe_redirect_url("/students?q=John", tab="certificates") == "/students?q=John&tab=certificates"

        # Parameter merging / overriding
        res = _safe_redirect_url("/me?tab=overview", tab="certificates", notice="queued")
        assert "tab=certificates" in res
        assert "notice=queued" in res

        # Malicious targets must all safely fall back to default
        malicious = [
            "https://evil.com",
            "http://evil.com/phish",
            "//evil.com",
            "///evil.com",
            "/\\evil.com",
            "\\\\evil.com",
            "javascript:alert(1)",
            "data:text/html,<script>alert(1)</script>",
            "evil.com",
            "/unallowed_route/secret",
            "",
            None,
        ]
        for bad in malicious:
            result = _safe_redirect_url(bad, default="/me", tab="certificates")
            assert result.startswith("/me"), f"Failed to sanitize {bad}: got {result}"
            assert "evil.com" not in result
            assert "javascript" not in result

    def test_endpoints_block_open_redirect(self):
        client = TestClient(app)
        _make_session(client, "alice@uni.edu", "student", "Alice")

        # 1. Queue endpoint blocks external redirect
        res = client.post(
            "/certificates/queue",
            data={
                "title": "AWS Cert",
                "issuer": "Amazon",
                "return_to": "https://evil.com/steal_creds",
            },
            follow_redirects=False,
        )
        assert res.status_code == 303
        loc = res.headers["location"]
        assert loc.startswith("/me?")
        assert "evil.com" not in loc

        # 2. Queue with protocol-relative URL blocks external redirect
        res2 = client.post(
            "/certificates/queue",
            data={
                "title": "AWS Cert",
                "issuer": "Amazon",
                "return_to": "//attacker.com/oauth_callback",
            },
            follow_redirects=False,
        )
        assert res2.status_code == 303
        loc2 = res2.headers["location"]
        assert loc2.startswith("/me?")
        assert "attacker.com" not in loc2

        # 3. Verification action endpoints block external redirect
        admin_client = TestClient(app)
        _make_session(admin_client, "admin@uni.edu", "admin", "Admin")

        cert = certificates.create_certificate(
            student_email="alice@uni.edu",
            student_id="1122334455",
            student_name="Alice",
            title="GCP Architect",
            issuer="Google",
        )
        cert_id = cert["id"]

        # Approve with malicious return_to
        appr = admin_client.post(
            f"/verification/certificates/{cert_id}/approve",
            data={"return_to": "https://evil-hacker.com"},
            follow_redirects=False,
        )
        assert appr.status_code == 303
        assert appr.headers["location"].startswith("/verification?")
        assert "evil-hacker.com" not in appr.headers["location"]

        # Reject with backslash malicious return_to
        rej = admin_client.post(
            f"/verification/certificates/{cert_id}/reject",
            data={"reason": "Invalid", "return_to": "/\\evil-hacker.com"},
            follow_redirects=False,
        )
        assert rej.status_code == 303
        assert rej.headers["location"].startswith("/verification?")
        assert "evil-hacker.com" not in rej.headers["location"]

        # Delete with javascript URI return_to
        del_res = admin_client.post(
            f"/verification/certificates/{cert_id}/delete",
            data={"return_to": "javascript:alert(document.cookie)"},
            follow_redirects=False,
        )
        assert del_res.status_code == 303
        assert del_res.headers["location"].startswith("/verification?")
        assert "javascript" not in del_res.headers["location"]


class TestFacultyVerificationScoping:
    """Faculty Verification is restricted to the students they teach."""

    def _students(self):
        import pandas as pd

        return pd.DataFrame([
            {"Student_ID": "3000000001", "Student Name": "Teach A", "Roster_Email": "teach-a@college.edu",
             "Division": "3", "Batch": "1", "Semester": "Semester 3"},
            {"Student_ID": "3000000002", "Student Name": "Teach B", "Roster_Email": "teach-b@college.edu",
             "Division": "3", "Batch": "2", "Semester": "Semester 3"},
            {"Student_ID": "3000000003", "Student Name": "Teach C", "Roster_Email": "teach-c@college.edu",
             "Division": "5", "Batch": "1", "Semester": "Semester 5"},
        ])

    def _cert(self, student_id, email, name, title):
        return certificates.create_certificate(
            student_email=email, student_id=student_id, student_name=name,
            title=title, issuer="Org",
        )

    def test_filter_by_prn_and_email(self):
        from app import views

        a = self._cert("3000000001", "teach-a@college.edu", "Teach A", "A")
        self._cert("3000000003", "teach-c@college.edu", "Teach C", "C")
        students = self._students()
        scoped = views.filter_certificates_for_students(
            certificates.list_certificates(status=None, limit=500), students
        )
        # Everything scoped to the full frame; then narrow to Division 3.
        assert {c["title"] for c in scoped} == {"A", "C"}
        only_div3 = views.verification_payload(
            {"students": students},
            certificates.list_certificates(status=None, limit=500),
            division="3",
        )
        assert [c["title"] for c in only_div3["items"]] == ["A"]

    def test_verification_payload_applies_semester_and_batch(self):
        from app import views

        self._cert("3000000001", "teach-a@college.edu", "Teach A", "A")
        self._cert("3000000002", "teach-b@college.edu", "Teach B", "B")
        students = self._students()
        items = certificates.list_certificates(status=None, limit=500)
        by_batch = views.verification_payload({"students": students}, items, batch="2")
        assert [c["title"] for c in by_batch["items"]] == ["B"]
        by_sem = views.verification_payload({"students": students}, items, semester="Semester 5")
        assert [c["title"] for c in by_sem["items"]] == []
        assert by_sem["total"] == 0

    def test_certificate_without_matching_student_is_hidden(self):
        from app import views

        self._cert("9999999999", "stranger@college.edu", "Stranger", "Hidden")
        payload = views.verification_payload(
            {"students": self._students()},
            certificates.list_certificates(status=None, limit=500),
        )
        assert payload["items"] == []


class TestQueuedCertificateStoresPrn:
    """BUG-151: ``POST /certificates/queue`` read ``request.state.user_row``,
    which is only assigned by ``_base_context`` (page renders) - a POST never
    has it, so every queued certificate was stored with ``student_id=""`` and
    PRN-keyed lookups silently missed it."""

    def test_queue_persists_the_account_prn(self):
        email = "prn-queue@college.edu"
        assert auth.create_user(email, "secret123", "student", "Prn Queue") is not None
        assert auth.db_set_onboarding(email, prn="1234567890")

        client = TestClient(app)
        _make_session(client, email, "student", "Prn Queue")
        res = client.post(
            "/certificates/queue",
            data={"title": "Cloud Basics", "issuer": "AWS", "return_to": "/me"},
            files={"attachment": ("cert.pdf", b"%PDF-1.4 sample content", "application/pdf")},
            follow_redirects=False,
        )
        assert res.status_code == 303
        assert "notice=queued" in res.headers["location"]

        items = certificates.list_certificates_for_student(student_email=email, approved_only=False)
        assert len(items) == 1
        assert items[0]["student_id"] == "1234567890"

    def test_queue_without_an_account_row_still_works(self):
        # Legacy token-only session (no users row): the lookup returns None and
        # the certificate is stored with an empty PRN instead of failing.
        client = TestClient(app)
        _make_session(client, "ghost@uni.edu", "student", "Ghost")
        res = client.post(
            "/certificates/queue",
            data={"title": "No Row", "issuer": "Vendor", "return_to": "/me"},
            files={"attachment": ("cert.pdf", b"%PDF-1.4 sample content", "application/pdf")},
            follow_redirects=False,
        )
        assert res.status_code == 303
        assert "notice=queued" in res.headers["location"]
        items = certificates.list_certificates_for_student(student_email="ghost@uni.edu", approved_only=False)
        assert len(items) == 1
        assert items[0]["student_id"] == ""


class TestCertificateApiScoping:
    """BUG-148: ``/api/certificates`` authorised only ``student_email`` while
    the query matched ``student_email OR student_id``, so a student could pass
    their own address plus another student's PRN and read that student's
    rejected certificate (admin_notes, verified_by, skills)."""

    def _seed_two_students(self):
        assert auth.create_user("alice@college.edu", "secret123", "student", "Alice") is not None
        assert auth.db_set_onboarding("alice@college.edu", prn="1111111111")
        assert auth.create_user("bob@college.edu", "secret123", "student", "Bob") is not None
        assert auth.db_set_onboarding("bob@college.edu", prn="9999888877")
        own = certificates.create_certificate(
            student_email="alice@college.edu",
            student_id="1111111111",
            student_name="Alice",
            title="Own Cert",
            issuer="Meta",
        )
        victim = certificates.create_certificate(
            student_email="bob@college.edu",
            student_id="9999888877",
            student_name="Bob",
            title="Secret Cert",
            issuer="Vendor",
            skills="secret skill",
        )
        certificates.update_certificate_status(victim["id"], "rejected", "admin@college.edu", "ID was fake")
        return own, victim

    def test_student_cannot_read_another_student_by_prn(self):
        self._seed_two_students()
        client = TestClient(app)
        _make_session(client, "alice@college.edu", "student", "Alice")

        res = client.get("/api/certificates?student_email=alice@college.edu&student_id=9999888877")
        assert res.status_code == 200
        items = res.json()["certificates"]
        assert [c["title"] for c in items] == ["Own Cert"]
        assert all(c["student_email"] == "alice@college.edu" for c in items)
        assert not any(c.get("admin_notes") for c in items)

    def test_student_gets_own_rows_for_any_query_string(self):
        self._seed_two_students()
        client = TestClient(app)
        _make_session(client, "alice@college.edu", "student", "Alice")

        res = client.get("/api/certificates?student_email=bob@college.edu")
        assert res.status_code == 200
        assert [c["title"] for c in res.json()["certificates"]] == ["Own Cert"]

    def test_admin_can_still_query_any_student(self):
        self._seed_two_students()
        client = TestClient(app)
        _make_session(client, "admin@college.edu", "admin", "Admin")

        res = client.get("/api/certificates?student_id=9999888877")
        assert res.status_code == 200
        items = res.json()["certificates"]
        assert [c["title"] for c in items] == ["Secret Cert"]
        assert items[0]["admin_notes"] == "ID was fake"

    def test_faculty_can_still_query_a_student(self):
        self._seed_two_students()
        approved = certificates.create_certificate(
            student_email="bob@college.edu",
            student_id="9999888877",
            student_name="Bob",
            title="Approved Cert",
            issuer="Vendor",
        )
        certificates.update_certificate_status(approved["id"], "approved", "prof@college.edu")
        client = TestClient(app)
        _make_session(client, "prof@college.edu", "faculty", "Professor")

        res = client.get("/api/certificates?student_email=bob@college.edu")
        assert res.status_code == 200
        assert [c["title"] for c in res.json()["certificates"]] == ["Approved Cert"]
