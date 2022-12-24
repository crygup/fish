# these are mainly just here so i can access for feedback command lol

import textwrap
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageSequence
from wand.image import Image as wImage

from utils import to_thread


@to_thread
def text_to_image(text: str) -> BytesIO:
    output_buffer = BytesIO()

    text = textwrap.fill(text, 25)
    font = ImageFont.truetype("src/files/assets/fonts/wsr.otf", 50)
    padding = 50

    images = [Image.new("RGBA", (1, 1), color=(255, 255, 255)) for _ in range(2)]
    for index, (new_image, color) in enumerate(zip(images, ((47, 49, 54), "black"))):
        draw = ImageDraw.Draw(new_image)
        w, h = draw.multiline_textsize(text, font=font)
        images[index] = new_image = new_image.resize((w + padding, h + padding))
        draw = ImageDraw.Draw(new_image)
        draw.multiline_text(
            (padding / 2, padding / 2), text=text, fill=color, font=font
        )

    background, foreground = images
    background.paste(foreground, (0, 0), foreground)
    background.save(output_buffer, "png")

    output_buffer.seek(0)
    return output_buffer


@to_thread
def add_images(image: BytesIO, text: BytesIO) -> BytesIO:
    text_image = Image.open(text)
    bottom_image = Image.open(image)

    new_text_width = text_image.width
    new_text_height = text_image.height

    while new_text_width > bottom_image.width:
        new_text_width //= 2
        new_text_height //= 2

    if text_image.width > new_text_width or text_image.height > new_text_height:
        text_image = text_image.resize((new_text_width, new_text_height))

    base_image = Image.new(
        "RGBA",
        (
            bottom_image.width,
            text_image.height + bottom_image.height,
        ),
        (255, 255, 255),
    )
    center_x = (bottom_image.width - text_image.width) // 2
    base_image.paste(text_image, (center_x, 0))
    base_image.paste(bottom_image, (0, text_image.height))

    buf = BytesIO()
    base_image.save(buf, format="png")
    buf.seek(0)

    return buf


@to_thread
def gif_maker(image: BytesIO, text: BytesIO) -> BytesIO:
    """This assumes the image you provide is a GIF."""
    gif = Image.open(image)
    text_image = Image.open(text)
    new_images = []
    # Calculate the x-coordinate for the center of the image

    new_text_width = text_image.width
    new_text_height = text_image.height

    while new_text_width > gif.width:
        new_text_width //= 2
        new_text_height //= 2

    if text_image.width > new_text_width or text_image.height > new_text_height:
        text_image = text_image.resize((new_text_width, new_text_height))

    center_x = (gif.width - text_image.width) // 2

    # Iterate through the frames and paste image1 onto each frame
    for frame in ImageSequence.Iterator(gif):
        new_image = Image.new(
            "RGBA",
            (gif.width, text_image.height + gif.height),
            (255, 255, 255),
        )

        new_image.paste(text_image, (center_x, 0))
        new_image.paste(frame, (0, text_image.height))
        new_image.info["duration"] = frame.info["duration"]

        new_images.append(new_image)

    # Save the resulting GIF
    output_buffer = BytesIO()
    new_images[0].save(
        output_buffer,
        save_all=True,
        append_images=new_images[1:],
        format="gif",
        optimize=False,
        loop=0,
        disposal=2,
    )
    output_buffer.seek(0)
    return output_buffer


@to_thread
def invert_method(image: BytesIO) -> BytesIO:
    with Image.open(image) as output:
        output_buffer = BytesIO()

        new_im = Image.new("RGB", (output.width, output.height))
        new_im.paste(output)

        inverted = ImageOps.invert(new_im)

        inverted.save(output_buffer, "png")
        output_buffer.seek(0)

        return output_buffer


@to_thread
def layer_image(image: BytesIO, cover_file: str) -> BytesIO:
    with Image.open(image) as output:
        output_buffer = BytesIO()

        with Image.open(cover_file) as cover:
            resized_to_fit = cover.resize((output.width, output.height))
            output.paste(resized_to_fit, mask=resized_to_fit)

        output.save(output_buffer, "png")
        output_buffer.seek(0)

        return output_buffer


@to_thread
def resize_method(image: BytesIO, height: int, width: int) -> BytesIO:
    with Image.open(image) as output:
        output_buffer = BytesIO()

        resized = output.resize((height, width))
        resized.save(output_buffer, "png")
        output_buffer.seek(0)

        return output_buffer


@to_thread
def blur_method(image: BytesIO, scale: int = 3) -> BytesIO:
    with wImage(file=image) as output:
        buffer = BytesIO()
        output.blur(radius=0, sigma=scale)

        output.save(buffer)
        buffer.seek(0)
        return buffer


@to_thread
def kuwahara_method(image: BytesIO, scale: float = 1.5) -> BytesIO:
    with wImage(file=image) as output:
        buffer = BytesIO()
        output.kuwahara(radius=2, sigma=scale)

        output.save(buffer)
        buffer.seek(0)
        return buffer


@to_thread
def sharpen_method(image: BytesIO, scale: int = 4) -> BytesIO:
    with wImage(file=image) as output:
        buffer = BytesIO()
        output.sharpen(radius=8, sigma=scale)

        output.save(buffer)
        buffer.seek(0)
        return buffer


@to_thread
def spread_method(image: BytesIO) -> BytesIO:
    with wImage(file=image) as output:
        buffer = BytesIO()
        output.spread(radius=8)

        output.save(buffer)
        buffer.seek(0)
        return buffer
