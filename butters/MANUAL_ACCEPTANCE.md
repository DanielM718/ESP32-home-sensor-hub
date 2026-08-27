# Manual acceptance checklist

Physical and browser checks that automated tests in this repository cannot
cover. No JavaScript runtime is available here, so every browser behaviour
below is asserted only as a source contract in the test suite and must still be
confirmed by hand before deployment.

Run these against the private Tailscale URL, on a build of
`feature/butters-remote-interaction-completion`. Nothing in this list requires
weakening WebAuthn, Origin, CSRF, or peer binding to perform. If a step cannot
be completed without weakening one of those, stop and treat it as a failure.

## Desktop browser

- [ ] Open Butters through the private Tailscale HTTPS URL.
- [ ] The session initializes and the composer becomes usable.
- [ ] Block `/api/session` (offline mode or devtools) and reload: the header
      shows a connection error, and the send button and microphone stay
      withheld. Typing and pressing Enter sends nothing.
- [ ] Send five text turns in a row; each answers and the composer recovers.
- [ ] Send twenty text turns; no leak, no stall, composer still recovers.
- [ ] Clear during an active text turn: the pending reply never appears, the
      conversation empties, and a new turn works immediately afterwards.
- [ ] Confirm a late reply cannot repopulate the cleared view (clear right
      after sending a slow request, then wait past its completion).
- [ ] Spoken playback begins for a reply when Voice is On.
- [ ] Stop Speaking is offered while synthesis is still being fetched, not only
      once audio starts, and pressing it stops the turn from speaking at all.
- [ ] Clear during TTS playback stops the audio immediately.
- [ ] Toggle Voice: Off, confirm no playback; reload and confirm the preference
      persisted; toggle back on.
- [ ] Drop the network mid-turn and restore it: the UI reports that it stopped
      waiting rather than claiming the request failed, and recovers.
- [ ] Restart the Butters service with the browser tab still open, then send a
      text turn: the session renews once and the turn is delivered exactly
      once. Confirm in the service log that only one turn was executed.
- [ ] Repeat the restart with a slow request in flight and confirm no duplicate
      execution appears in the log.

## WebAuthn and privileged actions

- [ ] Request a privileged desktop action; the pending-action card appears and
      nothing executes yet.
- [ ] Passkey assertion succeeds and the exact action executes.
- [ ] Cancel the passkey prompt: nothing executes and no elevation is granted.
- [ ] Lock Now returns the session to locked and hides the elevated controls.
- [ ] Leave the session elevated for ten minutes and confirm the elevation
      expires on its own.
- [ ] Restart the service while a pending action is displayed, then send a text
      turn. The session renews, the stale action card is dismissed, and the
      action is **not** replayed automatically.
- [ ] "Wake my desktop and tell me when it is reachable" still requires the
      same passkey ceremony as a plain wake, and the summary shown before
      authentication names the action being authorized.
- [ ] After authorizing it, the reply reports actual reachability rather than
      only that a wake was sent.

## Voice

- [ ] First microphone use prompts for permission and then records.
- [ ] Partial transcripts appear while speaking.
- [ ] The final transcript appears once and is not duplicated.
- [ ] A second voice turn works immediately after the first.
- [ ] Clear while recording stops capture, releases the microphone, and closes
      the socket.
- [ ] Clear after transcription while the backend is still processing: the
      answer never appears in the cleared conversation.
- [ ] Cancel a voice turn after transcription has finished. Confirm the reply
      is either genuinely stopped or reported honestly as possibly still
      running - it must never claim a cancellation that did not happen.
- [ ] Kill the socket mid-turn (restart the service) and confirm the mic
      control recovers and the next tap works.
- [ ] Deny microphone permission, then grant it and retry successfully.
- [ ] The operating-system microphone indicator disappears after every stop,
      including after Clear and after an error.
- [ ] Voice reply plays back; Stop Speaking works; Clear during playback works.
- [ ] After a service restart, a voice turn reports that the connection was
      refused and invites another tap. Confirm the previously captured audio is
      **not** silently re-sent.

## iPhone Safari

- [ ] Microphone permission prompt appears and recording starts.
- [ ] Partial and final transcription both appear.
- [ ] A second voice turn works immediately afterwards.
- [ ] Audio autoplay behaviour is acceptable for the first reply.
- [ ] Stop Speaking works.
- [ ] Clear works and leaves the UI usable.
- [ ] At the narrowest viewport, the header controls (auth, lock, voice toggle,
      clear) are all reachable and none is clipped or overlapped. **This is the
      one item with no automated or reasoned backing in this pass: the CSS was
      deliberately left unchanged because it could not be visually verified
      here. If controls are unusable, a minimal responsive fix is still owed.**
- [ ] The keyboard opens on tap and the composer is not obscured.
- [ ] The microphone indicator is released after each turn.

## Security

- [ ] Copy a session cookie and CSRF token from one tailnet identity and use
      them from a second identity: `/ws/voice` refuses the connection before
      any listening or cancelled event is returned.
- [ ] The same copied session is refused on the HTTP surface.
- [ ] An unauthorized identity still receives 403 on the admin routes.
- [ ] A desktop action still requires the current passkey flow end to end.
- [ ] Inspect the model-visible catalog on a build with a reasoning provider
      configured in a scratch environment: it contains no ACTION, no
      administrator observation, and no arbitrary execution surface.

## Must remain true after deployment

- [ ] `llm.enabled`, `cloud.enabled`, and `allow_paid_calls` are all still
      false in the deployed configuration.
- [ ] No provider credentials were added.
- [ ] No local conversational model was downloaded or deployed.
