"""Group attendance decision logic.

`decide()` is PURE (no DB, no AWS): it turns the faces found in one or more
photos into a per-student result.  Keeping it pure makes the rules easy to
unit-test and to tune without touching the views.

Rules (the safety model for a class photo):
  * A face is auto-marked PRESENT only when ALL are true:
      - similarity >= FACE_GROUP_MATCH_THRESHOLD
      - the best match is a student on THIS class roster
      - no other person is within FACE_GROUP_AMBIGUITY_MARGIN of the score
      - no suspicion flag (phone over face, screen pattern, occlusion)
      - only ONE face in the frame claims that student
  * Anything weaker is REVIEW - the teacher decides before submitting.
  * Faces that match nobody on the roster are reported as unknown faces.
  * Students with no face on file / no guardian consent are never auto-marked.
"""
from collections import defaultdict

from django.conf import settings

PRESENT, ABSENT, REVIEW = "PRESENT", "ABSENT", "REVIEW"
NOT_ENROLLED, NO_CONSENT = "NOT_ENROLLED", "NO_CONSENT"
SCOPE = "students"          # provider collection scope for student faces


def get_cfg():
    g = lambda name, default: getattr(settings, name, default)
    return {
        "match_threshold": float(g("FACE_GROUP_MATCH_THRESHOLD", 92.0)),
        "review_threshold": float(g("FACE_GROUP_REVIEW_THRESHOLD", 80.0)),
        "ambiguity_margin": float(g("FACE_GROUP_AMBIGUITY_MARGIN", 3.0)),
        "min_detection_confidence": float(g("FACE_GROUP_MIN_DETECTION_CONF", 85.0)),
        "min_face_px": int(g("FACE_GROUP_MIN_FACE_PX", 40)),
        "min_brightness": float(g("FACE_GROUP_MIN_BRIGHTNESS", 20.0)),
        "min_sharpness": float(g("FACE_GROUP_MIN_SHARPNESS", 20.0)),
        "max_frames": int(g("FACE_GROUP_MAX_FRAMES", 5)),
        "max_image_bytes": int(g("FACE_GROUP_MAX_IMAGE_BYTES", 12 * 1024 * 1024)),
        "session_ttl": int(g("FACE_GROUP_SESSION_TTL", 900)),
        "max_capture_age": int(g("FACE_GROUP_MAX_CAPTURE_AGE", 120)),
        "replay_window_hours": int(g("FACE_GROUP_REPLAY_WINDOW_HOURS", 24)),
    }


def classify_observation(obs, owner_by_face, cfg):
    """One detected face -> {"kind": PRESENT|REVIEW|UNKNOWN, ...}.

    Safety rule: a face that matches an enrolled athlete on this roster is
    never dropped to UNKNOWN (which the caller turns into an absent candidate)
    just because the crop is small, dark or soft.  Degraded evidence can only
    hold a face back from auto-PRESENT, so the trainer decides instead of the
    athlete being silently reported as not seen.
    """
    matches = sorted(obs.get("matches") or [],
                     key=lambda m: m["similarity"], reverse=True)
    if not matches:
        return {"kind": "UNKNOWN", "reason": "NO_MATCH"}
    top = matches[0]
    owner = owner_by_face.get(top["face_id"])
    if owner is None:
        return {"kind": "UNKNOWN", "reason": "NOT_IN_ROSTER"}

    base = {"student_key": owner, "similarity": top["similarity"],
            "box": obs.get("box")}
    for other in matches[1:]:
        other_owner = owner_by_face.get(other["face_id"])
        if (other_owner is not None
                and other_owner != owner
                and top["similarity"] - other["similarity"] < cfg["ambiguity_margin"]):
            return {**base, "kind": REVIEW, "reason": "AMBIGUOUS"}
    if obs.get("flags"):
        return {**base, "kind": REVIEW, "reason": "SPOOF_SUSPECT",
                "flags": list(obs["flags"])}
    if top["similarity"] < cfg["review_threshold"]:
        # Not a confident claim on anyone: the athlete simply is not in frame.
        return {"kind": "UNKNOWN", "reason": "BELOW_THRESHOLD"}

    degraded = []
    if obs.get("search_error"):
        degraded.append("SEARCH_FAILED")
    if obs.get("detection_confidence", 0.0) < cfg["min_detection_confidence"]:
        degraded.append("LOW_DETECTION")
    px = obs.get("pixel_size")
    if px and min(px) < cfg["min_face_px"]:
        degraded.append("FACE_TOO_SMALL")
    q = obs.get("quality") or {}
    if (q.get("brightness", 100.0) < cfg["min_brightness"]
            or q.get("sharpness", 100.0) < cfg["min_sharpness"]):
        degraded.append("LOW_QUALITY")
    if degraded:
        return {**base, "kind": REVIEW, "reason": degraded[0]}
    if top["similarity"] >= cfg["match_threshold"]:
        return {**base, "kind": PRESENT, "reason": ""}
    return {**base, "kind": REVIEW, "reason": "LOW_CONFIDENCE"}


