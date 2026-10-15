"""Tests for Certificate Registry, Student Upload Queue, and Admin Verification (Phase 19 / Row 78)."""

import io
import pytest
from starlette.testclient import TestClient

from app import certificates, db, database
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

    def test_queue_requires_proof_url_or_file(self):
        client = TestClient(app)
        _make_session(client, "alice@uni.edu", "student", "Alice")
        res = client.post(
            "/certificates/queue",
            data={"title": "AWS Cloud Practitioner", "issuer": "AWS", "return_to": "/me"},
            follow_redirects=False,
        )
        assert res.status_code == 303
        assert "error=proof_required" in res.headers["location"]

    def test_queue_success_with_url(self):
        client = TestClient(app)
        _make_session(client, "alice@uni.edu", "student", "Alice")
        res = client.post(
            "/certificates/queue",
            data={
                "title": "AWS Certified Architect",
                "issuer": "Amazon Web Services",
                "credential_url": "https://credly.com/earner/test",
                "skills": "Cloud, AWS, S3",
                "return_to": "/me",
            },
            follow_redirects=False,
        )
        assert res.status_code == 303
        assert "notice=queued" in res.headers["location"]

        # Check DB
        items = certificates.list_certificates_for_student(student_email="alice@uni.edu", approved_only=False)
        assert len(items) == 1
        assert items[0]["title"] == "AWS Certified Architect"
        assert items[0]["status"] == "pending"

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
