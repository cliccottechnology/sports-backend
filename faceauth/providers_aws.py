"""AWS Rekognition provider (face match + Face Liveness).

Environment / settings
----------------------
  FACE_VERIFICATION_PROVIDER=aws
  AWS_REGION=ap-south-1                 collection region (match/index/search)
  AWS_LIVENESS_REGION=ap-south-1        Face Liveness region (must be a region
                                        where Face Liveness is available; defaults
                                        to AWS_REGION)
  AWS_REKOGNITION_COLLECTION_PREFIX=cliccot   -> one collection per tenant
  FACE_MATCH_THRESHOLD=90
  FACE_LIVENESS_MIN_CONFIDENCE=90
  AWS_LIVENESS_ROLE_ARN=arn:aws:iam::<acct>:role/<role>   (optional, see below)
  AWS_LIVENESS_S3_BUCKET=                (optional, only if reference image is
                                          not returned inline)

Why Face Liveness
-----------------
A still photo can never prove a person is live. Face Liveness streams a short
selfie video from the device camera to AWS, shows a colour-light challenge on
the screen and returns a confidence score. Printed photos, photos/videos shown
on another phone or monitor, and replayed recordings fail the challenge. The
backend never trusts anything sent by the app except its own session token:
the result is read from AWS directly with GetFaceLivenessSessionResults.

Device credentials
------------------
The app streams video straight to Rekognition, so it needs temporary AWS
credentials that can ONLY call rekognition:StartFaceLivenessSession.
  * If AWS_LIVENESS_ROLE_ARN is set, this backend issues 15-minute STS
    credentials for that role (issue_client_credentials).
  * Otherwise return None and let the mobile SDK get credentials from a Cognito
    identity pool (AWS' reference setup).
"""
import logging

from .providers import FaceProvider, ProviderNotConfigured

logger = logging.getLogger(__name__)

MIN_BRIGHTNESS = 30
MIN_SHARPNESS = 30
AMBIGUITY_MARGIN = 3.0


def _setting(name, default=None):
    from django.conf import settings as dj_settings

    return getattr(dj_settings, name, default)


def _normalize_image_bytes(image_bytes):
    try:
        import io
        from PIL import Image, ImageOps
        with Image.open(io.BytesIO(image_bytes)) as img:
            img = ImageOps.exif_transpose(img)
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            out = io.BytesIO()
            img.save(out, format="JPEG", quality=95)
            return out.getvalue()
    except Exception:
        return image_bytes


