from types import SimpleNamespace

from extensions.fun.upload import UploadCommands
from extensions.fun.upload_limits import (
    consume_manual_uploads,
    manual_upload_lock,
    reset_manual_upload_limits,
)


def test_unified_upload_classifies_common_media() -> None:
    assert UploadCommands._kind_from_filename("clip.mp4") == "video"
    assert UploadCommands._kind_from_filename("animation.gif") == "post"
    assert UploadCommands._kind_from_url("https://klipy.com/gifs/example") == "post"
    assert UploadCommands._kind_from_url("https://example.com/video.mp4") == "video"
    assert UploadCommands._kind_from_url("https://example.com/convert.avif") is None


def test_unified_upload_classifies_avif_before_iso_video_containers() -> None:
    # AVIF uses the ISO-BMFF ``ftyp`` box too, but its brand identifies it as
    # an image and it should be routed through the post upload converter.
    avif_header = b"\x00\x00\x00\x1cftypavif\x00\x00\x00\x00"
    assert UploadCommands._kind_from_bytes(avif_header, "convert.avif") == "post"


def test_unified_upload_uses_content_type_for_attachments() -> None:
    video = SimpleNamespace(filename="upload", content_type="video/mp4")
    image = SimpleNamespace(filename="upload", content_type="image/png")

    assert UploadCommands._kind_from_filename(video.filename) is None
    assert UploadCommands._kind_from_filename(image.filename) is None


def test_manual_upload_limit_is_shared_by_all_upload_commands() -> None:
    reset_manual_upload_limits()
    context = SimpleNamespace(
        guild=SimpleNamespace(id=123), author=SimpleNamespace(id=456)
    )

    assert all(consume_manual_uploads(context) is None for _ in range(20))
    assert consume_manual_uploads(context) is not None
    reset_manual_upload_limits()


def test_manual_upload_lock_is_scoped_to_guild_and_user() -> None:
    reset_manual_upload_limits()
    first = SimpleNamespace(
        guild=SimpleNamespace(id=123), author=SimpleNamespace(id=456)
    )
    same_user = SimpleNamespace(
        guild=SimpleNamespace(id=123), author=SimpleNamespace(id=456)
    )
    other_user = SimpleNamespace(
        guild=SimpleNamespace(id=123), author=SimpleNamespace(id=789)
    )
    assert manual_upload_lock(first) is manual_upload_lock(same_user)
    assert manual_upload_lock(first) is not manual_upload_lock(other_user)
    reset_manual_upload_limits()
