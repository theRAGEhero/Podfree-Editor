# Podfree Toolkit

Podfree bundles the episode automation scripts and the web UI into a single folder so you can review assets, run utilities, and edit notes without juggling files in the same directory.

**Repository:** [github.com/theRAGEhero/Podfree-Editor](https://github.com/theRAGEhero/Podfree-Editor)

## Layout

```
Podfree/
├─ app/
│  ├─ server.py          # local web server powering the UI
│  └─ static/            # HTML/CSS/JS front-end (player + notes editor)
└─ scripts/              # automation scripts (run from the UI or manually)
```

The automation scripts are copied from the original workflow and renamed with consistent snake_case filenames:

- `generate_covers.py`
- `fix_transcription.py`
- `create_ghost_post.py`
- `castopod_post.py`
- `deepgram_transcribe_debates.py`
- `generate_chapters.py`
- `export_castopod_chapters.py`
- `prepare_linkedin_post.py`
- `post_to_linkedin.py`
- `identify_participants.py`
- `split_video.py`
- `summarization.py`
- `extract_audio_from_video.py`

## Setup & Running the app

1. *(Recommended)* create and activate a virtual environment, then install dependencies:
   ```bash
   cd Podfree
   python3 -m venv .venv
   source .venv/bin/activate  # On Windows: .venv\Scripts\activate
   pip install -U pip
   pip install -r requirements.txt
   ```
   This installs Pillow, Requests, Markdown, and the Deepgram SDK used by `deepgram_transcribe_debates.py`.
2. Start the server:
   ```bash
   python3 app/server.py --port 8000
   ```
3. Duplicate the example environment file and adjust the values:
   ```bash
   cp .env.example .env
   # edit .env with your secrets
   ```
4. Open `http://localhost:8000/projects.html` to create a project and upload its assets (at minimum upload the video; notes, audio, and transcripts are optional and can be added later).
5. From the Projects page, click **Open in Player** (or **Open in Notes**) to activate that project as the current workspace; the player/notes views will load automatically.

## Authentication

The Podfree UI is gated by a lightweight login screen. Configure the credentials in the environment (or in a `.env` file that sits alongside `Podfree/`) before starting the server:

```
PODFREE_USERNAME=your_username
PODFREE_PASSWORD=your_password
DEEPGRAM_API_KEY=your_deepgram_key
```

The chapter generator requires an OpenRouter API key and (optionally) a default author name for Castopod exports:

```
OPENROUTER_API_KEY=sk-or-your-key
PODCAST_AUTHOR=Your Name
PODFREE_LLM_MODEL=deepseek/deepseek-r1
```

### LLM defaults

Most automation scripts use the model referenced by `PODFREE_LLM_MODEL`. Popular OpenRouter choices and their prices (USD per 1M tokens) are:

- `deepseek/deepseek-r1` — $0.42 input / $2.11 output
- `anthropic/claude-3.7-sonnet` — $3.16 input / $15.79 output
- `anthropic/claude-3.5-haiku` — $0.84 input / $4.21 output

Override the model per command with `--model` if you need something different.

Restart `app/server.py` after updating the values. When you open the app you'll be redirected to `/login.html`; signing in drops you straight into the Projects dashboard. Use the **Logout** link in the header to end the session or switch accounts.

The player page lets you:
- Switch between the original video and a lightweight proxy (and create the proxy with ffmpeg if it is missing) while keeping transcript highlighting in sync.
- View the toolbox of scripts and run them; jobs execute inside the selected workspace and all logs stream to the terminal running `server.py`.

The video page (`light_editor.html`) focuses on instant scrubbing of the proxy file, shows a rolling spectrogram with diarized speaker colours, and provides quick controls for skip marks, speed changes, selections, and ad‑hoc markers.

The notes editor page provides a dual-pane Markdown editor with live preview. Saving writes back to the notes file under the workspace.

The projects page (`projects.html`) lets you create named workspaces, upload source files directly through the browser, and jump into the player/editor with a single click.

## Script execution

When you start a script from the UI, the server executes the corresponding file from `Podfree/scripts` with the workspace folder as the working directory. Any output (covers, JSON files, etc.) is generated right inside the workspace, so the assets stay together even though the scripts live in the Podfree folder.

You can still run the scripts manually, for example:
```bash
cd Podfree/scripts
python3 generate_covers.py
```
Just remember to pass `--help` or run from the workspace directory if the script expects relative paths.

### Automatic chapter generation

- Requires `OPENROUTER_API_KEY` in the environment.
- Defaults to the latest Deepgram `Deliberation Json/*.json` transcript in the workspace and targets the Notes.md in the same folder.
- Refreshes the `## Chapters` section inside Notes.md so downstream tooling (and you!) have a clean outline.
- Run manually with `python3 generate_chapters.py` (add `--help` for customization) or trigger it from the Projects toolbox in the web UI.

### Castopod chapters export

- Once the Notes.md chapter list looks good, run `python3 export_castopod_chapters.py --output Castopod/<name>_chapters.json` (or use the “Export Castopod Chapters” button) to generate Podcasting 2.0 JSON.
- The exporter reads the `## Chapters` bullets (`- [mm:ss] Title — summary`) and writes `startTime`, `title`, and `description` entries.
- Feed that JSON into `castopod_post.py` or upload it manually when publishing the episode.

### LinkedIn workflow

1. Run `python3 prepare_linkedin_post.py` (or launch “Draft LinkedIn Post” from the toolbox) to let OpenRouter craft a draft for the `## LinkedIn` section in Notes.md. Adjust the generated copy as needed.
2. When you are ready to publish, run `python3 post_to_linkedin.py` (or trigger “Post to LinkedIn”). The script reads the finalized copy from Notes.md and, unless `--dry-run` is supplied, publishes it to your LinkedIn organization page.
3. Configure LinkedIn credentials via `LINKEDIN_CLIENT_ID`, `LINKEDIN_CLIENT_SECRET`, `LINKEDIN_REDIRECT_URI`, and optionally `LINKEDIN_ORG_VANITY` / `LINKEDIN_HASHTAGS` in your `.env`.

### Participant name extraction

- Run `python3 identify_participants.py` (or the **Identify Participants** button in the toolbox) to have an LLM infer the interviewer and guest names from the latest Deepgram transcript.
- The script updates the `## Interviewer` and `## Guest` sections in Notes.md, which also keeps the editor header in sync after a workspace refresh.

## Requirements

- Python 3.10+
- `pip install pillow requests markdown` (and any extra dependencies you need for the automation scripts, e.g. ffmpeg on your PATH)
- Optional: create a `.env` inside the workspace for API keys (Groq, Ghost, LinkedIn, etc.); the scripts keep the same environment expectations as before.

## Tips

- Use the Projects page to refresh a project after running scripts so the summary reflects new files.
- The server streams job logs to the terminal, so you can watch progress and diagnose failures without leaving the command line.
- The player falls back to the original MP4 automatically if the proxy is missing; click **Lightweight** to trigger generation.

## Docker deployment notes

The container entrypoint now normalizes permissions on `/app/data`, `/app/logs`, and `/app/public` to UID/GID `1000`. When those paths are mounted as volumes (for example, by the RSS analysis workflow that stores its SQLite database under `/app/data`), the directories are created and ownership is corrected automatically before the application starts.

_Made by [alexoppo.com](https://alexoppo.com) ♥_
