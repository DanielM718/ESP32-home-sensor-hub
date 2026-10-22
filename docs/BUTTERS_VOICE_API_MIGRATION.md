# Butters voice API migration

Reviewed 2026-10-22 against OpenAI's official deprecation, text-to-speech,
speech-to-text, Realtime, and pricing documentation.

## Existing pipeline

The production voice path is deliberately modular:

1. `SherpaOnnxWakeWordDetector` performs local streaming wake-word detection,
   with local pre-roll and the existing activation chime.
2. `AlsaAudioSource` captures mono 16 kHz signed PCM through `arecord`.
   The audio frontend performs conversion, buffering, and VAD locally.
3. `SherpaOnnxStreamingTranscriber` performs normal microphone and browser
   transcription with local ONNX models. Browser PCM WebSockets also use the
   local recognizer pool.
4. The deterministic/local router or the optional OpenAI Responses provider
   produces text. Conversation locks, trace data, and usage accounting are
   independent of speech I/O.
5. Piper is the default local TTS engine. When paid cloud TTS is explicitly
   enabled, the OpenAI adapter synthesizes through a short Realtime WebSocket
   and returns a WAV to the unchanged player and HTTP boundary.
6. `OpenAISTTProvider` is only the optional bounded-WAV Admin transcription
   path. It is not the normal live microphone recognizer.

Speech provider profiles live in SQLite `ai_provider_profiles`. The older
named `voice_presets` table remains supported. Credentials use the dedicated
credential store, with `OPENAI_API_KEY` as the existing environment fallback.
Static defaults and paid-feature gates remain in `assistant.toml`; paid STT
and TTS remain disabled by default.

## Deprecation findings

The deployed configuration used `gpt-4o-mini-tts` and
`gpt-4o-mini-transcribe`.

| Existing family | Shutdown | Replacement | Endpoint consequence |
| --- | --- | --- | --- |
| `tts-1`, `tts-1-hd`, and `gpt-4o-mini-tts` snapshots/family | 2027-01-06 | `gpt-realtime-2.1-mini` | Not supported by `/v1/audio/speech`; requires a Realtime API workflow. |
| `whisper-1`, `gpt-4o-transcribe`, `gpt-4o-mini-transcribe`, `gpt-4o-transcribe-diarize` | 2027-02-26 | `gpt-transcribe` or `gpt-live-transcribe` | `gpt-transcribe` remains compatible with `/v1/audio/transcriptions`; `gpt-live-transcribe` is for live Realtime transcription. |

Butters does not use an OpenAI Realtime model for wake-word detection or its
normal live STT path. Migrating those local components to
`gpt-live-transcribe` would be an architectural and privacy change, not a
required endpoint substitution. It was therefore not done. Likewise, legacy
Realtime model retirements do not affect this repository because no such
model was configured.

Official sources:

- <https://developers.openai.com/api/docs/deprecations>
- <https://developers.openai.com/api/docs/guides/text-to-speech>
- <https://developers.openai.com/api/docs/guides/speech-to-text>
- <https://developers.openai.com/api/docs/guides/realtime>
- <https://developers.openai.com/api/docs/guides/voice-websockets>
- <https://developers.openai.com/api/docs/pricing>

## Implemented migration

- The only selectable OpenAI speech model is `gpt-realtime-2.1-mini`, with the
  documented Realtime voice set. Numeric speed is not advertised or sent.
  Style remains available as bounded session instructions.
- Cloud TTS now sends `session.update`, `conversation.item.create`, and
  `response.create`; it validates streamed base64 PCM, terminal status,
  timeouts, byte limits, and usage, then returns a 24 kHz mono WAV.
- Optional file transcription now uses `gpt-transcribe` at the existing
  multipart `/v1/audio/transcriptions` boundary.
- Retired model IDs receive a distinct `model_retired` error with shutdown and
  replacement guidance. Authentication, rate limiting, timeout, unavailable,
  malformed response, and configuration errors remain distinguishable.
- Existing profiles are migrated when loaded. Compatible voices are retained;
  voices absent from Realtime (`fable`, `nova`, and `onyx`) fall back to
  `alloy`. Legacy speed becomes neutral `1.0`. Both AI profiles and legacy
  presets are written back only after successful validation.
- Realtime usage makes successful TTS cost `provider_reported`. Admission
  still reserves a conservative ceiling before opening a paid connection.

No dependency change is required: the existing `websockets>=15,<16`
dependency supplies the synchronous server-side client.

## Validation boundaries

Automated tests mock all OpenAI HTTP and WebSocket traffic. No paid call is
part of the suite. A hardware validation after deployment should:

1. Confirm local wake, VAD, sherpa transcription, Piper playback, and browser
   microphone transcription with paid speech disabled.
2. In Admin, install a nonproduction project key and explicitly enable paid
   TTS/STT within a small test budget.
3. Preview two supported Realtime voices and confirm returned WAV playback,
   latency, and usage accounting.
4. Submit a short prerecorded WAV to the optional cloud transcription preview
   and confirm `gpt-transcribe` is recorded.
5. Exercise an invalid voice, an expired key, and a forced timeout to verify
   the distinct operator errors.

Do not run those steps with production credentials or restart production as
part of code review.

## Deployment after other work lands

After the Desktop Agent branch has been merged, merge or rebase this feature
branch through the normal review process and resolve only actual overlap.
Deploy the resulting revision with the repository's standard sealed
configuration workflow. Preserve `/etc/butters/assistant.local.toml`, the
credential store, and SQLite state. Review the effective Admin speech profile
before enabling paid voice, then restart through the normal deployment
procedure and perform the hardware validation above. No schema command or
manual database rewrite is required; profile migration occurs on read.
