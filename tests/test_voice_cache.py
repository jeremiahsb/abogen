from types import SimpleNamespace
from typing import cast

import pytest

from abogen.constants import VOICES_INTERNAL
from abogen.voice_cache import _CACHED_VOICES, ensure_voice_assets
from abogen.web.conversion_runner import _collect_required_voice_ids
from abogen.web.service import Job


@pytest.fixture(autouse=True)
def clear_voice_cache():
    _CACHED_VOICES.clear()
    yield
    _CACHED_VOICES.clear()


def test_ensure_voice_assets_downloads_missing(monkeypatch):
    recorded = []

    def fake_download(**kwargs):
        recorded.append(kwargs["filename"])
        return "/tmp/fake"

    monkeypatch.setattr("abogen.voice_cache.hf_hub_download", fake_download)

    downloaded, errors = ensure_voice_assets(["af_nova", "am_liam"])

    assert downloaded == {"af_nova", "am_liam"}
    assert errors == {}
    assert set(recorded) == {"voices/af_nova.pt", "voices/am_liam.pt"}

    recorded.clear()
    downloaded_again, errors_again = ensure_voice_assets(["af_nova"])

    assert downloaded_again == set()
    assert errors_again == {}
    assert recorded == []


def test_collect_required_voice_ids_includes_all():
    job = SimpleNamespace(
        voice="af_nova",
        chapters=[{"voice_formula": "af_nova*0.7+am_liam*0.3"}],
        chunks=[{"voice": "am_michael"}],
        speakers={
            "hero": {"voice_formula": "af_nova*0.6+am_liam*0.4"},
            "narrator": {"voice": "af_nova"},
        },
    )

    voices = _collect_required_voice_ids(cast(Job, job))

    assert {"af_nova", "am_liam", "am_michael"}.issubset(voices)
    assert voices.issuperset(VOICES_INTERNAL)
