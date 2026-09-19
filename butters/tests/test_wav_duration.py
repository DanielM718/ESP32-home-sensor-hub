"""WAV duration measured from bytes received, not from declared chunk sizes.

The first paid OpenAI speech request returned a streaming WAV whose RIFF and
`data` chunk sizes were both 0xFFFFFFFF. Python's `wave` module trusted them,
returned `getnframes() == 2147483647`, and Butters reported a 1.2 second clip
as 89478.485 seconds. `test_the_streaming_placeholder_regression` is the exact
reproduction; the rest fixes the parsing that allowed it.
"""

from __future__ import annotations

import io
import struct
import wave

import pytest
from butters.audio.wav import WavFormatError, measure_wav, wav_duration_seconds
from butters.web.speech import SpeechProviderError, _wav_duration

# The accepted production payload's shape.
CHANNELS = 1
SAMPLE_WIDTH = 2
SAMPLE_RATE = 24_000
BLOCK_ALIGN = CHANNELS * SAMPLE_WIDTH
PCM_BYTES = 57_600
EXPECTED_SECONDS = 1.2
PLACEHOLDER = 0xFFFFFFFF
STREAMING_SENTINEL_VALUE = 0xFFFFFFFF


def _chunk(identifier: bytes, body: bytes, *, declared: int | None = None) -> bytes:
    size = len(body) if declared is None else declared
    padding = b"\x00" if len(body) % 2 else b""
    return identifier + struct.pack("<I", size) + body + padding


def _fmt(audio_format: int = 1, *, block_align: int = BLOCK_ALIGN) -> bytes:
    return _chunk(
        b"fmt ",
        struct.pack(
            "<HHIIHH",
            audio_format,
            CHANNELS,
            SAMPLE_RATE,
            SAMPLE_RATE * BLOCK_ALIGN,
            block_align,
            SAMPLE_WIDTH * 8,
        ),
    )


