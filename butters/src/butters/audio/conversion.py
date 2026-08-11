"""Bounded, streaming PCM channel and sample-rate conversion."""

from __future__ import annotations

import sys
from array import array


def _decode_samples(data: bytes, sample_width: int) -> list[int]:
    if sample_width == 1:
        return [(value - 128) << 8 for value in data]
    if sample_width == 2:
        values = array("h")
        values.frombytes(data)
        if sys.byteorder != "little":
            values.byteswap()
        return values.tolist()
    if sample_width == 3:
        decoded: list[int] = []
        for offset in range(0, len(data), 3):
            value = int.from_bytes(data[offset : offset + 3], "little", signed=True)
            decoded.append(value >> 8)
        return decoded
    if sample_width == 4:
        values = array("i")
        values.frombytes(data)
        if sys.byteorder != "little":
            values.byteswap()
        return [value >> 16 for value in values]
    raise ValueError(f"unsupported PCM sample width: {sample_width} bytes")


def decode_pcm_to_mono(data: bytes, sample_width: int, channels: int) -> list[int]:
    if channels < 1:
        raise ValueError("channel count must be positive")
    frame_width = sample_width * channels
    if len(data) % frame_width:
        raise ValueError("PCM data ends in the middle of an input frame")
    samples = _decode_samples(data, sample_width)
    if channels == 1:
        return samples

    mono: list[int] = []
    for offset in range(0, len(samples), channels):
        channel_sum = sum(samples[offset : offset + channels])
        magnitude = abs(channel_sum) // channels
        mono.append(-magnitude if channel_sum < 0 else magnitude)
    return mono


def pcm16_bytes(samples: list[int]) -> bytes:
    clamped = array("h", (max(-32768, min(32767, value)) for value in samples))
    if sys.byteorder != "little":
        clamped.byteswap()
    return clamped.tobytes()


class LinearResampler:
    """Stateful linear interpolator with a small, bounded carry buffer."""

    def __init__(self, input_rate: int, output_rate: int) -> None:
        if input_rate <= 0 or output_rate <= 0:
            raise ValueError("sample rates must be positive")
        self.input_rate = input_rate
        self.output_rate = output_rate
        self._samples: list[int] = []
        self._position_numerator = 0
        self._total_input = 0
        self._total_output = 0
        self._finished = False

    @property
    def buffered_samples(self) -> int:
        return len(self._samples)

    def feed(self, samples: list[int]) -> list[int]:
        if self._finished:
            raise RuntimeError("cannot feed a finished resampler")
        if not samples:
            return []
        self._samples.extend(samples)
        self._total_input += len(samples)
        output = self._emit_available()
        self._compact()
        return output

    def _emit_available(self) -> list[int]:
        output: list[int] = []
        while self._position_numerator // self.output_rate + 1 < len(self._samples):
            left_index, remainder = divmod(self._position_numerator, self.output_rate)
            left = self._samples[left_index]
            right = self._samples[left_index + 1]
            interpolated = (
                left * (self.output_rate - remainder) + right * remainder
            ) // self.output_rate
            output.append(interpolated)
            self._position_numerator += self.input_rate
            self._total_output += 1
        return output

    def _compact(self) -> None:
        if not self._samples:
            return
        consumed = min(
            self._position_numerator // self.output_rate,
            len(self._samples) - 1,
        )
        if consumed:
            del self._samples[:consumed]
            self._position_numerator -= consumed * self.output_rate

    def finish(self) -> list[int]:
        if self._finished:
            return []
        self._finished = True
        if not self._samples:
            return []
        target_output = (
            self._total_input * self.output_rate + self.input_rate // 2
        ) // self.input_rate
        self._samples.append(self._samples[-1])
        output: list[int] = []
        while self._total_output < target_output:
            left_index, remainder = divmod(self._position_numerator, self.output_rate)
            if left_index + 1 >= len(self._samples):
                self._samples.append(self._samples[-1])
            left = self._samples[left_index]
            right = self._samples[left_index + 1]
            output.append(
                (left * (self.output_rate - remainder) + right * remainder)
                // self.output_rate
            )
            self._position_numerator += self.input_rate
            self._total_output += 1
        self._samples.clear()
        return output


class StreamingPcmConverter:
    """Convert arbitrary integer PCM chunks to 16 kHz mono S16_LE."""

    def __init__(
        self,
        *,
        input_rate: int,
        input_channels: int,
        input_sample_width: int,
        output_rate: int = 16_000,
    ) -> None:
        if input_sample_width not in {1, 2, 3, 4}:
            raise ValueError("only 8-, 16-, 24-, and 32-bit PCM are supported")
        self.input_channels = input_channels
        self.input_sample_width = input_sample_width
        self._resampler = LinearResampler(input_rate, output_rate)
        self._finished = False

    @property
    def buffered_samples(self) -> int:
        return self._resampler.buffered_samples

    def convert(self, data: bytes) -> bytes:
        if self._finished:
            raise RuntimeError("cannot convert after finish")
        mono = decode_pcm_to_mono(
            data,
            sample_width=self.input_sample_width,
            channels=self.input_channels,
        )
        return pcm16_bytes(self._resampler.feed(mono))

    def finish(self) -> bytes:
        if self._finished:
            return b""
        self._finished = True
        return pcm16_bytes(self._resampler.finish())