def decide(frames, roster, owner_by_face, enrolled_keys, consent_keys, cfg=None):
    """frames: list[list[observation]] (one list per uploaded photo).

    roster: [{"student_key","name"}]; owner_by_face: {face_id: student_key}
    (ONLY consented, enrolled students of this roster); enrolled_keys
    (active face on file) / consent_keys (guardian consent recorded): sets of student_key.
    """
    cfg = cfg or get_cfg()
    best = {}                 # student_key -> best claim across frames
    sticky_dup = set()        # students claimed twice in ONE frame
    unknown = []
    faces_detected = 0

    for fi, observations in enumerate(frames):
        faces_detected += len(observations)
        claims = defaultdict(list)
        for obs in observations:
            c = classify_observation(obs, owner_by_face, cfg)
            if c["kind"] == "UNKNOWN":
                unknown.append({"frame": fi, "box": obs.get("box"),
                                "reason_code": c["reason"]})
            else:
                claims[c["student_key"]].append(c)
        for key, cl in claims.items():
            cl.sort(key=lambda c: c["similarity"], reverse=True)
            chosen = dict(cl[0], frame=fi)
            if len(cl) > 1:
                # two faces, one student: one of them is a proxy / photo / twin
                sticky_dup.add(key)
                for extra in cl[1:]:
                    unknown.append({"frame": fi, "box": extra.get("box"),
                                    "reason_code": "DUPLICATE_CLAIM"})
            cur = best.get(key)
            rank = lambda c: (c["kind"] == PRESENT, c["similarity"])
            if cur is None or rank(chosen) > rank(cur):
                best[key] = chosen

    students = {}
    for r in roster:
        key = r["student_key"]
        row = {"student_key": key, "name": r["name"], "status": ABSENT,
               "similarity": None, "box": None, "reason_code": "NOT_SEEN"}
        if key not in enrolled_keys:
            row.update(status=NOT_ENROLLED, reason_code="NO_FACE_ON_FILE")
        elif key not in consent_keys:
            row.update(status=NO_CONSENT, reason_code="NO_GUARDIAN_CONSENT")
        else:
            cand = best.get(key)
            if cand is not None:
                row.update(similarity=cand["similarity"], box=cand["box"])
                if cand["kind"] == PRESENT and key not in sticky_dup:
                    row.update(status=PRESENT, reason_code="")
                else:
                    row.update(
                        status=REVIEW,
                        reason_code="DUPLICATE_CLAIM" if key in sticky_dup
                        else cand.get("reason") or "LOW_CONFIDENCE",
                    )
        students[key] = row
    return {"students": students, "unknown_faces": unknown,
            "faces_detected": faces_detected}


def summarize(rows, unknown_faces=0, faces_detected=0, frames=0):
    counts = defaultdict(int)
    manual = 0
    for r in rows:
        counts[r["status"]] += 1
        if r.get("method") == "MANUAL_OVERRIDE":
            manual += 1
    return {
        "roster": len(rows),
        "present": counts[PRESENT], "absent": counts[ABSENT],
        "review": counts[REVIEW], "not_enrolled": counts[NOT_ENROLLED],
        "no_consent": counts[NO_CONSENT], "manual_overrides": manual,
        "unknown_faces": unknown_faces, "faces_detected": faces_detected,
        "frames": frames,
    }
