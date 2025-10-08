from abogen.speaker_analysis import analyze_speakers


def _chapters():
    return [
        {
            "id": "0001",
            "index": 0,
            "title": "Test",
            "text": "",
            "enabled": True,
        }
    ]


def _chunk(text: str, idx: int) -> dict:
    return {
        "id": f"chunk-{idx}",
        "chapter_index": 0,
        "chunk_index": idx,
        "text": text,
    }


def test_analyze_speakers_infers_gender_from_pronouns():
    chunks = [
        _chunk("\"Greetings,\" said John. He adjusted his hat as he smiled.", 0),
        _chunk("\"Hello,\" said Mary. She straightened her dress as she introduced herself.", 1),
        _chunk("\"Nice to meet you,\" said Alex.", 2),
    ]

    analysis = analyze_speakers(_chapters(), chunks, threshold=1, max_speakers=0)

    john = analysis.speakers.get("john")
    mary = analysis.speakers.get("mary")
    alex = analysis.speakers.get("alex")

    assert john is not None
    assert mary is not None
    assert alex is not None

    assert john.gender == "male"
    assert mary.gender == "female"
    assert alex.gender == "unknown"
