"""Batch training days are manual (any days, min 1) — never silently forced."""
import pytest


@pytest.mark.django_db
def test_batch_serializer_validates_training_days():
    from apps.academy.models import Sport
    from apps.batches.views import BatchSerializer
    sport = Sport.objects.create(name="T-Ball", code="TB")
    base = {"name": "B1", "sport": sport.pk, "start_time": "07:00:00", "end_time": "08:00:00"}
    bad = BatchSerializer(data={**base, "training_days": []})
    # empty = unset is allowed (legacy rows); explicit garbage is not
    assert bad.is_valid(), bad.errors
    bad = BatchSerializer(data={**base, "training_days": [0, 7]})
    assert not bad.is_valid()
    bad = BatchSerializer(data={**base, "training_days": [1, 1]})
    # duplicates collapse to a single day — still valid, stored deduped
    assert bad.is_valid(), bad.errors
    ok = BatchSerializer(data={**base, "training_days": [0, 2, 4]})
    assert ok.is_valid(), ok.errors
