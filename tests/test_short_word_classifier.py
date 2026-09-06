from __future__ import annotations

import json
import sys
from pathlib import Path


def _classifier_functions():
    repository_root = Path(__file__).resolve().parents[1]
    if str(repository_root) not in sys.path:
        sys.path.insert(0, str(repository_root))
    from scripts.classify_short_words import classify_source, write_outputs

    return classify_source, write_outputs


def _fixture_files(tmp_path: Path) -> tuple[Path, Path, bytes]:
    source = tmp_path / "2-3letters.txt"
    source_bytes = b"air\nabc\naa\naaa\nzoo\n"
    source.write_bytes(source_bytes)
    dictionary = tmp_path / "american-english"
    dictionary.write_text("air\nzoo\nABC\nAA\nAAA\n", encoding="utf-8")
    return source, dictionary, source_bytes


def test_classifier_is_conservative_and_keeps_source_unchanged(
    tmp_path: Path,
) -> None:
    classify_source, _ = _classifier_functions()
    source, dictionary, source_bytes = _fixture_files(tmp_path)

    result = classify_source(source, dictionary)

    assert result.valid == ("air", "zoo")
    assert result.invalid == ("aa", "aaa", "abc")
    assert result.review == ()
    assert result.counts == {
        "source": 5,
        "valid": 2,
        "review": 0,
        "invalid": 3,
    }
    assert source.read_bytes() == source_bytes


def test_outputs_are_sorted_partitioned_and_reproducible(tmp_path: Path) -> None:
    classify_source, write_outputs = _classifier_functions()
    source, dictionary, _ = _fixture_files(tmp_path)

    result = classify_source(source, dictionary)
    outputs = write_outputs(result, tmp_path / "outputs")

    assert outputs["valid"].read_text(encoding="utf-8") == "air\nzoo\n"
    assert outputs["review"].read_text(encoding="utf-8") == ""
    assert outputs["invalid"].read_text(encoding="utf-8") == "aa\naaa\nabc\n"

    metadata = json.loads(outputs["metadata"].read_text(encoding="utf-8"))
    assert metadata["algorithm"] == "lowercase-dictionary-v1"
    assert metadata["counts"] == result.counts
    assert metadata["examples"] == {
        "aa": "invalid",
        "aaa": "invalid",
        "abc": "invalid",
        "air": "valid",
    }
    assert metadata["source"]["source_sha256"] == result.source_sha256
