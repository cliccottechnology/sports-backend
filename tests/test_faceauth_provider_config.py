"""Config/selection tests for the photo-attendance vision provider.

They lock in that `faceauth` is the only provider and is really wired to a real
face engine, and that the startup check flags a misconfigured deployment
instead of silently degrading. They also cover idempotent AWS collection
creation.
"""
import pytest


class _NotFound(Exception):
    pass


class _AlreadyExists(Exception):
    pass


class _Exceptions:
    ResourceNotFoundException = _NotFound
    ResourceAlreadyExistsException = _AlreadyExists


class _FakeClient:
    def __init__(self, describe_raises=None, create_raises=None):
        self.exceptions = _Exceptions
        self.describe_raises = describe_raises
        self.create_raises = create_raises
        self.describe_calls = 0
        self.create_calls = 0

    def describe_collection(self, CollectionId):
        self.describe_calls += 1
        if self.describe_raises:
            raise self.describe_raises
        return {"CollectionArn": "arn:aws:rekognition:::collection/col"}

    def create_collection(self, CollectionId):
        self.create_calls += 1
        if self.create_raises:
            raise self.create_raises
        return {"StatusCode": 200}


def _run_check(settings, **overrides):
    from faceauth.checks import attendance_vision_provider_check

    for key, value in overrides.items():
        setattr(settings, key, value)
    return attendance_vision_provider_check(None)


def test_get_provider_raises_for_unknown_provider(settings):
    from apps.attendance.vision import get_provider

    settings.ATTENDANCE_VISION_PROVIDER = "pilot"
    with pytest.raises(ValueError):
        get_provider()


@pytest.mark.django_db
def test_get_provider_selects_faceauth(settings):
    from apps.attendance.vision import get_provider

    settings.ATTENDANCE_VISION_PROVIDER = "faceauth"
    settings.FACE_VERIFICATION_PROVIDER = "mock"
    assert get_provider().name == "faceauth"


def test_check_errors_when_faceauth_has_no_engine(settings):
    from faceauth.checks import attendance_vision_provider_check

    settings.ATTENDANCE_VISION_PROVIDER = "faceauth"
    settings.FACE_VERIFICATION_PROVIDER = "none"
    issues = attendance_vision_provider_check(None)
    assert [i.id for i in issues] == ["faceauth.E002"]


def test_check_passes_for_faceauth_with_aws(settings):
    from faceauth.checks import attendance_vision_provider_check

    settings.ATTENDANCE_VISION_PROVIDER = "faceauth"
    settings.FACE_VERIFICATION_PROVIDER = "aws"
    issues = attendance_vision_provider_check(None)
    # Only warnings (missing region/creds) are acceptable; never an error.
    assert not [i for i in issues if i.id.startswith("faceauth.E")]


def test_check_flags_unknown_provider(settings):
    from faceauth.checks import attendance_vision_provider_check

    settings.ATTENDANCE_VISION_PROVIDER = "banana"
    issues = attendance_vision_provider_check(None)
    assert [i.id for i in issues] == ["faceauth.E001"]


@pytest.mark.parametrize("describe_raises", [None, _NotFound, RuntimeError])
def test_ensure_collection_is_idempotent(describe_raises):
    from faceauth.providers_aws import AWSRekognitionProvider

    client = _FakeClient(describe_raises=describe_raises)
    AWSRekognitionProvider()._ensure_collection(client, "cliccot-students-1")
    expected_create = 0 if describe_raises is None else 1
    assert client.create_calls == expected_create


def test_ensure_collection_tolerates_concurrent_create():
    from faceauth.providers_aws import AWSRekognitionProvider

    client = _FakeClient(describe_raises=_NotFound, create_raises=_AlreadyExists)
    # Must not raise even though another process created it first.
    AWSRekognitionProvider()._ensure_collection(client, "cliccot-students-1")
    assert client.create_calls == 1
