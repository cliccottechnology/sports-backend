"""Academy documents: retitle/recategorise, replace file (type stays in sync), delete."""
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient


@pytest.fixture
def authed():
    from apps.accounts.models import Permission, Role, User
    role, _ = Role.objects.get_or_create(name="doc_role")
    codes = ["documents.create", "documents.edit", "documents.archive"]
    role.permissions.add(*Permission.objects.filter(code__in=codes))
    cl = APIClient()
    u = User(username="docboss", role=role, is_active=True)
    u.set_password("pass12345")
    u.save()
    r = cl.post("/api/auth/login/", {"username": "docboss", "password": "pass12345"})
    assert r.status_code == 200, r.content
    cl.credentials(HTTP_AUTHORIZATION=f"Bearer {r.data['access']}")
    return cl


def _upload(cl, title="Policy", name="policy.pdf"):
    return cl.post("/api/documents/", {"title": title, "category": "academy",
                                       "file": SimpleUploadedFile(name, b"%PDF-1.4 fake")}, format="multipart")


@pytest.mark.django_db
def test_document_edit_and_replace(authed):
    r = _upload(authed)
    assert r.status_code == 201, r.content
    doc_id = r.data["id"]
    assert r.data["file_type"] == "pdf"

    r = authed.patch(f"/api/documents/{doc_id}/", {"title": "Sports policy", "category": "receipt"}, format="json")
    assert r.status_code == 200, r.content
    assert r.data["title"] == "Sports policy"
    assert r.data["category"] == "receipt"
    assert r.data["file_type"] == "pdf"  # untouched file keeps its type

    r = authed.patch(f"/api/documents/{doc_id}/",
                     {"file": SimpleUploadedFile("scan.png", b"\x89PNG fake")}, format="multipart")
    assert r.status_code == 200, r.content
    assert r.data["file_type"] == "png"  # replaced file re-syncs the type


@pytest.mark.django_db
def test_document_delete(authed):
    from apps.documents.models import Document
    r = _upload(authed, title="Temp")
    assert r.status_code == 201, r.content
    doc_id = r.data["id"]
    r = authed.delete(f"/api/documents/{doc_id}/")
    assert r.status_code == 204, r.content
    assert Document.objects.filter(pk=doc_id).count() == 0
