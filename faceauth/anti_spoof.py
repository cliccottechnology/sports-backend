"""Advanced Multi-Layer Anti-Spoofing & Liveness Detection.

Protects against:
  1. Photo of another mobile phone or tablet held up to camera (replay attack)
  2. Printed paper / photograph spoofing
  3. Closed eyes, face occlusions, or multi-face spoofing

Techniques employed:
  - Layer 1: AWS Rekognition DetectLabels (detects second mobile phone or tablet in frame)
  - Layer 2: AWS Rekognition DetectFaces Quality & Eye Liveness Verification
  - Layer 3: 2D FFT Moiré Pattern Analysis (detects LCD/OLED subpixel grid interference)
"""
import io
import logging
import numpy as np
from PIL import Image, ImageOps

logger = logging.getLogger(__name__)

# Physical handheld mobile devices used to spoof camera (holding another phone/tablet in front of face).
# Background office items (monitors, laptops, wall posters, ID badges) are deliberately excluded
# so employees taking attendance at their office desks or in meeting rooms are never falsely rejected.
SPOOF_DEVICE_LABELS = {
    "cell phone",
    "mobile phone",
    "smartphone",
    "tablet computer",
    "ipad",
}

# Explicitly benign labels that must NEVER trigger a spoof detection
BENIGN_LABELS = {
    "photography", "photo", "photograph", "picture", "portrait",
    "person", "human", "face", "head", "skin", "selfie", "smile",
    "clothing", "apparel", "hair", "forehead", "chin", "cheek",
    "nose", "mouth", "lip", "jaw", "neck", "collar", "screen",
    "monitor", "display", "id card", "badge", "poster", "room",
    "indoors", "office", "wall", "interior design"
}