def _wav(
    *,
    pcm_bytes: int = PCM_BYTES,
    streaming: bool = False,
    extra_chunks: bytes = b"",
    audio_format: int = 1,
    block_align: int = BLOCK_ALIGN,
    riff_size: int | None = None,
) -> bytes:
    pcm = b"\x01\x00" * (pcm_bytes // 2)
    data = _chunk(b"data", pcm, declared=PLACEHOLDER if streaming else None)
    body = b"WAVE" + _fmt(audio_format, block_align=block_align) + extra_chunks + data
    size = riff_size if riff_size is not None else len(body)
    return b"RIFF" + struct.pack("<I", size) + body


# ======================= the exact production regression ===================


def test_the_streaming_placeholder_regression() -> None:
    """A 1.2 second clip must not read as 89478 seconds.

    The fixture reproduces exactly what OpenAI returned: placeholder sizes in
    both the RIFF header and the data chunk, mono 16-bit 24 kHz, 57,600 PCM
    bytes.
    """

    payload = _wav(streaming=True, riff_size=PLACEHOLDER)

    # The old behaviour, still reproducible through the stdlib parser.
    with wave.open(io.BytesIO(payload), "rb") as legacy:
        legacy_seconds = legacy.getnframes() / legacy.getframerate()
    assert legacy.getnframes() == 2147483647
    assert legacy_seconds == pytest.approx(89478.485, abs=0.001)

    # The corrected behaviour.
    assert wav_duration_seconds(payload) == pytest.approx(EXPECTED_SECONDS)
    assert _wav_duration(payload) == pytest.approx(EXPECTED_SECONDS)

    measurement = measure_wav(payload)
    assert measurement.streaming is True
    assert measurement.declared_data_bytes is None
    assert measurement.data_bytes == PCM_BYTES
    assert measurement.frames == PCM_BYTES // BLOCK_ALIGN
    # 57,600 / 2 / 24,000 = 1.200
    assert measurement.duration_seconds == pytest.approx(57_600 / 2 / 24_000)


def test_the_sentinel_never_produces_a_gigantic_duration() -> None:
    """Whatever the RIFF container claims, the data sentinel governs."""

    for riff_size in (PLACEHOLDER, 0x7FFFFFFF, 0):
        payload = _wav(streaming=True, riff_size=riff_size)
        assert wav_duration_seconds(payload) == pytest.approx(EXPECTED_SECONDS)
        assert wav_duration_seconds(payload) < 60


# ============================== well-formed ================================


def test_a_conventional_finalized_wav_uses_its_declared_length() -> None:
    payload = _wav()
    measurement = measure_wav(payload)

    assert measurement.streaming is False
    assert measurement.declared_data_bytes == PCM_BYTES
    assert measurement.duration_seconds == pytest.approx(EXPECTED_SECONDS)


def test_the_parser_agrees_with_the_stdlib_on_ordinary_files() -> None:
    """Where `wave` is trustworthy, the new parser must not disagree."""

    output = io.BytesIO()
    with wave.open(output, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(16_000)
        target.writeframes(b"\0\0" * 8_000)
    payload = output.getvalue()

    with wave.open(io.BytesIO(payload), "rb") as source:
        expected = source.getnframes() / source.getframerate()
    assert wav_duration_seconds(payload) == pytest.approx(expected)
    assert wav_duration_seconds(payload) == pytest.approx(0.5)


def test_extra_chunks_before_data_are_walked_not_assumed_away() -> None:
    """No fixed 44-byte header: LIST and fact chunks are ordinary."""

    extra = _chunk(b"LIST", b"INFOISFT" + b"Butters\x00") + _chunk(
        b"fact", struct.pack("<I", PCM_BYTES // BLOCK_ALIGN)
    )
    payload = _wav(extra_chunks=extra)
    measurement = measure_wav(payload)

    assert measurement.data_offset > 44
    assert measurement.duration_seconds == pytest.approx(EXPECTED_SECONDS)


def test_odd_sized_chunks_are_padded_to_an_even_boundary() -> None:
    payload = _wav(extra_chunks=_chunk(b"LIST", b"odd"))
    assert wav_duration_seconds(payload) == pytest.approx(EXPECTED_SECONDS)


# ================== RIFF chunkSize semantics, evidence-based ===============
#
# chunkSize is defined as the count of valid bytes in the chunk. Only one
# value is treated as a streaming sentinel, because only one was ever
# observed: the captured OpenAI response used 0xFFFFFFFF. Everything else
# follows the specification, so a malformed file stays malformed instead of
# being reinterpreted as a stream.


def test_only_the_observed_sentinel_is_recognized() -> None:
    from butters.audio.wav import STREAMING_SENTINEL

    assert STREAMING_SENTINEL == 0xFFFFFFFF


def test_a_truncated_finite_length_is_an_error_not_an_implicit_stream() -> None:
    """Case C: declares 57,600 PCM bytes, only 40,000 arrive.

    The wrong answer here is 40,000 / 2 / 24,000 = 0.8333 s, which would
    present an incomplete download as valid audio.
    """

    arrived = 40_000
    pcm = b"\x01\x00" * (arrived // 2)
    data = _chunk(b"data", pcm, declared=PCM_BYTES)
    payload = b"RIFF" + struct.pack("<I", 0) + b"WAVE" + _fmt() + data

    with pytest.raises(WavFormatError) as denied:
        measure_wav(payload)
    assert "only 40000 arrived" in str(denied.value)
    with pytest.raises(SpeechProviderError) as refused:
        _wav_duration(payload)
    assert refused.value.code == "malformed_audio"


def test_a_finite_data_chunk_counts_only_its_declared_bytes() -> None:
    """Case D: a trailing chunk after data is not audio."""

    pcm = b"\x01\x00" * (PCM_BYTES // 2)
    trailing = _chunk(b"LIST", b"INFOISFT" + b"Butters\x00")
    payload = (
        b"RIFF"
        + struct.pack("<I", 0)
        + b"WAVE"
        + _fmt()
        + _chunk(b"data", pcm)
        + trailing
    )
    measurement = measure_wav(payload)

    assert measurement.streaming is False
    assert measurement.declared_data_bytes == PCM_BYTES
    # Exactly the declared PCM, not PCM + the trailing chunk.
    assert measurement.data_bytes == PCM_BYTES
    assert measurement.duration_seconds == pytest.approx(EXPECTED_SECONDS)


def test_a_zero_length_data_chunk_is_empty_not_a_sentinel() -> None:
    """Case B: zero is a legitimate empty chunk under RIFF."""

    trailing = b"\x02\x00" * 1000  # bytes after data that must NOT be read
    payload = (
        b"RIFF"
        + struct.pack("<I", 0)
        + b"WAVE"
        + _fmt()
        + _chunk(b"data", b"")
        + trailing
    )
    measurement = measure_wav(payload)

    assert measurement.streaming is False
    assert measurement.declared_data_bytes == 0
    assert measurement.data_bytes == 0
    assert measurement.duration_seconds == 0.0
    # And the STT path, where duration gates admission, still fails closed:
    # its existing `duration <= 0` check rejects empty audio.


def test_0x7fffffff_is_an_ordinary_length_not_a_sentinel() -> None:
    """Case E: no captured evidence, so it gets no special treatment.

    2147483647 was only ever `wave.getnframes()` - 0xFFFFFFFF divided by a
    2-byte block alignment - never a declared chunk size.
    """

    pcm = b"\x01\x00" * (PCM_BYTES // 2)
    data = _chunk(b"data", pcm, declared=0x7FFFFFFF)
    payload = b"RIFF" + struct.pack("<I", 0) + b"WAVE" + _fmt() + data

    with pytest.raises(WavFormatError) as denied:
        measure_wav(payload)
    assert "only 57600 arrived" in str(denied.value)


def test_a_partial_trailing_frame_is_not_silently_floored() -> None:
    """Case F: an odd byte count is incomplete audio, not 1.1999 s."""

    pcm = b"\x01\x00" * (PCM_BYTES // 2) + b"\x7f"
    payload = (
        b"RIFF"
        + struct.pack("<I", STREAMING_SENTINEL_VALUE)
        + b"WAVE"
        + _fmt()
        + b"data"
        + struct.pack("<I", STREAMING_SENTINEL_VALUE)
        + pcm
    )
    with pytest.raises(WavFormatError) as denied:
        measure_wav(payload)
    assert "whole number" in str(denied.value)


def test_an_oversized_riff_container_size_does_not_imply_streaming() -> None:
    """The RIFF size field never decides how much audio arrived."""

    pcm = b"\x01\x00" * (PCM_BYTES // 2)
    payload = (
        b"RIFF" + struct.pack("<I", 0xFFFFFF00) + b"WAVE" + _fmt() + _chunk(b"data", pcm)
    )
    measurement = measure_wav(payload)

    assert measurement.streaming is False
    assert measurement.duration_seconds == pytest.approx(EXPECTED_SECONDS)


# ============================ fail truthfully ==============================


@pytest.mark.parametrize(
    ("payload", "reason"),
    (
        (b"", "empty"),
        (b"RIFF", "truncated signature"),
        (b"NOPE" + struct.pack("<I", 0) + b"WAVE", "bad RIFF magic"),
        (b"RIFF" + struct.pack("<I", 0) + b"AVI ", "not WAVE"),
    ),
)
def test_a_non_wave_payload_is_refused(payload: bytes, reason: str) -> None:
    with pytest.raises(WavFormatError):
        measure_wav(payload)
    # The provider turns that into a request failure: a 200 carrying non-audio
    # is a provider problem, not a metadata gap.
    with pytest.raises(SpeechProviderError) as denied:
        _wav_duration(payload)
    assert denied.value.code == "malformed_audio"


def test_a_missing_fmt_chunk_is_refused() -> None:
    payload = b"RIFF" + struct.pack("<I", 0) + b"WAVE" + _chunk(b"data", b"\0\0")
    with pytest.raises(WavFormatError):
        measure_wav(payload)


def test_a_missing_data_chunk_is_refused() -> None:
    payload = b"RIFF" + struct.pack("<I", 0) + b"WAVE" + _fmt()
    with pytest.raises(WavFormatError):
        measure_wav(payload)


def test_a_truncated_fmt_chunk_is_refused() -> None:
    payload = b"RIFF" + struct.pack("<I", 0) + b"WAVE" + b"fmt " + struct.pack("<I", 16) + b"\x01\x00"
    with pytest.raises(WavFormatError):
        measure_wav(payload)


def test_a_zero_sample_rate_is_refused_not_divided_by() -> None:
    fmt = _chunk(b"fmt ", struct.pack("<HHIIHH", 1, 1, 0, 0, 2, 16))
    payload = b"RIFF" + struct.pack("<I", 0) + b"WAVE" + fmt + _chunk(b"data", b"\0\0")

    with pytest.raises(WavFormatError) as denied:
        measure_wav(payload)
    assert "no audio" in str(denied.value) or "sample" in str(denied.value)


def test_a_zero_block_alignment_is_derived_rather_than_dividing_by_zero() -> None:
    payload = _wav(block_align=0)
    measurement = measure_wav(payload)

    assert measurement.block_align == BLOCK_ALIGN
    assert measurement.duration_seconds == pytest.approx(EXPECTED_SECONDS)


def test_a_compressed_encoding_reports_unknown_rather_than_a_byte_guess() -> None:
    """Bytes-to-frames is only valid for constant-bitrate PCM."""

    payload = _wav(audio_format=0x0011)  # IMA ADPCM
    measurement = measure_wav(payload)

    assert measurement.measurable is False
    assert wav_duration_seconds(payload) is None
    # Playable audio with an unknown duration is not a request failure.
    assert _wav_duration(payload) is None


def test_unknown_is_never_reported_as_zero() -> None:
    payload = _wav(audio_format=0x0011)
    assert wav_duration_seconds(payload) is not 0.0  # noqa: F632 - identity is the point
    assert wav_duration_seconds(payload) is None


# ============================== surfaces ===================================


def test_the_corrected_duration_reaches_every_surface() -> None:
    """SpeechResult, the response header, and the trace field."""

    from butters.web.app import _audio_seconds_header
    from butters.web.speech import SpeechResult

    payload = _wav(streaming=True, riff_size=PLACEHOLDER)
    seconds = _wav_duration(payload)
    result = SpeechResult(payload, "openai", "tts-1", "alloy", 1.9, seconds, 0.0003)

    # 1. SpeechResult
    assert result.audio_seconds == pytest.approx(EXPECTED_SECONDS)
    # 2. X-Butters-Audio-Seconds
    assert _audio_seconds_header(result.audio_seconds, 3) == "1.2"
    assert _audio_seconds_header(result.audio_seconds, 4) == "1.2"
    # 3. the trace TTS stage rounds the same value
    assert round(result.audio_seconds, 3) == pytest.approx(EXPECTED_SECONDS)


def test_an_unknown_duration_is_rendered_as_unknown_everywhere() -> None:
    from butters.web.app import _audio_seconds_header

    assert _audio_seconds_header(None, 3) == "unknown"
    assert _audio_seconds_header(None, 4) == "unknown"
    assert _audio_seconds_header(0.0, 3) == "0.0"


# ===================== pricing does not consume duration ===================


def test_speech_pricing_is_independent_of_duration() -> None:
    """The defect could not have mispriced anything, and still cannot."""

    import inspect

    from butters.pricing import CostBasis, speech_cost

    from butters import pricing

    # Same characters, wildly different audio: identical cost.
    quiet = speech_cost("tts-1", characters=20)
    assert quiet.amount_usd == pytest.approx(20 * 15.00 / 1_000_000)
    assert quiet.amount_usd == pytest.approx(0.0003)
    assert quiet.basis is CostBasis.INPUT_MEASURED

    source = inspect.getsource(pricing)
    for forbidden in ("audio_seconds", "duration", "getnframes", "measure_wav"):
        assert forbidden not in source, forbidden


def test_the_accepted_production_request_would_price_identically() -> None:
    """20 characters at $15/1M is $0.000300 regardless of the duration bug."""

    from butters.pricing import speech_cost

    assert speech_cost("tts-1", characters=len("Butters speech test.")).amount_usd == (
        pytest.approx(0.0003)
    )
