# Computer Recorder

Record computer activity locally for a study, then share the recording with the
research team. The recorder saves **screenshots and mouse/keyboard events**,
including typed keys. It does not produce a continuous video or record audio.

This recorder-only fork is adapted from the
[Workflow Induction Toolkit (WIT)](https://github.com/zorazrw/workflow-induction-toolkit).
The original full repository is preserved on
[`archive/full-workflow-induction-toolkit`](https://github.com/joebaumann/computer-recorder/tree/archive/full-workflow-induction-toolkit).
The `main` branch contains the recorder and its dependencies, without workflow
induction or AI processing. No API key or cloud account is needed to record.

## 1. Install

Requirements: **macOS**, **Python 3.10 or newer**, and **Git**. The source recorder
uses Apple's Quartz framework; Windows and Linux are not supported.

Open Terminal and run:

```bash
git clone --depth 1 --single-branch --branch main https://github.com/joebaumann/computer-recorder.git
cd computer-recorder
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

The shallow clone downloads the current recorder version without the old toolkit
history. If you already cloned the repository, enter its folder and follow the
environment creation and installation commands above.

## 2. Allow recording

In **System Settings → Privacy & Security**, allow the app that runs the recorder
(for example, Terminal, iTerm, or VS Code) to use:

- **Screen Recording** (or **Screen & System Audio Recording**, depending on macOS).
- **Accessibility**, for mouse and keyboard events.
- **Input Monitoring**, if macOS requests it.

Restart that app if macOS asks you to, then return to the repository and run
`source .venv/bin/activate` again.

## 3. Record your study session

Close unrelated windows and avoid entering passwords or other private information
while recording: screenshots and typed keys are captured locally.

```bash
crec
```

Keep Terminal open while you perform the study task. To stop, return to Terminal,
press **Ctrl+C**, and wait for the command to finish.

The files are saved to:

```text
~/Downloads/records/
├── actions.db       # Mouse and keyboard event log
└── screenshots/     # JPEG screenshots around interactions
```

Do a short trial before the study: click recognizable targets near different
corners of a test window, type synthetic text, and scroll several times without
moving the pointer. Stop recording, then check that `screenshots/` contains
readable images and `actions.db` exists. In `click_left(...)` images, the red box
should surround the clicked spot. In `key_press(...)` images it marks the mouse
position, not the text caret. Scrolling is sampled; this is not continuous video.
Move or rename the trial's `records` folder before starting the actual study.
Each run uses the same output folder, so also move or rename earlier recordings
before starting another session.

## 4. Share the recording

After the recorder has stopped:

1. Open **Finder → Downloads** and locate `records`.
2. Right-click the entire `records` folder and choose **Compress “records”**.
3. Rename the ZIP using the participant/session identifier supplied by the study
   team, then send it using the team's designated upload link or sharing method.

Share the whole folder, including `actions.db`, `screenshots/`, and any other files
inside it. Nothing is uploaded automatically. Keep the local copy until the study
team confirms receipt. Do not commit recordings to this repository.

## Options and troubleshooting

Run `crec --help` to see the available options. `python -m crec` runs the same
recorder. If `crec` is not found, activate `.venv` from the repository folder again.
If screenshots are blank or events are missing, check the permissions above and
restart the app running the recorder.

Optional scroll filtering settings:

```bash
crec --scroll-debounce 0.5 \
     --scroll-min-distance 5.0 \
     --scroll-max-frequency 10 \
     --scroll-session-timeout 2.0
```

These are the defaults. They control the minimum time between sampled scroll
events, accumulated scroll distance in input-event units (not pointer movement),
maximum event frequency, and inactivity before a new scroll session. The first
nonzero scroll of a new session is captured even if small. Use the defaults
unless the study team instructs otherwise.

## Attribution and license

This fork modifies WIT's packaging, documentation, and recorder support code to
provide standalone local recording. WIT's recording tool was itself modified from
[GUM](https://github.com/generalusermodels/gum). The original
[Apache 2.0 license](LICENSE) is retained.

Original WIT research:

> Wang, Zora Zhiruo; Shao, Yijia; Shaikh, Omar; Fried, Daniel; Neubig, Graham;
> and Yang, Diyi. *How AI Agents Do Human Work? Comparing AI and Human Workflows
> Across Diverse Occupations* (2025).
> [arXiv:2510.22780](https://arxiv.org/abs/2510.22780).
