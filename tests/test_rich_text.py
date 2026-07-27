from io import BytesIO

from PIL import Image

from utils.rich_text import (
    _CJK_FONT,
    CUSTOM_EMOJI_RE,
    inline_animation_duration,
    inline_image_tokens,
    tokenize_inline_text,
)


def test_cjk_fallback_uses_regular_weight() -> None:
    assert _CJK_FONT.name == "NotoSansCJK-Regular.ttc"


def test_unicode_sequences_and_custom_emoji_are_single_image_tokens() -> None:
    custom = "<a:dance:123456789012345678>"
    text = f"家族 👨‍👩‍👧‍👦 flag 🇯🇵 {custom}"

    assert inline_image_tokens(text) == ["👨‍👩‍👧‍👦", "🇯🇵", custom]
    assert CUSTOM_EMOJI_RE.fullmatch(custom) is not None
    assert [token.kind for token in tokenize_inline_text(text)].count("image") == 3


def test_animated_custom_emoji_duration_is_detected() -> None:
    output = BytesIO()
    frames = [
        Image.new("RGBA", (16, 16), color)
        for color in ((255, 0, 0, 255), (0, 255, 0, 255))
    ]
    frames[0].save(
        output,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=[40, 60],
        loop=0,
    )

    assert (
        inline_animation_duration({"<a:test:123456789012345678>": output.getvalue()})
        == 100
    )
