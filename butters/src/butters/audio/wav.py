"""Bounded RIFF/WAVE inspection that does not trust declared chunk sizes.

The first paid OpenAI speech request returned a *streaming* WAV: both the RIFF
chunk size and the `data` chunk size were the 0xFFFFFFFF placeholder a writer
emits when the total length is not known up front. Python's `wave` module
takes those fields at face value, so `getnframes()` returned 2147483647 and a
1.2 second clip was reported as 89478.485 seconds.

The bytes actually received are the trustworthy quantity. This module walks
the chunk structure properly - no fixed 44-byte header, no assumption that
`data` is the first or only chunk, and padding handled - then measures the
audio from the PCM bytes that are really present, using a declared length only
when it is plausible.

Nothing here is used for billing. Speech is priced from submitted characters
or from provider-reported tokens; duration is reported metadata.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

# A writer that does not know the final length emits one of these. 0xFFFFFFFF
# is what OpenAI sends; 0 appears in some streaming writers; the signed
# maximum shows up once a reader has already divided a placeholder down.
_PLACEHOLDER_SIZES = frozenset({0xFFFFFFFF, 0x7FFFFFFF, 0})
_PCM = 0x0001
_EXTENSIBLE = 0xFFFE
# Real files carry a handful of chunks. This only bounds a malformed or
# hostile payload; it is never reached by ordinary audio.
_MAX_CHUNKS = 64


class WavFormatError(ValueError):
    """The payload is not interpretable as RIFF/WAVE."""


@dataclass(frozen=True, slots=True)
class WavMeasurement:
    channels: int
    sample_width: int
    sample_rate: int
    block_align: int
    data_offset: int
    data_bytes: int
    declared_data_bytes: int | None
    streaming: bool
    audio_format: int

    @property
    def frames(self) -> int:
        return self.data_bytes // self.block_align

    @property
    def duration_seconds(self) -> float:
        return self.frames / self.sample_rate

    @property
    def measurable(self) -> bool:
        """Whether a byte count converts to a duration for this encoding.

        Only constant-bitrate PCM has a trustworthy bytes-to-frames
        relationship. A compressed payload does not, and this refuses to
        pretend otherwise.
        """

        return self.audio_format in {_PCM, _EXTENSIBLE} and self.block_align > 0


def measure_wav(payload: bytes) -> WavMeasurement:
    """Parse a RIFF/WAVE payload, preferring received bytes over claims."""

    if len(payload) < 12:
        raise WavFormatError("payload is too short to be RIFF/WAVE")
    if payload[0:4] != b"RIFF" or payload[8:12] != b"WAVE":
        raise WavFormatError("payload is not a RIFF/WAVE container")

    audio_format = channels = sample_rate = block_align = bits = 0
    data_offset: int | None = None
    declared_data: int | None = None
    total = len(payload)
    offset = 12

    for _ in range(_MAX_CHUNKS):
        if offset + 8 > total:
            break
        chunk_id = payload[offset : offset + 4]
        (declared,) = struct.unpack_from("<I", payload, offset + 4)
        body = offset + 8

        if chunk_id == b"fmt ":
            if declared < 16 or body + 16 > total:
                raise WavFormatError("fmt chunk is truncated")
            audio_format, channels, sample_rate, _byte_rate, block_align, bits = (
                struct.unpack_from("<HHIIHH", payload, body)
            )
        elif chunk_id == b"data":
            data_offset = body
            declared_data = declared
            # The data chunk is the last one Butters needs; anything after it
            # cannot change how many audio bytes were received.
            break

        if declared in _PLACEHOLDER_SIZES:
            # A placeholder on a non-data chunk leaves the chunk sequence
            # unwalkable, so stop rather than guess at the next offset.
            break
        # RIFF pads odd-length chunk bodies to an even boundary.
        offset = body + declared + (declared & 1)

    if sample_rate == 0 or channels == 0:
        raise WavFormatError("fmt chunk is missing or declares no audio")
    if data_offset is None:
        raise WavFormatError("no data chunk was found")

    if block_align == 0:
        # Derive it when the writer omitted it, rather than dividing by zero.
        block_align = channels * max(1, (bits + 7) // 8)
    if block_align <= 0:
        raise WavFormatError("block alignment is not positive")

    available = max(0, total - data_offset)
    streaming = declared_data in _PLACEHOLDER_SIZES or declared_data > available
    # Trust a declared length only when it fits inside what actually arrived.
    data_bytes = available if streaming else declared_data

    return WavMeasurement(
        channels=channels,
        sample_width=max(1, (bits + 7) // 8),
        sample_rate=sample_rate,
        block_align=block_align,
        data_offset=data_offset,
        data_bytes=data_bytes,
        declared_data_bytes=None if declared_data in _PLACEHOLDER_SIZES else declared_data,
        streaming=streaming,
        audio_format=audio_format,
    )


def wav_duration_seconds(payload: bytes) -> float | None:
    """Duration of the audio actually received, or None when unmeasurable.

    `None` is a deliberate third state. Returning 0.0 for audio that plainly
    exists would be a lie of the same kind as the 89478-second reading this
    replaces, and callers can render "unknown" honestly.
    """

    measurement = measure_wav(payload)
    if not measurement.measurable:
        return None
    return measurement.duration_seconds
