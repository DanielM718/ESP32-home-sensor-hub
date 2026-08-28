from __future__ import annotations

import sys
from array import array

from butters.audio.conversion import StreamingPcmConverter


def _little_endian_bytes(values: array) -> bytes:
    if sys.byteorder != "little":
        values.byteswap()
    return values.tobytes()


def _decode_s16(data: bytes) -> list[int]:
    values = array("h")
    values.frombytes(data)
    if sys.byteorder != "little":
        values.byteswap()
    return values.tolist()


def test_stereo_48k_is_downmixed_and_resampled_to_16k() -> None:
    input_frames = 48_000
    interleaved = array("h")
    for _ in range(input_frames):
        interleaved.extend((10_000, 2_000))
    raw = _little_endian_bytes(interleaved)
    converter = StreamingPcmConverter(
        input_rate=48_000,
        input_channels=2,
        input_sample_width=2,
    )

    output = bytearray()
    input_frame_bytes = 2 * 2
    for offset in range(0, len(raw), 997 * input_frame_bytes):
        output.extend(converter.convert(raw[offset : offset + 997 * input_frame_bytes]))
    output.extend(converter.finish())

    samples = _decode_s16(bytes(output))
    assert len(samples) == 16_000
    assert set(samples) == {6_000}
    assert converter.buffered_samples == 0


def test_8k_unsigned_pcm_is_upsampled_with_expected_duration() -> None:
    raw = bytes([128] * 800)
    converter = StreamingPcmConverter(
        input_rate=8_000,
        input_channels=1,
        input_sample_width=1,
    )
    output = converter.convert(raw[:317]) + converter.convert(raw[317:])
    output += converter.finish()

    assert len(output) == 1_600 * 2
    assert set(_decode_s16(output)) == {0}


def test_common_sample_rates_preserve_duration_across_uneven_chunks() -> None:
    for input_rate in (8_000, 16_000, 22_050, 44_100, 48_000):
        input_count = round(input_rate * 0.123)
        raw = _little_endian_bytes(array("h", range(input_count)))
        converter = StreamingPcmConverter(
            input_rate=input_rate,
            input_channels=1,
            input_sample_width=2,
        )
        output = bytearray()
        for offset in range(0, len(raw), 74):
            output.extend(converter.convert(raw[offset : offset + 74]))
        output.extend(converter.finish())

        expected_samples = round(input_count * 16_000 / input_rate)
        assert len(output) // 2 == expected_samples


def test_96khz_pcm_preserves_duration_when_resampled_to_16khz() -> None:
    input_count = 96_000
    raw = _little_endian_bytes(array("h", [1_234]) * input_count)
    converter = StreamingPcmConverter(
        input_rate=96_000,
        input_channels=1,
        input_sample_width=2,
    )

    output = bytearray()
    for offset in range(0, len(raw), 1_978):
        output.extend(converter.convert(raw[offset : offset + 1_978]))
    output.extend(converter.finish())

    samples = _decode_s16(bytes(output))
    assert len(samples) == 16_000
    assert set(samples) == {1_234}
    assert converter.buffered_samples == 0