def _check_screen_moire_and_frequency(gray_arr):
    """Analyze 2D Fourier power spectrum for Moiré subpixel screen grid patterns.
    
    Human skin has natural smooth 1/f^2 energy falloff without isolated frequency delta peaks.
    Digital screens produce characteristic periodic high-frequency Moiré peaks.
    """
    try:
        h, w = gray_arr.shape
        if h < 64 or w < 64:
            return False, 0.0, "image too small"

        # Center crop to power-of-two size (up to 256x256) for reliable FFT
        size = min(h, w, 256)
        y0 = (h - size) // 2
        x0 = (w - size) // 2
        crop = gray_arr[y0:y0 + size, x0:x0 + size].astype(np.float32)

        # Apply Hanning window to reduce spectral leakage at borders
        hann_2d = np.outer(np.hanning(size), np.hanning(size))
        windowed = crop * hann_2d

        # 2D Fast Fourier Transform
        f = np.fft.fft2(windowed)
        fshift = np.fft.fftshift(f)
        magnitude = np.abs(fshift)

        cy, cx = size // 2, size // 2
        y, x = np.ogrid[:size, :size]
        dist_sq = (y - cy) ** 2 + (x - cx) ** 2

        # Radial zones: High frequency (> 1/4 size)
        r_mid = (size // 4) ** 2
        high_mask = dist_sq > r_mid

        high_mag = magnitude[high_mask]
        if len(high_mag) == 0:
            return False, 0.0, "no high freq"

        total_energy = float(np.sum(magnitude)) + 1e-6
        high_energy = float(np.sum(magnitude[high_mask]))
        high_ratio = high_energy / total_energy

        mean_high = float(np.mean(high_mag))
        max_high = float(np.max(high_mag))
        spike_ratio = max_high / (mean_high + 1e-6)

        # True Moiré screen condition:
        # A real electronic screen grid produces strong high-frequency energy (mean_high > 60 and high_ratio > 0.30)
        # AND a concentrated harmonic spike (spike_ratio > 35.0).
        # Smooth skin or natural camera lighting produces low mean_high (< 10), which will never trigger.
        is_screen = (mean_high > 60.0) and (high_ratio > 0.30) and (spike_ratio > 35.0)
        score = float(spike_ratio)
        return is_screen, score, f"spike={spike_ratio:.1f}, high_ratio={high_ratio:.3f}, mean={mean_high:.1f}"
    except Exception as exc:
        logger.warning("Moiré frequency check failed: %s", exc)
        return False, 0.0, str(exc)


def verify_live_camera_capture(image_bytes, aws_client=None):
    """Perform multi-layer anti-spoofing and liveness check.
    
    Protects against:
      - Holding up another mobile phone or tablet showing a photo
      - Pointing camera at electronic screen replay
      - Closed eyes, face occlusions, or multi-face spoofing

    Returns:
        (is_live: bool, reason_code: str, message: str)
    """
    if not image_bytes or len(image_bytes) < 100:
        return False, "LIVENESS_FAIL", "LIVENESS_FAIL: Empty or invalid camera capture."

    # Parse and normalize image
    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            img = ImageOps.exif_transpose(img)
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            rgb_arr = np.array(img.convert("RGB"))
            gray_arr = np.array(img.convert("L"))
    except Exception as exc:
        logger.warning("Failed to open image for anti-spoof check: %s", exc)
        return False, "LIVENESS_FAIL", "LIVENESS_FAIL: Invalid image format. Please capture with live camera."

    # -------------------------------------------------------------
    # Layer 1: AWS Rekognition Object / Screen Device Spoof Detection
    # -------------------------------------------------------------
    if aws_client is not None:
        try:
            label_resp = aws_client.detect_labels(
                Image={"Bytes": image_bytes},
                MaxLabels=30,
                MinConfidence=70.0,
            )
            detected_labels = label_resp.get("Labels", [])
            for lab in detected_labels:
                name = lab.get("Name", "").strip().lower()
                conf = float(lab.get("Confidence", 0.0))
                if conf < 70.0:
                    continue

                parent_names = [p.get("Name", "").strip().lower() for p in lab.get("Parents", [])]
                all_names = [name] + parent_names

                # Check against physical screen spoof devices, ignoring benign human/photo labels
                for target_name in all_names:
                    if target_name in BENIGN_LABELS:
                        continue
                    if target_name in SPOOF_DEVICE_LABELS:
                        logger.warning(
                            "Anti-spoof: detected physical screen device '%s' matching '%s' (conf=%.1f%%)",
                            lab.get("Name"), target_name, conf
                        )
                        return (
                            False,
                            "LIVENESS_FAIL",
                            f"LIVENESS_FAIL: Spoofing or photo of screen detected ({lab.get('Name')}). Only live camera image accepted.",
                        )
        except Exception as exc:
            logger.warning("AWS detect_labels call skipped: %s", exc)

    # -------------------------------------------------------------
    # Layer 2: AWS Rekognition DetectFaces Quality & Human Attributes
    # -------------------------------------------------------------
    if aws_client is not None:
        try:
            face_resp = aws_client.detect_faces(
                Image={"Bytes": image_bytes},
                Attributes=["ALL"],
            )
            faces = face_resp.get("FaceDetails", [])
            if len(faces) == 0:
                return (
                    False,
                    "LIVENESS_FAIL",
                    "LIVENESS_FAIL: No human face detected. Please position your face clearly in the camera.",
                )
            if len(faces) > 1:
                return (
                    False,
                    "LIVENESS_FAIL",
                    "LIVENESS_FAIL: Multiple faces detected. Only one live person must be in frame.",
                )

            face = faces[0]
            conf = float(face.get("Confidence", 0.0))
            if conf < 80.0:
                return (
                    False,
                    "LIVENESS_FAIL",
                    "LIVENESS_FAIL: Low face detection confidence. Please hold camera steady in good lighting.",
                )

            quality = face.get("Quality", {})
            brightness = float(quality.get("Brightness", 0.0))
            sharpness = float(quality.get("Sharpness", 0.0))
            if brightness < 15.0 or sharpness < 15.0:
                return (
                    False,
                    "LIVENESS_FAIL",
                    "LIVENESS_FAIL: Image is too dark or blurry. Please capture with adequate lighting.",
                )

            # Eyes must be open on a live camera selfie
            eyes_open = face.get("EyesOpen", {})
            if eyes_open.get("Value") is False and float(eyes_open.get("Confidence", 0.0)) > 88.0:
                return (
                    False,
                    "LIVENESS_FAIL",
                    "LIVENESS_FAIL: Eyes appear closed. Please look directly into the camera.",
                )

            # Face occlusion check
            occluded = face.get("FaceOccluded", {})
            if occluded.get("Value") is True and float(occluded.get("Confidence", 0.0)) > 88.0:
                return (
                    False,
                    "LIVENESS_FAIL",
                    "LIVENESS_FAIL: Face is partially covered or occluded. Please remove any obstruction.",
                )
        except Exception as exc:
            logger.warning("AWS detect_faces quality gate error: %s", exc)

    # -------------------------------------------------------------
    # Layer 3: 2D FFT Moiré Pattern & Subpixel Grid Analysis
    # -------------------------------------------------------------
    is_moire, moire_score, moire_msg = _check_screen_moire_and_frequency(gray_arr)
    if is_moire:
        logger.warning("Anti-spoof: Moiré pattern detected (%s)", moire_msg)
        return (
            False,
            "LIVENESS_FAIL",
            "LIVENESS_FAIL: Spoofing or photo of screen detected (Moiré screen grid pattern). Only live camera image accepted.",
        )

    return True, "MATCH_OK", "Live camera image verified."


# ======================================================================
# Group-photo helpers (teacher app: many faces in ONE capture)
# ----------------------------------------------------------------------
# verify_live_camera_capture() above intentionally rejects frames with more
# than one face, so it cannot be used for a class photo.  Per-person liveness
# (Face Liveness video) is impossible for a group, so the group flow uses
# per-face *suspicion flags*.  A flagged face is NEVER auto-marked present -
# it goes to the teacher's review list.
# ======================================================================
FLAG_DEVICE_OVER_FACE = "DEVICE_OVER_FACE"   # phone/tablet covers the face
FLAG_SCREEN_PATTERN = "SCREEN_PATTERN"       # Moire grid => photo of a screen
FLAG_OCCLUDED = "OCCLUDED"                   # mask / hand / object on the face


def box_overlap_ratio(face_box, other_box):
    """Share of `face_box` area covered by `other_box` (both normalised)."""
    fx1, fy1 = face_box["left"], face_box["top"]
    fx2, fy2 = fx1 + face_box["width"], fy1 + face_box["height"]
    ox1, oy1 = other_box["left"], other_box["top"]
    ox2, oy2 = ox1 + other_box["width"], oy1 + other_box["height"]
    iw = max(0.0, min(fx2, ox2) - max(fx1, ox1))
    ih = max(0.0, min(fy2, oy2) - max(fy1, oy1))
    area = max(face_box["width"] * face_box["height"], 1e-9)
    return (iw * ih) / area


def device_boxes_from_labels(label_resp, min_conf=80.0):
    """Bounding boxes of phones/tablets found by Rekognition DetectLabels."""
    boxes = []
    for lab in (label_resp or {}).get("Labels", []):
        names = [lab.get("Name", "").strip().lower()] + [
            p.get("Name", "").strip().lower() for p in lab.get("Parents", [])
        ]
        if not any(n in SPOOF_DEVICE_LABELS for n in names):
            continue
        for inst in lab.get("Instances", []):
            if float(inst.get("Confidence", 0.0)) < min_conf:
                continue
            b = inst.get("BoundingBox") or {}
            boxes.append({
                "left": float(b.get("Left", 0)), "top": float(b.get("Top", 0)),
                "width": float(b.get("Width", 0)), "height": float(b.get("Height", 0)),
            })
    return boxes


def assess_group_face(crop_gray_arr, face_box, device_boxes, face_detail=None):
    """Return the list of suspicion flags for ONE face of a group photo."""
    flags = []
    for d in device_boxes or []:
        if box_overlap_ratio(face_box, d) >= 0.30:
            flags.append(FLAG_DEVICE_OVER_FACE)
            break
    if crop_gray_arr is not None:
        is_screen, _score, _msg = _check_screen_moire_and_frequency(crop_gray_arr)
        if is_screen:
            flags.append(FLAG_SCREEN_PATTERN)
    occ = (face_detail or {}).get("FaceOccluded") or {}
    if occ.get("Value") is True and float(occ.get("Confidence", 0.0)) > 88.0:
        flags.append(FLAG_OCCLUDED)
    return flags
