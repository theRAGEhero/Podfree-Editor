# Podfree Toolkit

Podfree bundles the episode automation scripts and the web UI into a single folder so you can review assets, run utilities, and edit notes without juggling files in the same directory.

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
- `post_to_linkedin.py`
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

Restart `app/server.py` after updating the values. When you open the app you'll be redirected to `/login.html`; signing in drops you straight into the Projects dashboard. Use the **Logout** link in the header to end the session or switch accounts.

The player page lets you:
- Switch between the original video and a lightweight proxy (and create the proxy with ffmpeg if it is missing) while keeping transcript highlighting in sync.
- View the toolbox of scripts and run them; jobs execute inside the selected workspace and all logs stream to the terminal running `server.py`.

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

## Requirements

- Python 3.10+
- `pip install pillow requests markdown` (and any extra dependencies you need for the automation scripts, e.g. ffmpeg on your PATH)
- Optional: create a `.env` inside the workspace for API keys (Groq, Ghost, LinkedIn, etc.); the scripts keep the same environment expectations as before.

## Tips

- Use the Projects page to refresh a project after running scripts so the summary reflects new files.
- The server streams job logs to the terminal, so you can watch progress and diagnose failures without leaving the command line.
- The player falls back to the original MP4 automatically if the proxy is missing; click **Lightweight** to trigger generation.
