# Bundled text effect fonts

These 15 font families come from the [Google Fonts repository](https://github.com/google/fonts) and use the SIL Open Font License 1.1.

The expected assets are:

- Roboto
- Open Sans
- Lato
- Montserrat
- Oswald
- Raleway
- Poppins
- Bebas Neue
- Anton
- Bangers
- Comic Neue
- Lobster
- Pacifico
- Playfair Display
- Noto Sans

Run `venv/bin/python scripts/fetch_media_fonts.py` from the repository root to
fetch the official upstream files and each family's copyright and OFL notice.
Docker builds do this automatically. The bot falls back to the system Noto or
DejaVu font if an asset is missing during local development. Production images
store the fetched files outside `/app/src` so the read-only source bind mount
does not hide them.
