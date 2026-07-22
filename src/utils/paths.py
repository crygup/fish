from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = SOURCE_ROOT.parent
FILES_ROOT = SOURCE_ROOT / "files"
DOWNLOADS_ROOT = FILES_ROOT / "downloads"
