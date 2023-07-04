from __future__ import annotations

import itertools
from io import BytesIO
from typing import TYPE_CHECKING, Any, Dict, List, Tuple

from PIL import Image, ImageDraw, ImageFont

from utils import BlankException, to_thread, get_lastfm_data

if TYPE_CHECKING:
    from bot import Bot


@to_thread
def make_chart(data: List[Tuple[BytesIO, str]], name: str):
    # fmt: off
    image_cords = itertools.chain(
        [(100, 100), (400, 100), (700, 100), (100, 400), (400, 400), (700, 400), (100, 700), (400, 700), (700, 700),]
    )
    spacing = 20
    # fmt: on
    font = ImageFont.truetype("src/files/assets/fonts/wsr.otf", 25)
    name_font = ImageFont.truetype("src/files/assets/fonts/wsr.otf", 50)
    output_buffer = BytesIO()
    with Image.open("src/files/assets/chart.png") as image:
        draw = ImageDraw.Draw(image)

        text_width, _ = draw.textsize(name, font=name_font)
        text_x = 500 - text_width // 2
        draw.text((text_x, 30), name, fill=(255, 255, 255), font=name_font)

        for item in data:
            with Image.open(item[0]) as cover:
                cover = cover.resize((200, 200))
                x, y = next(image_cords)
                image.paste(cover, (x, y))
                draw.text(
                    (x, y + 200 + spacing), item[1], font=font, fill=(255, 255, 255)
                )

        image.save(output_buffer, "png")
        output_buffer.seek(0)

    return output_buffer


async def get_top_albums(bot: Bot, period: str, name: str) -> Dict[Any, Any]:
    results = await get_lastfm_data(
        bot,
        "2.0",
        "user.gettopalbums",
        "user",
        name,
        extras={"limit": 100, "period": period},
    )

    data: Dict[Any, Any] = results["topalbums"].get("album")

    if data == [] or data is None:
        raise BlankException("No tracks found for this user.")

    if len(data) < 9:
        raise BlankException(
            "Not enough albums to make a chart, sorry. Maybe try a different time period?"
        )

    return data


@to_thread
def make_advanced_chart(data: List[Tuple[BytesIO, str]]):
    image_cords = itertools.chain(
        [
            # top
            ((20, 60), (150, 150)),
            ((195, 60), (150, 150)),
            ((370, 60), (150, 150)),
            ((545, 60), (150, 150)),
            # top second row
            ((20, 300), (80, 80)),
            ((104, 300), (80, 80)),
            ((188, 300), (80, 80)),
            ((272, 300), (80, 80)),
            ((356, 300), (80, 80)),
            ((440, 300), (80, 80)),
            ((524, 300), (80, 80)),
            ((608, 300), (80, 80)),
            # bottom second row
            ((20, 390), (80, 80)),
            ((104, 390), (80, 80)),
            ((188, 390), (80, 80)),
            ((272, 390), (80, 80)),
            ((356, 390), (80, 80)),
            ((440, 390), (80, 80)),
            ((524, 390), (80, 80)),
            ((608, 390), (80, 80)),
            # top last row
            ((20, 560), (80, 80)),
            ((104, 560), (80, 80)),
            ((188, 560), (80, 80)),
            ((272, 560), (80, 80)),
            ((356, 560), (80, 80)),
            ((440, 560), (80, 80)),
            ((524, 560), (80, 80)),
            ((608, 560), (80, 80)),
            ((692, 560), (80, 80)),
            ((776, 560), (80, 80)),
            # second last row
            ((20, 650), (80, 80)),
            ((104, 650), (80, 80)),
            ((188, 650), (80, 80)),
            ((272, 650), (80, 80)),
            ((356, 650), (80, 80)),
            ((440, 650), (80, 80)),
            ((524, 650), (80, 80)),
            ((608, 650), (80, 80)),
            ((692, 650), (80, 80)),
            ((776, 650), (80, 80)),
            # bottom last row
            ((20, 740), (80, 80)),
            ((104, 740), (80, 80)),
            ((188, 740), (80, 80)),
            ((272, 740), (80, 80)),
            ((356, 740), (80, 80)),
            ((440, 740), (80, 80)),
            ((524, 740), (80, 80)),
            ((608, 740), (80, 80)),
            ((692, 740), (80, 80)),
            ((776, 740), (80, 80)),
        ]
    )

    spacing = 20

    font = ImageFont.truetype("src/files/assets/fonts/wsr.otf", 13)

    output_buffer = BytesIO()
    with Image.open("src/files/assets/chart2.png") as output:
        new_im = Image.new("RGBA", (output.width, output.height))
        new_im.paste(output)

        for item in data:
            with Image.open(item[0]) as cover:
                pos, size = next(image_cords)
                cover = cover.resize(size)
                new_im.paste(cover, pos)

        new_im.save(output_buffer, "png")
        output_buffer.seek(0)

    return output_buffer
