from __future__ import annotations

import argparse
import hashlib
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GOOGLE_FONTS_REVISION = "7ff85c87f93ea6cca5f41c69f2e4edcb90240f26"
RAW = "https://raw.githubusercontent.com/google/fonts/" f"{GOOGLE_FONTS_REVISION}"
FILES = {
    "Roboto.ttf": "ofl/roboto/Roboto%5Bwdth,wght%5D.ttf",
    "OpenSans.ttf": "ofl/opensans/OpenSans%5Bwdth,wght%5D.ttf",
    "Lato.ttf": "ofl/lato/Lato-Regular.ttf",
    "Montserrat.ttf": "ofl/montserrat/Montserrat%5Bwght%5D.ttf",
    "Oswald.ttf": "ofl/oswald/Oswald%5Bwght%5D.ttf",
    "Raleway.ttf": "ofl/raleway/Raleway%5Bwght%5D.ttf",
    "Poppins.ttf": "ofl/poppins/Poppins-Regular.ttf",
    "BebasNeue.ttf": "ofl/bebasneue/BebasNeue-Regular.ttf",
    "Anton.ttf": "ofl/anton/Anton-Regular.ttf",
    "Bangers.ttf": "ofl/bangers/Bangers-Regular.ttf",
    "ComicNeue.ttf": "ofl/comicneue/ComicNeue-Regular.ttf",
    "Lobster.ttf": "ofl/lobster/Lobster-Regular.ttf",
    "Pacifico.ttf": "ofl/pacifico/Pacifico-Regular.ttf",
    "PlayfairDisplay.ttf": "ofl/playfairdisplay/PlayfairDisplay%5Bwght%5D.ttf",
    "NotoSans.ttf": "ofl/notosans/NotoSans%5Bwdth,wght%5D.ttf",
}
LICENSES = {
    "Roboto-OFL.txt": "ofl/roboto/OFL.txt",
    "OpenSans-OFL.txt": "ofl/opensans/OFL.txt",
    "Lato-OFL.txt": "ofl/lato/OFL.txt",
    "Montserrat-OFL.txt": "ofl/montserrat/OFL.txt",
    "Oswald-OFL.txt": "ofl/oswald/OFL.txt",
    "Raleway-OFL.txt": "ofl/raleway/OFL.txt",
    "Poppins-OFL.txt": "ofl/poppins/OFL.txt",
    "BebasNeue-OFL.txt": "ofl/bebasneue/OFL.txt",
    "Anton-OFL.txt": "ofl/anton/OFL.txt",
    "Bangers-OFL.txt": "ofl/bangers/OFL.txt",
    "ComicNeue-OFL.txt": "ofl/comicneue/OFL.txt",
    "Lobster-OFL.txt": "ofl/lobster/OFL.txt",
    "Pacifico-OFL.txt": "ofl/pacifico/OFL.txt",
    "PlayfairDisplay-OFL.txt": "ofl/playfairdisplay/OFL.txt",
    "NotoSans-OFL.txt": "ofl/notosans/OFL.txt",
}


def fetch(source: str) -> bytes:
    request = urllib.request.Request(
        f"{RAW}/{source}",
        headers={"User-Agent": "Fishie font asset installer"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Install Fishie's redistributable Google Fonts assets."
    )
    parser.add_argument(
        "--destination",
        type=Path,
        default=ROOT / "src" / "files" / "fonts" / "text",
    )
    arguments = parser.parse_args()
    destination = arguments.destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    hashes: set[str] = set()
    for filename, source in FILES.items():
        target = destination / filename
        print(f"Fetching {filename}")
        data = fetch(source)
        if data[:4] not in {
            b"\x00\x01\x00\x00",
            b"OTTO",
            b"ttcf",
        }:
            raise RuntimeError(f"{filename} is not a valid OpenType font")
        digest = hashlib.sha256(data).hexdigest()
        if digest in hashes:
            raise RuntimeError(f"{filename} duplicates another font asset")
        hashes.add(digest)
        target.write_bytes(data)
    if len(hashes) != 15:
        raise RuntimeError("Expected 15 distinct font assets")
    license_directory = destination / "licenses"
    license_directory.mkdir(exist_ok=True)
    for filename, source in LICENSES.items():
        print(f"Fetching license {filename}")
        data = fetch(source)
        if b"SIL OPEN FONT LICENSE" not in data.upper():
            raise RuntimeError(f"{filename} is not an SIL Open Font License")
        (license_directory / filename).write_bytes(data)
    print(f"Installed {len(hashes)} OFL font assets in {destination}")


if __name__ == "__main__":
    main()
