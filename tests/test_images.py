from extensions.fun.images import _atempo_filter


def test_audio_speed_filter_stays_inside_ffmpeg_limits() -> None:
    for speed in (0.1, 0.25, 0.5, 1.0, 2.0, 10.0):
        factors = [float(item.split("=", 1)[1]) for item in _atempo_filter(speed).split(",")]
        assert all(0.5 <= factor <= 2.0 for factor in factors)
        product = 1.0
        for factor in factors:
            product *= factor
        assert abs(product - speed) < 1e-5
