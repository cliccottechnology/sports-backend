"""Group photo decision rules.

The safety property under test: an enrolled athlete whose face is recognised
must never end up as an absent candidate just because the crop is soft, small
or the recognition service rate-limited that one search.  Degraded evidence
only ever holds a face back from auto-PRESENT, so the trainer decides.
"""
import pytest

from faceauth.group_service import classify_observation, decide, get_cfg

CFG = {**get_cfg(),
       "match_threshold": 90.0, "review_threshold": 80.0,
       "ambiguity_margin": 3.0}


def obs(matches=None, sharpness=50.0, brightness=70.0, det=99.0,
        px=(90, 110), flags=None, search_error=False, box=None):
    o = {
        "box": box or {"left": 0.1, "top": 0.1, "width": 0.1, "height": 0.12},
        "detection_confidence": det,
        "quality": {"brightness": brightness, "sharpness": sharpness},
        "flags": list(flags or []),
        "matches": list(matches or []),
        "pixel_size": list(px),
    }
    if search_error:
        o["search_error"] = True
    return o


def test_soft_face_that_matched_is_review_not_absent():
    # p50 sharpness for a downscaled class photo is ~4 — must not mean absent.
    got = classify_observation(
        obs(matches=[{"face_id": "F1", "similarity": 100.0}], sharpness=4.4),
        {"F1": "1"}, CFG)
    assert got["kind"] == "REVIEW"
    assert got["reason"] == "LOW_QUALITY"
    assert got["student_key"] == "1"


def test_clean_face_auto_presents():
    got = classify_observation(
        obs(matches=[{"face_id": "F1", "similarity": 99.0}]), {"F1": "1"}, CFG)
    assert got["kind"] == "PRESENT"


def test_tiny_face_that_matched_is_review():
    got = classify_observation(
        obs(matches=[{"face_id": "F1", "similarity": 97.0}], px=(25, 30)),
        {"F1": "1"}, CFG)
    assert (got["kind"], got["reason"]) == ("REVIEW", "FACE_TOO_SMALL")


def test_dark_face_that_matched_is_review():
    got = classify_observation(
        obs(matches=[{"face_id": "F1", "similarity": 97.0}], brightness=12.0),
        {"F1": "1"}, CFG)
    assert (got["kind"], got["reason"]) == ("REVIEW", "LOW_QUALITY")


def test_search_failure_that_matched_is_review():
    got = classify_observation(
        obs(matches=[{"face_id": "F1", "similarity": 95.0}], search_error=True),
        {"F1": "1"}, CFG)
    assert (got["kind"], got["reason"]) == ("REVIEW", "SEARCH_FAILED")


def test_no_matches_is_unknown():
    got = classify_observation(obs(matches=[]), {"F1": "1"}, CFG)
    assert got["kind"] == "UNKNOWN"


def test_match_outside_roster_is_unknown():
    got = classify_observation(
        obs(matches=[{"face_id": "OTHER", "similarity": 99.0}]), {"F1": "1"}, CFG)
    assert (got["kind"], got["reason"]) == ("UNKNOWN", "NOT_IN_ROSTER")


def test_weak_match_below_review_threshold_is_unknown():
    # Not confident enough to claim the athlete is in frame at all.
    got = classify_observation(
        obs(matches=[{"face_id": "F1", "similarity": 71.0}]), {"F1": "1"}, CFG)
    assert (got["kind"], got["reason"]) == ("UNKNOWN", "BELOW_THRESHOLD")


def test_rival_within_margin_is_ambiguous():
    got = classify_observation(
        obs(matches=[{"face_id": "F1", "similarity": 95.0},
                     {"face_id": "F2", "similarity": 94.0}]),
        {"F1": "1", "F2": "2"}, CFG)
    assert (got["kind"], got["reason"]) == ("REVIEW", "AMBIGUOUS")


def test_flagged_face_is_review_even_when_confident():
    got = classify_observation(
        obs(matches=[{"face_id": "F1", "similarity": 99.0}],
            flags=["SCREEN_PATTERN"]), {"F1": "1"}, CFG)
    assert got["kind"] == "REVIEW"


ROSTER = [{"student_key": "1", "name": "Asha Dev"},
          {"student_key": "2", "name": "Bunny AA"},
          {"student_key": "3", "name": "Utkarsh TEST"}]
OWNERS = {"F1": "1", "F2": "2"}
ENROLLED = {"1", "2"}


def run(frames):
    return decide(frames, ROSTER, OWNERS, ENROLLED, set(ENROLLED), cfg=CFG)


def test_degraded_match_becomes_review_not_absent():
    result = run([[obs(matches=[{"face_id": "F1", "similarity": 100.0}],
                       sharpness=4.4)]])
    assert result["students"]["1"]["status"] == "REVIEW"
    assert result["students"]["1"]["reason_code"] == "LOW_QUALITY"
    # nobody is claimed, so the other enrolled athlete stays an absent candidate
    assert result["students"]["2"]["status"] == "ABSENT"
    assert result["students"]["2"]["reason_code"] == "NOT_SEEN"


def test_confident_clean_face_is_present():
    result = run([[obs(matches=[{"face_id": "F1", "similarity": 99.5}]),
                   obs(matches=[{"face_id": "F2", "similarity": 97.0}])]])
    assert result["students"]["1"]["status"] == "PRESENT"
    assert result["students"]["2"]["status"] == "PRESENT"
    assert result["unknown_faces"] == []


def test_student_without_face_is_absent_candidate():
    result = run([[obs(), obs(matches=[{"face_id": "F1", "similarity": 99.5}])]])
    assert result["students"]["2"]["status"] == "ABSENT"
    assert result["students"]["2"]["reason_code"] == "NOT_SEEN"
    assert len(result["unknown_faces"]) == 1
    assert result["unknown_faces"][0]["frame"] == 0
    assert result["unknown_faces"][0]["reason_code"] == "NO_MATCH"


def test_student_with_no_reference_photo_is_never_auto_marked():
    result = run([[obs(matches=[{"face_id": "F1", "similarity": 99.5}])]])
    assert result["students"]["3"]["status"] == "NOT_ENROLLED"
    assert result["students"]["3"]["reason_code"] == "NO_FACE_ON_FILE"


def test_two_faces_claiming_one_student_blocks_auto_present():
    result = run([[obs(matches=[{"face_id": "F1", "similarity": 99.5}],
                       box={"left": 0.1, "top": 0.1, "width": 0.1, "height": 0.1}),
                   obs(matches=[{"face_id": "F1", "similarity": 98.0}],
                       box={"left": 0.5, "top": 0.1, "width": 0.1, "height": 0.1})]])
    assert result["students"]["1"]["status"] == "REVIEW"
    assert result["students"]["1"]["reason_code"] == "DUPLICATE_CLAIM"
    assert result["students"]["2"]["status"] == "ABSENT"


def test_faces_detected_counts_every_face_in_frame():
    result = run([[obs(), obs(matches=[{"face_id": "F1", "similarity": 99.0}])]])
    assert result["faces_detected"] == 2
