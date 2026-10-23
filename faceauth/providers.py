"""Face provider interface.

Production MUST configure a real matching + liveness provider
(FACE_VERIFICATION_PROVIDER). Until then every verification
attempt fails closed — nothing is ever treated as verified.
"""


class ProviderNotConfigured(Exception):
    pass


class FaceProvider:
    """1:1 matcher + liveness checker. Implement per vendor."""

    name = "base"

    def enroll(self, image_bytes_list, company_id=None, employee_code=""):
        """Return one template bytes per image. Raise on failure."""
        raise NotImplementedError

    def verify(self, template_bytes_list, image_bytes, company_id=None):
        """Return (match: bool, score: float). Raise on failure."""
        raise NotImplementedError

    def liveness(self, image_bytes):
        """Return True if a live person is present. Raise on failure."""
        raise NotImplementedError

    # --- Server-driven liveness (video challenge on the device) -------
    def create_liveness_session(self, client_token):
        """Return {"session_id": str, "region": str}."""
        raise NotImplementedError

    def get_liveness_result(self, session_id):
        """Return {"status": str, "confidence": float, "reference_image": bytes|None}."""
        raise NotImplementedError

    def issue_client_credentials(self, user_id):
        """Short-lived device credentials for the liveness stream, or None."""
        return None

    def find_face_ids(self, image_bytes, company_id=None, scope=""):
        return []

    def ping(self):
        """Auth/connectivity probe. Returns a backend-specific value or None."""
        return None

    def delete_faces(self, face_ids, company_id=None, scope=""):
        return 0

    # --- Group (1:N) identification -----------------------------------
    def identify_faces(self, image_bytes, company_id=None, scope="students",
                       min_similarity=80.0, max_faces=100):
        """Find EVERY face in one photo and search each in the collection.

        Used by the teacher app: one click -> whole class.  Returns a list of
        observations, one per detected face:

            {
              "box": {"left","top","width","height"},   # normalised 0..1
              "detection_confidence": float,
              "quality": {"brightness": float, "sharpness": float},
              "flags": [str],      # e.g. DEVICE_OVER_FACE, SCREEN_PATTERN, OCCLUDED
              "matches": [{"face_id": str, "similarity": float}, ...]  # best first
            }
        """
        raise NotImplementedError


