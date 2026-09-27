# Voice

Voice is an interface onto the interactive session, not a separate product:

```text
voice ─▶ record (push-to-talk) ─▶ local speech-to-text ─▶ "Heard: …" ─▶ you confirm
      ─▶ session.handle(text) ─▶ resolver (Free) or agent (Pro) ─▶ action engine ─▶ spoken summary
```

The same text typed at the prompt takes exactly the same path. Voice has no business logic,
no separate approvals and no separate actions.

## Using it

```text
highhx voice            # the session with voice on
/voice on | off         # inside the session
/voice mute | unmute    # spoken replies
/voice status           # engines in use
highhx voice --check    # what is available on this machine
```

With voice on, press **Enter on an empty line** to start recording and **Enter** again to
stop. The transcript is shown — `Run it? [Y/n/e=edit]` — and nothing runs until you accept
(or correct) it. Approvals for risky actions still apply. Ctrl+C cancels a recording and stops
speech. The microphone is never open except between those two presses.

## Engines

Everything runs locally; nothing is uploaded and no language model is involved in
recognition.

| Role | Supported | Selection |
|---|---|---|
| Recording | `rec` (sox), `ffmpeg` (macOS avfoundation, Linux ALSA), `arecord` | `HIGHHX_VOICE_RECORDER` |
| Speech-to-text | whisper.cpp CLI (`whisper-cli` / `whisper-cpp`) with a model file; Vosk with a model directory | `HIGHHX_VOICE_STT`, `HIGHHX_WHISPER_MODEL`, `HIGHHX_VOSK_MODEL` |
| Speech | macOS `say`, `espeak-ng`/`espeak`, `spd-say`, Windows SAPI | `HIGHHX_VOICE_TTS` |

Each variable also accepts `off`. Without a speech-to-text engine, voice mode still speaks the
outcomes of typed requests and says what is missing.

Setup example (macOS): `brew install sox whisper-cpp`, download a model such as
`ggml-base.en.bin`, then `export HIGHHX_WHISPER_MODEL=/path/to/ggml-base.en.bin`.

## Free and Pro

* **Free:** transcripts go to the deterministic resolver — "run the tests", "deploy staging",
  "show git status". Open-ended requests get the same Pro panel as typed ones, spoken as one
  sentence ("AI debugging requires HighhX Pro."). Speech recognition is local and
  deterministic in the sense that matters for Free: no model call, no account, no network.
* **Pro:** transcripts go to the AI agent — conversational, multi-step, with the agent's
  approvals and plans.

## What is spoken

One short sentence per request: the action outcome ("run the tests: done."), the agent's
summary (Markdown and code removed, a few sentences at most), or the capability panel's
reason.

## Implementation

`highhx/voice/engines.py` (detection and the local programs), `highhx/voice/mode.py` (the
push-to-talk loop, confirmation, speaking), `AgentREPL` (the `/voice` command and the hook in
the input loop). Tests use fake engines to drive the real session end to end
(`tests/unit/actions/test_agent_and_boundaries.py`).
