# Voice

HighhX Voice lets you speak a request instead of typing it. It is built into the normal
interactive session, it is free, and it works fully offline: speech-to-text is
[whisper.cpp](https://github.com/ggml-org/whisper.cpp) running on your machine. You don't
need an account, an API key, HighhX Pro or any cloud speech service.

## Getting started

```text
highhx

❯ /voice on
```

The first time, HighhX checks what voice needs and offers to set it up. It asks before
installing or downloading anything:

```text
🎙 HighhX Voice setup
Whisper.cpp is not installed.
  → brew install whisper-cpp
Set up local voice now? [Y/n]
…
Whisper model not found.
Download required voice model (base.en, 148 MB)? [Y/n]
✓ Model base.en downloaded and verified (SHA-256)
Checking the microphone (1 second)…

🎙 Voice on
Speech-to-text: whisper.cpp (base.en)
Microphone: ready
Voice replies: enabled
Push-to-talk: press Enter on an empty line to talk · /voice off

❯                      ← Enter on an empty line
🎙 Listening...
Speak now. Press Enter when you're done · Ctrl+C cancels
◉ Transcribing...
✓ "Open YouTube and play Adhento Gani"
```

After that, `/voice on` starts at once. HighhX saves what it found, so it never reinstalls or
downloads anything again. If you decline setup, HighhX keeps working normally with typed
requests, and you can run `/voice on` again whenever you like.

## Commands

| In the session | From the shell | What it does |
|---|---|---|
| `/voice` or `/voice on` | `highhx voice` | Turn voice on (setting it up first if needed). Nothing is recorded until you press Enter on an empty line |
| `/voice off` | | Stop voice input and spoken replies |
| `/voice status` | `highhx voice status` | Show whisper.cpp, the model, the recorder, the microphone and replies |
| `/voice setup` | `highhx voice setup` | Install or repair what voice needs, then check the microphone |
| `/voice test` | `highhx voice test` | Record and transcribe a sample. Nothing is run |
| `/voice mute` / `unmute` | | Turn spoken replies off or on |
| | `highhx voice model [name]` | Show or switch the model (`tiny.en`, `base.en`, `small.en`) |

If you type `highhx voice …` inside a session, HighhX runs the matching `/voice` command. It
doesn't try to start a second session.

`highhx voice status --json`, `highhx voice model --json` and
`highhx voice test --file speech.wav` are available for scripts. The `--file` option
transcribes a 16 kHz WAV file without using the microphone.

```text
❯ /voice status

🎙 HighhX Voice

Status            ON
Speech-to-text    whisper.cpp
Model             base.en
Microphone        ready
Recorder          ready (rec)
Voice replies     enabled
Push-to-talk      ready
Setup             ready
```

When something is missing, `/voice status` names it and the fix. For example:
`Model: base.en (missing) → /voice setup`.

## Push-to-talk

HighhX never listens all the time. The microphone is on only while you are recording:

* **Start:** press **Enter** on an empty line while voice is on. `/voice on` itself never
  records.
* **Stop:** press **Enter**. A recording also stops by itself after 60 seconds.
* **Cancel:** press **Ctrl+C**.
* **Type instead:** if you type a line while recording (for example `/voice status`), the
  recording is thrown away without being transcribed, and HighhX handles what you typed.

`/voice …` commands are handled by the session itself. They never reach the resolver, the AI
agent or the HighhX Pro screen.

## Voice goes through the normal pipeline

```text
microphone → recorder → whisper.cpp → transcript
          → the same AgentREPL.handle() as typed text
          → resolver (Free) or agent (Pro) → action plan → risk → approval
          → executor → verification → audit → spoken summary
```

Voice only turns your speech into text. It has no actions, approvals or automation of its
own, and it never skips a safety check. If you say *"push"* or *"delete the production
database"*, HighhX shows the same plan and asks for the same approval as it would if you had
typed it. Typed and spoken requests are also recorded the same way. The only extra event is
`voice.heard`, which stores the transcript and its confidence.

**Unclear transcripts.** whisper.cpp gives each word a probability. If the average is below
60%, HighhX shows what it heard and asks `Run it? [y/N/e=edit]` before doing anything. Clear
transcripts run straight away, and any risky action still asks for approval. To confirm
every transcript, set `"confirm": "always"` in the voice settings file (see below).

**Replies.** Only requests you *spoke* get a spoken reply. HighhX uses your system's own
speech: macOS `say`, `espeak-ng`/`espeak` or `spd-say` on Linux, and SAPI on Windows. Typed
requests are answered in the terminal as usual.

## What setup installs, and where

| Component | macOS | Linux |
|---|---|---|
| whisper.cpp | `brew install whisper-cpp` | Homebrew if you have it. Otherwise HighhX builds whisper.cpp v1.9.4 from source in its data directory (needs git, cmake and a C++ compiler) |
| Recorder | `brew install sox` (`rec`) | `alsa-utils` (`arecord`) from your package manager (uses `sudo`) |
| Model | `ggml-base.en.bin` (148 MB) from the whisper.cpp model repository | the same |

If a recorder is already installed (`rec`, `ffmpeg` or `arecord`), HighhX uses it. The same
goes for an existing whisper.cpp (`whisper-cli`) on `PATH`.

**The model:**

* Downloaded to `~/Library/Application Support/highhx/voice/models/` on macOS, or
  `~/.local/share/highhx/voice/models/` on Linux. It is never stored in a project.
* Checked while downloading. HighhX compares the size and SHA-256 hash with the published
  values and only keeps the file if they match.
* On later starts, HighhX checks the file's size and header. It re-downloads a damaged model
  when you run `/voice setup`.

**Settings** are in `voice.json` in your HighhX configuration directory. They include the
model, the whisper.cpp path, the recorder, the microphone state, whether replies are on, and
`confirm` (`auto` or `always`). You normally never need to edit this file.

## When something goes wrong

Every problem is shown as a short message with a way to fix it. You never see a Python
traceback.

| Message | What to do |
|---|---|
| ⚠ Microphone access is unavailable. | On macOS, open **System Settings → Privacy & Security → Microphone**. Turn on access for your terminal app (Terminal, iTerm, VS Code …), quit and reopen that app, then run `/voice setup` or `/voice test`. On Linux, check that the microphone is connected and not muted. |
| Whisper.cpp is not installed. | Run `/voice setup`. On a Mac without Homebrew, install it from https://brew.sh first. |
| Whisper model … is not downloaded / damaged | Run `/voice setup`. |
| Downloading the … voice model failed. | Check your internet connection, then run `/voice setup`. |
| Transcription failed. | Run `/voice test`. If it keeps failing, run `/voice setup` to check the installation. |
| Nothing was heard. | Speak a little closer to the microphone, then press Enter on an empty line to try again. |
| Voice input is not supported on Windows yet. | Use typed requests. Spoken replies still work. |

## Advanced overrides

You never need these. They are for unusual setups:

| Variable | Effect |
|---|---|
| `HIGHHX_VOICE_RECORDER` | Prefer `rec`, `ffmpeg` or `arecord` |
| `HIGHHX_VOICE_TTS` | `say`, `espeak`, `spd-say`, `sapi` or `off` |
| `HIGHHX_WHISPER_MODEL` | Use this whisper.cpp model file instead of the one HighhX manages |

## Free and Pro

* **Free:** transcripts go to the deterministic resolver. For example: "run the tests",
  "show git status", "open YouTube and play …", "switch to Safari". They use the same plan,
  executor, automation bridge and verification as typed requests. There is no AI model and
  no account.
* **Pro:** transcripts go to the AI agent, exactly like typed requests, with the agent's
  plans and approvals.

## Platform support

* **macOS** (Apple silicon and Intel) and **Linux:** full voice input.
* **Windows:** voice input is not supported yet. Spoken replies use SAPI.
* **WSL:** no direct microphone access. Run HighhX in a native terminal instead.

## Implementation

`src/highhx/voice/`:

| Module | Role |
|---|---|
| `platform.py` | Supported platforms and the package manager to use |
| `config.py` | `voice.json`: saved settings and setup state |
| `recorder.py` | Push-to-talk recording, microphone checks, detecting digital silence (how macOS reports denied permission) |
| `whisper.py` | Finding whisper.cpp and transcribing with it, including per-token confidence from `--output-json-full` |
| `model_manager.py` | The model list, download with progress, SHA-256 checks, caching |
| `installer.py` | Installing whisper.cpp and the recorder after you agree (Homebrew, the system package manager, or a pinned source build) |
| `speech_output.py` | Spoken replies with the system's local speech |
| `voice_manager.py` | Status (detection only) and the first-run setup flow |
| `mode.py` | The session side: `/voice` commands, listening, confirmation, speaking |

`AgentREPL` adds the `/voice` command and the input-loop hook, which passes a transcript to
`handle()`. Tests use a fake microphone, whisper.cpp, package manager, model server and
speech output (`tests/unit/voice/`). CI needs no audio hardware or network.