class MockFaceProvider(FaceProvider):
    """Sandbox / Mock provider for testing without live AWS billing or keys."""

    name = "mock"

    # Sandbox registry so the group flow can be demoed without AWS:
    # (company_id, scope) -> [face_id, ...].  identify_faces() "sees" every
    # enrolled face once (everyone present) unless `scripted` is set.
    registry = {}
    scripted = None

    def enroll(self, image_bytes_list, company_id=None, employee_code="", scope=""):
        ids = [
            f"mock-face-id-{employee_code or 'emp'}-{i}".encode()
            for i in range(len(image_bytes_list))
        ]
        MockFaceProvider.registry.setdefault((company_id, scope), []).extend(
            i.decode() for i in ids
        )
        return ids

    def identify_faces(self, image_bytes, company_id=None, scope="students",
                       min_similarity=80.0, max_faces=100):
        from django.conf import settings as dj_settings
        is_demo_or_test = (
            getattr(dj_settings, "DEBUG", False)
            or getattr(dj_settings, "ALLOW_MOCK_FACE_AUTH", False)
            or getattr(dj_settings, "TESTING", False)
        )
        if not is_demo_or_test:
            raise ProviderNotConfigured(
                "Mock face recognition provider is not permitted in production. "
                "Set FACE_VERIFICATION_PROVIDER=aws with valid AWS credentials."
            )

        if MockFaceProvider.scripted is not None:
            return list(MockFaceProvider.scripted)

        if not image_bytes:
            return []

        # Empty image inspection: solid blank colors or near-zero variance contain no faces
        try:
            import io
            from PIL import Image, ImageStat
            with Image.open(io.BytesIO(image_bytes)) as im:
                im.verify()
            with Image.open(io.BytesIO(image_bytes)) as im:
                stat = ImageStat.Stat(im.convert("L"))
                if stat.stddev[0] < 3.0 or stat.mean[0] < 5.0 or (stat.mean[0] > 250.0 and stat.stddev[0] < 5.0):
                    return []
        except Exception:
            return []

        out = []
        seen = set()
        for n, fid in enumerate(MockFaceProvider.registry.get((company_id, scope), [])):
            owner = fid.rsplit("-", 1)[0]  # one observation per person
            if owner in seen:
                continue
            seen.add(owner)
            out.append({
                "box": {"left": (n % 10) / 10.0, "top": (n // 10) / 10.0,
                        "width": 0.08, "height": 0.1},
                "detection_confidence": 99.0,
                "quality": {"brightness": 70.0, "sharpness": 70.0},
                "flags": [],
                "matches": [{"face_id": fid, "similarity": 99.0}],
            })
        return out[:max_faces]

    def verify(self, template_bytes_list, image_bytes, company_id=None, quality_gate=False):
        return True, 99.0

    def liveness(self, image_bytes):
        return True

    def create_liveness_session(self, client_token):
        return {
            "session_id": f"mock-sess-{client_token[:8]}",
            "region": "ap-south-1",
        }

    def get_liveness_result(self, session_id):
        tiny_jpeg = (
            b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00"
            b"\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t\x08"
            b"\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a\x1f\x1e"
            b"\x1d\x1a\x1c\x1c $.' \",#\x1c\x1c(7),01444\x1f'9=82<.342\xff\xc0"
            b"\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00\xff\xc4\x00\x1f\x00"
            b"\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00"
            b"\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\xff\xda\x00\x08\x01"
            b"\x01\x00\x00?\x00\xbf\x00\xff\xd9"
        )
        return {
            "status": "SUCCEEDED",
            "confidence": 99.5,
            "reference_image": tiny_jpeg,
        }

    def issue_client_credentials(self, user_id):
        return {
            "access_key_id": "MOCK_KEY",
            "secret_access_key": "MOCK_SECRET",
            "session_token": "MOCK_TOKEN",
            "expiration": "2099-01-01T00:00:00Z",
        }

    def find_face_ids(self, image_bytes, company_id=None, scope=""):
        return []

    def delete_faces(self, face_ids, company_id=None, scope=""):
        gone = {f.decode() if isinstance(f, bytes) else str(f) for f in face_ids}
        for key, ids in MockFaceProvider.registry.items():
            MockFaceProvider.registry[key] = [i for i in ids if i not in gone]
        return len(face_ids)


class UnconfiguredProvider(FaceProvider):
    name = "none"

    def _fail(self):
        raise ProviderNotConfigured(
            "No face verification provider configured. Set "
            "FACE_VERIFICATION_PROVIDER and provider credentials; "
            "verification stays disabled until then."
        )

    def enroll(self, image_bytes_list, company_id=None, employee_code=""):
        self._fail()

    def verify(self, template_bytes_list, image_bytes, company_id=None):
        self._fail()

    def liveness(self, image_bytes):
        self._fail()

    def create_liveness_session(self, client_token):
        self._fail()

    def get_liveness_result(self, session_id):
        self._fail()

    def identify_faces(self, image_bytes, company_id=None, scope="students",
                       min_similarity=80.0, max_faces=100):
        self._fail()


def get_provider():
    from django.conf import settings as dj_settings

    name = str(
        getattr(dj_settings, "FACE_VERIFICATION_PROVIDER", "none") or "none"
    ).lower()
    if name == "aws":
        from .providers_aws import AWSRekognitionProvider

        return AWSRekognitionProvider()
    if name in ("mock", "sandbox", "test"):
        is_demo_or_test = (
            getattr(dj_settings, "DEBUG", False)
            or getattr(dj_settings, "ALLOW_MOCK_FACE_AUTH", False)
            or getattr(dj_settings, "TESTING", False)
        )
        if not is_demo_or_test:
            raise ProviderNotConfigured(
                "Mock face recognition provider is not permitted in production. "
                "Set FACE_VERIFICATION_PROVIDER=aws with valid AWS credentials."
            )
        return MockFaceProvider()
    if name == "none":
        return UnconfiguredProvider()
    raise ValueError(
        f"Unknown FACE_VERIFICATION_PROVIDER: {name!r}. "
        "Supported face verification providers: 'aws' (production) or 'mock' (tests/demo only)."
    )