class AWSRekognitionProvider(FaceProvider):
    name = "aws"

    # ------------------------------------------------------------------
    # clients / config
    # ------------------------------------------------------------------
    def _boto3(self):
        try:
            import boto3
        except ImportError as exc:
            raise ProviderNotConfigured(
                "boto3 is not installed. Run: pip install boto3"
            ) from exc
        return boto3

    def _region(self):
        return _setting("AWS_REGION", "ap-south-1") or "ap-south-1"

    def _liveness_region(self):
        return _setting("AWS_LIVENESS_REGION", "") or self._region()

    def _client(self):
        return self._boto3().client("rekognition", region_name=self._region())

    def _liveness_client(self):
        return self._boto3().client(
            "rekognition", region_name=self._liveness_region()
        )

    def _collection(self, company_id, scope=""):
        prefix = (
            _setting("AWS_REKOGNITION_COLLECTION_PREFIX", "")
            or _setting("FACE_REKOGNITION_COLLECTION_ID", "cliccot")
            or "cliccot"
        ).strip().rstrip("-")
        if scope:
            # Separate collection (e.g. cliccot-students-7) so a class photo can
            # never match staff/teacher faces and vice-versa.
            return f"{prefix}-{scope}-{company_id}" if company_id else f"{prefix}-{scope}"
        return f"{prefix}-{company_id}" if company_id else prefix

    def _threshold(self):
        try:
            return float(_setting("FACE_MATCH_THRESHOLD", 90) or 90)
        except (TypeError, ValueError):
            return 90.0

    # ------------------------------------------------------------------
    # quality gate
    # ------------------------------------------------------------------
    def _single_face(self, client, image_bytes):
        """DetectFaces quality gate: exactly one bright, sharp face."""
        resp = client.detect_faces(
            Image={"Bytes": image_bytes}, Attributes=["DEFAULT"]
        )
        faces = resp.get("FaceDetails", [])
        if len(faces) != 1:
            raise ValueError(
                "Please capture a clear selfie containing only your face."
            )
        quality = faces[0].get("Quality", {})
        if (
            quality.get("Brightness", 0) < MIN_BRIGHTNESS
            or quality.get("Sharpness", 0) < MIN_SHARPNESS
        ):
            raise ValueError(
                "Image quality is too low. Please try again with better lighting."
            )
        return faces[0]

    # ------------------------------------------------------------------
    # Face Liveness
    # ------------------------------------------------------------------
    def create_liveness_session(self, client_token):
        client = self._liveness_client()
        settings = {"AuditImagesLimit": int(_setting("FACE_LIVENESS_AUDIT_IMAGES", 0) or 0)}
        bucket = _setting("AWS_LIVENESS_S3_BUCKET", "")
        if bucket:
            settings["OutputConfig"] = {
                "S3Bucket": bucket,
                "S3KeyPrefix": "liveness/",
            }
        resp = client.create_face_liveness_session(
            Settings=settings,
            # Idempotency key: retries of the same call never create a 2nd session.
            ClientRequestToken=client_token[:64],
        )
        return {
            "session_id": resp["SessionId"],
            "region": self._liveness_region(),
        }

    def get_liveness_result(self, session_id):
        client = self._liveness_client()
        resp = client.get_face_liveness_session_results(SessionId=session_id)
        ref = resp.get("ReferenceImage") or {}
        image = ref.get("Bytes")
        if not image and ref.get("S3Object"):
            obj = ref["S3Object"]
            s3 = self._boto3().client("s3", region_name=self._liveness_region())
            image = s3.get_object(Bucket=obj["Bucket"], Key=obj["Name"])["Body"].read()
        return {
            "status": resp.get("Status", ""),
            "confidence": float(resp.get("Confidence") or 0.0),
            "reference_image": bytes(image) if image else None,
        }

    def issue_client_credentials(self, user_id):
        """15-minute credentials that can only start a liveness stream."""
        role_arn = _setting("AWS_LIVENESS_ROLE_ARN", "")
        if not role_arn:
            return None
        sts = self._boto3().client("sts", region_name=self._liveness_region())
        resp = sts.assume_role(
            RoleArn=role_arn,
            RoleSessionName=f"liveness-{user_id}"[:64],
            DurationSeconds=900,
        )
        c = resp["Credentials"]
        return {
            "access_key_id": c["AccessKeyId"],
            "secret_access_key": c["SecretAccessKey"],
            "session_token": c["SessionToken"],
            "expiration": c["Expiration"].isoformat(),
        }

    def liveness(self, image_bytes):
        from .anti_spoof import verify_live_camera_capture
        client = self._client()
        ok, reason, msg = verify_live_camera_capture(image_bytes, aws_client=client)
        if not ok:
            logger.info("Still-image liveness failed: %s (%s)", reason, msg)
            return False
        return True

    # ------------------------------------------------------------------
    # enroll / verify / purge
    # ------------------------------------------------------------------
    def find_face_ids(self, image_bytes, company_id=None, scope=""):
        """Return [(FaceId, similarity)] already in the tenant collection that
        look like this face (used to stop one person enrolling twice)."""
        client = self._client()
        if scope:
            try:
                resp = client.search_faces_by_image(
                    CollectionId=self._collection(company_id, scope),
                    Image={"Bytes": image_bytes}, MaxFaces=5,
                    FaceMatchThreshold=self._threshold(), QualityFilter="AUTO",
                )
                return [(m["Face"]["FaceId"], float(m.get("Similarity", 0.0)))
                        for m in resp.get("FaceMatches", [])]
            except Exception:
                return []
        cols_to_try = [self._collection(company_id)]
        fallback_col = f"cliccot-{company_id}" if company_id else "cliccot"
        if fallback_col not in cols_to_try:
            cols_to_try.append(fallback_col)
        alt_col = f"cliccot-faces-{company_id}" if company_id else "cliccot-faces"
        if alt_col not in cols_to_try:
            cols_to_try.append(alt_col)

        for col in cols_to_try:
            try:
                resp = client.search_faces_by_image(
                    CollectionId=col,
                    Image={"Bytes": image_bytes},
                    MaxFaces=5,
                    FaceMatchThreshold=self._threshold(),
                    QualityFilter="AUTO",
                )
                matches = resp.get("FaceMatches", [])
                if matches:
                    return [
                        (m["Face"]["FaceId"], float(m.get("Similarity", 0.0)))
                        for m in matches
                    ]
            except Exception:
                continue
        return []

    def _ensure_collection(self, client, collection):
        """Create the collection only when it is genuinely missing.

        ``describe_collection`` can fail transiently (throttling, a blip); the
        old blanket ``except Exception`` treated every error as "missing" and
        then blew up on ``ResourceAlreadyExistsException``. We now create only
        on a real not-found and swallow a concurrent/duplicate create so one
        racy photo can never abort an enrollment run.
        """
        not_found = getattr(client.exceptions, "ResourceNotFoundException", ())
        already = getattr(client.exceptions, "ResourceAlreadyExistsException", ())
        try:
            client.describe_collection(CollectionId=collection)
            return
        except not_found:
            pass
        except Exception:  # transient describe error: try to create, tolerate race
            pass
        try:
            client.create_collection(CollectionId=collection)
        except already:
            pass

    def ping(self):
        """Lightweight auth/connectivity probe. Returns the collection ids."""
        client = self._client()
        return list(client.list_collections(MaxResults=100).get("CollectionIds", []))

    def enroll(self, image_bytes_list, company_id=None, employee_code="", scope=""):
        client = self._client()
        collection = self._collection(company_id, scope)
        self._ensure_collection(client, collection)
        refs = []
        for i, blob in enumerate(image_bytes_list):
            self._single_face(client, blob)
            resp = client.index_faces(
                CollectionId=collection,
                Image={"Bytes": blob},
                ExternalImageId=f"{employee_code}-{i}"[:255],
                MaxFaces=1,
                QualityFilter="AUTO",
                DetectionAttributes=["DEFAULT"],
            )
            records = resp.get("FaceRecords", [])
            if len(records) != 1:
                raise ValueError("The face could not be registered. Please try again.")
            refs.append(records[0]["Face"]["FaceId"].encode())
        return refs

    def verify(self, template_bytes_list, image_bytes, company_id=None,
               quality_gate=True):
        """1:1 check - the best match in the tenant collection must be one of
        THIS employee's enrolled faces, and not ambiguous with someone else."""
        client = self._client()
        image_bytes = _normalize_image_bytes(image_bytes)
        if quality_gate:
            self._single_face(client, image_bytes)

        cols_to_try = [self._collection(company_id)]
        fallback_col = f"cliccot-{company_id}" if company_id else "cliccot"
        if fallback_col not in cols_to_try:
            cols_to_try.append(fallback_col)
        alt_col = f"cliccot-faces-{company_id}" if company_id else "cliccot-faces"
        if alt_col not in cols_to_try:
            cols_to_try.append(alt_col)

        matches = []
        for col in cols_to_try:
            try:
                resp = client.search_faces_by_image(
                    CollectionId=col,
                    Image={"Bytes": image_bytes},
                    MaxFaces=5,
                    FaceMatchThreshold=self._threshold(),
                    QualityFilter="AUTO",
                )
                m = resp.get("FaceMatches", [])
                if m:
                    matches = m
                    break
            except client.exceptions.InvalidParameterException as exc:
                logger.warning("AWS SearchFacesByImage rejected image in %s: %s", col, exc)
                continue
            except client.exceptions.ResourceNotFoundException:
                continue
            except Exception as exc:
                logger.warning("AWS SearchFacesByImage failed in %s: %s", col, exc)
                continue

        if not matches:
            return False, 0.0

        wanted = set()
        for b in template_bytes_list:
            try:
                wanted.add(b.decode().strip())
            except Exception:
                continue

        # Sort matches by similarity descending
        matches.sort(key=lambda x: float(x.get("Similarity", 0.0)), reverse=True)

        top = matches[0]
        top_face_id = top["Face"]["FaceId"]
        top_similarity = float(top.get("Similarity", 0.0))

        # Check if the top match is one of THIS employee's enrolled faces
        if top_face_id not in wanted:
            logger.info(
                "Face match rejected: top face %s not in enrolled templates for employee",
                top_face_id,
            )
            return False, top_similarity

        # Ambiguity check: ONLY trigger if there is another matching face belonging to
        # a DIFFERENT employee, and that other person's score is within AMBIGUITY_MARGIN.
        # Matches belonging to the SAME employee (multiple enrolled angles) are NOT ambiguous.
        other_matches = [
            m for m in matches[1:]
            if m["Face"]["FaceId"] not in wanted
        ]
        if other_matches:
            other_top = float(other_matches[0].get("Similarity", 0.0))
            if (top_similarity - other_top) < AMBIGUITY_MARGIN:
                logger.warning(
                    "Face match rejected as ambiguous with another person: "
                    "enrolled=%.2f vs other=%.2f (margin=%.2f)",
                    top_similarity, other_top, AMBIGUITY_MARGIN,
                )
                return False, top_similarity

        return True, top_similarity

    def delete_faces(self, face_ids, company_id=None, scope=""):
        """Purge enrolled faces (deactivation / revocation sync)."""
        ids = [f.decode() if isinstance(f, bytes) else str(f) for f in face_ids if f]
        if not ids:
            return 0
        client = self._client()
        cols = [self._collection(company_id, scope)]
        fallback_col = f"cliccot-{company_id}" if company_id else "cliccot"
        if not scope and fallback_col not in cols:
            cols.append(fallback_col)
        deleted = 0
        for col in cols:
            try:
                resp = client.delete_faces(CollectionId=col, FaceIds=ids)
                deleted += len(resp.get("DeletedFaces", []))
            except Exception:
                pass
        return deleted

    # ------------------------------------------------------------------
    # GROUP identification: many faces, ONE photo (teacher app)
    # ------------------------------------------------------------------
    def identify_faces(self, image_bytes, company_id=None, scope="students",
                       min_similarity=80.0, max_faces=100):
        """DetectFaces -> crop every face -> SearchFacesByImage per crop.

        SearchFacesByImage only ever searches the LARGEST face in an image, so
        a group photo must be cut into one crop per face.  Crops are searched in
        parallel.  Per-face suspicion flags (phone over face, screen pattern,
        occlusion) are attached; the caller decides what to do with them.
        """
        import concurrent.futures
        import io
        import time

        import numpy as np
        from PIL import Image

        from .anti_spoof import (
            FLAG_OCCLUDED, assess_group_face, device_boxes_from_labels,
        )

        client = self._client()
        prepared = _prepare_group_image(image_bytes)
        img = Image.open(io.BytesIO(prepared)).convert("RGB")
        W, H = img.size

        raw_faces = client.detect_faces(
            Image={"Bytes": prepared}, Attributes=["ALL"]
        ).get("FaceDetails", [])

        # Strict validation of detected faces:
        # Prevents false-positive detections from objects, electrical sockets,
        # screws, wall artifacts, and blurred reflections on curved bottles/flasks.
        valid_faces = []
        for face in raw_faces:
            conf = float(face.get("Confidence", 0.0))
            if conf < 85.0:
                continue

            bb = face.get("BoundingBox", {})
            w_px = int(bb.get("Width", 0.0) * W)
            h_px = int(bb.get("Height", 0.0) * H)
            if w_px < 40 or h_px < 40:
                continue

            landmarks = {lm.get("Type"): lm for lm in face.get("Landmarks", [])}
            has_eyes = "eyeLeft" in landmarks and "eyeRight" in landmarks
            has_nose = "nose" in landmarks
            has_mouth = "mouthLeft" in landmarks or "mouthRight" in landmarks
            if not (has_eyes and has_nose and has_mouth):
                continue

            q = face.get("Quality", {})
            if float(q.get("Sharpness", 100.0)) < 15.0:
                continue

            valid_faces.append(face)

        faces = sorted(
            valid_faces, key=lambda f: f["BoundingBox"]["Width"] * f["BoundingBox"]["Height"],
            reverse=True,
        )[:max_faces]
        if not faces:
            return []

        device_boxes = []
        try:
            device_boxes = device_boxes_from_labels(
                client.detect_labels(
                    Image={"Bytes": prepared}, MaxLabels=40, MinConfidence=70.0
                )
            )
        except Exception as exc:  # label check is best-effort
            logger.warning("Group DetectLabels skipped: %s", exc)

        collection = self._collection(company_id, scope)
        not_found = getattr(client.exceptions, "ResourceNotFoundException", ())

        def work(face):
            bb = face["BoundingBox"]
            box = {"left": max(0.0, bb["Left"]), "top": max(0.0, bb["Top"]),
                   "width": bb["Width"], "height": bb["Height"]}
            pad_x, pad_y = box["width"] * 0.25, box["height"] * 0.25
            x1 = int(max(0.0, box["left"] - pad_x) * W)
            y1 = int(max(0.0, box["top"] - pad_y) * H)
            x2 = int(min(1.0, box["left"] + box["width"] + pad_x) * W)
            y2 = int(min(1.0, box["top"] + box["height"] + pad_y) * H)
            crop = img.crop((x1, y1, x2, y2))
            buf = io.BytesIO()
            crop.save(buf, format="JPEG", quality=92)
            gray = np.array(crop.convert("L"))
            flags = assess_group_face(gray, box, device_boxes, face)
            matches = []
            search_error = False
            # A group photo is ~100 parallel searches: Rekognition occasionally
            # returns ProvisionedThroughputExceeded.  Silently swallowing it
            # would turn a present athlete into an absent one, so each face
            # backs off and retries before we give up on it.
            for attempt in range(4):
                try:
                    resp = client.search_faces_by_image(
                        CollectionId=collection,
                        Image={"Bytes": buf.getvalue()},
                        MaxFaces=5,
                        FaceMatchThreshold=float(min_similarity),
                        QualityFilter="AUTO",
                    )
                    matches = sorted(
                        ({"face_id": m["Face"]["FaceId"],
                          "similarity": float(m.get("Similarity", 0.0))}
                         for m in resp.get("FaceMatches", [])),
                        key=lambda m: m["similarity"], reverse=True,
                    )
                    break
                except not_found:
                    break
                except Exception as exc:
                    text = str(exc)
                    transient = any(
                        token in text for token in
                        ("ProvisionedThroughput", "Throttl",
                         "LimitExceeded", "ServiceUnavailable", "Internal")
                    )
                    if transient and attempt < 3:
                        time.sleep(0.5 * (2 ** attempt))
                        continue
                    logger.warning("Group SearchFacesByImage failed: %s", exc)
                    search_error = True
                    break
            q = face.get("Quality", {})
            return {
                "box": box,
                "detection_confidence": float(face.get("Confidence", 0.0)),
                "quality": {"brightness": float(q.get("Brightness", 0.0)),
                            "sharpness": float(q.get("Sharpness", 0.0))},
                "flags": flags,
                "matches": matches,
                "search_error": search_error,
                "pixel_size": [int(box["width"] * W), int(box["height"] * H)],
            }

        # 4 workers keeps us under the per-account SearchFacesByImage rate
        # limit for a 100-face photo (8 triggered ProvisionedThroughput errors).
        workers = max(1, min(4, len(faces)))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(work, faces))


def _prepare_group_image(image_bytes, max_side=4096, max_bytes=4_500_000):
    """EXIF-rotate + downscale so Rekognition (5 MB Bytes limit) accepts it
    while keeping enough pixels for small faces at the back of a classroom.

    Resolution matters twice: it decides whether a face is recognised at all,
    and Rekognition's Sharpness metric is measured on this image, so an
    over-aggressive downscale silently fails the group quality gate for a
    perfectly good photo.
    """
    import io

    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(image_bytes)) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        if max(im.size) > max_side:
            im.thumbnail((max_side, max_side))
        quality = 92
        while True:
            out = io.BytesIO()
            im.save(out, format="JPEG", quality=quality)
            if out.tell() <= max_bytes or quality <= 60:
                return out.getvalue()
            quality -= 8
