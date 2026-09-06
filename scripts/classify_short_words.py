"""Classify Fishie's short word candidates using a local word list.

The classifier reads the existing candidate file and never writes to it.  An
exact lowercase entry in the local American English dictionary is treated as
valid.  Uppercase-only entries, repeated-letter strings, and alphabetic
sequences are rejected conservatively; everything else remains available for
manual review.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

Classification = Literal["valid", "review", "invalid"]
DEFAULT_SOURCE_PATH = Path("/home/zil/projects/2-3letters.txt")
DEFAULT_DICTIONARY_PATH = Path("/usr/share/dict/american-english")
DEFAULT_OUTPUT_DIRECTORY = DEFAULT_SOURCE_PATH.parent
ALGORITHM_VERSION = "lowercase-dictionary-v1"


@dataclass(frozen=True, slots=True)
class ShortWordClassification:
    """Classified words and the hashes needed to reproduce the result."""

    source_path: str
    dictionary_path: str
    source_sha256: str
    dictionary_sha256: str
    source_count: int
    valid: tuple[str, ...]
    review: tuple[str, ...]
    invalid: tuple[str, ...]
    invalid_reasons: dict[str, int]

    @property
    def counts(self) -> dict[str, int]:
        """Return the number of source words in each output class."""

        return {
            "source": self.source_count,
            "valid": len(self.valid),
            "review": len(self.review),
            "invalid": len(self.invalid),
        }


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_words(data: bytes, *, encoding: str) -> tuple[str, ...]:
    """Read unique alphabetic words without changing the source file."""

    text = data.decode(encoding)
    words = {line.strip().casefold() for line in text.splitlines() if line.strip()}
    invalid = sorted(
        word for word in words if not word.isalpha() or len(word) not in (2, 3)
    )
    if invalid:
        preview = ", ".join(invalid[:5])
        raise ValueError(f"Source contains non-2/3-letter entries: {preview}")
    return tuple(sorted(words))


def _dictionary_sets(data: bytes) -> tuple[set[str], set[str]]:
    """Return lowercase entries and entries present only with capitals."""

    entries = data.decode("utf-8", errors="strict").splitlines()
    lowercase = {
        entry.casefold()
        for entry in entries
        if entry.isalpha() and entry == entry.casefold()
    }
    uppercase_only = {
        entry.casefold()
        for entry in entries
        if entry.isalpha() and entry != entry.casefold()
    }
    return lowercase, uppercase_only - lowercase


def _is_sequential(word: str) -> bool:
    """Return whether letters advance by one alphabet position."""

    return len(word) > 1 and all(
        ord(letter) == ord(word[0]) + index for index, letter in enumerate(word)
    )


def _classify_word(
    word: str,
    lowercase_dictionary: set[str],
    uppercase_only_dictionary: set[str],
) -> tuple[Classification, str | None]:
    """Classify one source word and return an optional invalid reason."""

    if len(set(word)) == 1:
        return "invalid", "repeated_letter"
    if _is_sequential(word):
        return "invalid", "alphabetical_sequence"
    if word in uppercase_only_dictionary:
        return "invalid", "uppercase_only_dictionary_entry"
    if word in lowercase_dictionary:
        return "valid", None
    return "review", None


def classify_source(
    source_path: Path = DEFAULT_SOURCE_PATH,
    dictionary_path: Path = DEFAULT_DICTIONARY_PATH,
) -> ShortWordClassification:
    """Classify the source file with a local dictionary and no side effects."""

    source_bytes = source_path.read_bytes()
    dictionary_bytes = dictionary_path.read_bytes()
    words = _read_words(source_bytes, encoding="utf-8")
    lowercase_dictionary, uppercase_only_dictionary = _dictionary_sets(dictionary_bytes)

    groups: dict[Classification, list[str]] = {
        "valid": [],
        "review": [],
        "invalid": [],
    }
    reasons: Counter[str] = Counter()
    for word in words:
        category, reason = _classify_word(
            word,
            lowercase_dictionary,
            uppercase_only_dictionary,
        )
        groups[category].append(word)
        if reason is not None:
            reasons[reason] += 1

    return ShortWordClassification(
        source_path=str(source_path),
        dictionary_path=str(dictionary_path),
        source_sha256=_sha256(source_bytes),
        dictionary_sha256=_sha256(dictionary_bytes),
        source_count=len(words),
        valid=tuple(groups["valid"]),
        review=tuple(groups["review"]),
        invalid=tuple(groups["invalid"]),
        invalid_reasons=dict(sorted(reasons.items())),
    )


def _atomic_write(path: Path, content: str) -> None:
    """Write one output atomically without ever targeting the source file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as temporary:
            temporary.write(content)
            temporary_name = temporary.name
        os.replace(temporary_name, path)
        temporary_name = None
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def _word_lines(words: tuple[str, ...]) -> str:
    return "".join(f"{word}\n" for word in words)


def write_outputs(
    result: ShortWordClassification,
    output_directory: Path = DEFAULT_OUTPUT_DIRECTORY,
) -> dict[str, Path]:
    """Write valid, review, invalid, and reproducibility metadata outputs."""

    outputs = {
        "valid": output_directory / "2-3letters-valid.txt",
        "review": output_directory / "2-3letters-review.txt",
        "invalid": output_directory / "2-3letters-invalid.txt",
        "metadata": output_directory / "2-3letters-classification.json",
    }
    _atomic_write(outputs["valid"], _word_lines(result.valid))
    _atomic_write(outputs["review"], _word_lines(result.review))
    _atomic_write(outputs["invalid"], _word_lines(result.invalid))
    metadata = {
        "algorithm": ALGORITHM_VERSION,
        "source": asdict(result),
        "counts": result.counts,
        "examples": {
            word: (
                "valid"
                if word in result.valid
                else "review" if word in result.review else "invalid"
            )
            for word in ("air", "abc", "aa", "aaa")
        },
    }
    _atomic_write(
        outputs["metadata"],
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
    )
    return outputs


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE_PATH)
    parser.add_argument(
        "--dictionary",
        type=Path,
        default=DEFAULT_DICTIONARY_PATH,
        help="Local newline-separated dictionary (default: %(default)s)",
    )
    parser.add_argument(
        "--output-directory",
        type=Path,
        default=DEFAULT_OUTPUT_DIRECTORY,
    )
    return parser.parse_args()


def main() -> None:
    arguments = _arguments()
    result = classify_source(arguments.source, arguments.dictionary)
    outputs = write_outputs(result, arguments.output_directory)
    print(
        f"Classified {result.source_count:,} words: "
        f"{len(result.valid):,} valid, {len(result.review):,} review, "
        f"{len(result.invalid):,} invalid."
    )
    for name, path in outputs.items():
        print(f"{name}: {path}")


if __name__ == "__main__":
    main()
