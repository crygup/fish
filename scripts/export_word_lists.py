"""Export the raw short-word candidates used to build Fishie's allowlist."""

from __future__ import annotations

import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
OUTPUT_PATH = Path("/home/zil/projects/2-3letters.txt")

if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))


def export_short_words(output_path: Path = OUTPUT_PATH) -> int:
    """Write every raw two- and three-letter dictionary candidate.

    This intentionally reads the source dictionaries directly.  The runtime
    Word Bomb lookup is filtered by the reviewed allowlist, and exporting that
    lookup would destroy the raw comparison file this script is meant to
    preserve.
    """

    from extensions.fun.minigames import (
        WORD_BOMB_EXTENDED_WORD_LIST_PATH,
        WORD_BOMB_WORD_LIST_PATH,
        WORD_LIST_PATH,
        _load_word_game_words,
        short_word_game_words,
    )

    raw_words = (
        *_load_word_game_words(WORD_LIST_PATH),
        *_load_word_game_words(WORD_BOMB_WORD_LIST_PATH),
        *_load_word_game_words(WORD_BOMB_EXTENDED_WORD_LIST_PATH),
    )
    words = short_word_game_words(raw_words)
    output_path.write_text("\n".join(words) + "\n", encoding="utf-8")
    return len(words)


def main() -> None:
    count = export_short_words()
    print(f"Wrote {count:,} words to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
