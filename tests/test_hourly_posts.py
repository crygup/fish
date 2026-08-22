from extensions.events.tasks import hourly_post_kind_enabled, hourly_post_media_kind


def test_hourly_post_media_kind_distinguishes_gifs_from_images() -> None:
    assert hourly_post_media_kind("reaction.GIF") == "gif"
    assert hourly_post_media_kind("photo.png") == "image"
    assert hourly_post_media_kind("photo.png?signed=1") == "image"


def test_hourly_post_kind_enabled_uses_each_media_setting() -> None:
    assert hourly_post_kind_enabled("image", images=True, gifs=False, videos=False)
    assert not hourly_post_kind_enabled(
        "gif", images=True, gifs=False, videos=True
    )
    assert hourly_post_kind_enabled("video", images=False, gifs=False, videos=True)
    assert not hourly_post_kind_enabled("audio", images=True, gifs=True, videos=True)
