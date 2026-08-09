from extensions.tools import _decode_qr_values, _make_qr_png


def test_qr_generation_round_trip() -> None:
    value = "https://crygup.com/fishie?source=qr"
    assert _decode_qr_values(_make_qr_png(value)) == [value]
