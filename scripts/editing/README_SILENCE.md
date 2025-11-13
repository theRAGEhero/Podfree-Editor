# Silence Detection and Removal

This plugin provides a two-step workflow for detecting and removing silence from audio files.

## Workflow

### Step 1: Detect Silence
Run the **Detect Silence** script to analyze your audio file and identify silent sections.

```bash
python detect_silence.py [audio_file] [--noise-threshold -50dB] [--min-duration 0.5]
```

**Parameters:**
- `audio_file` (optional): Path to the audio file. Auto-detects if omitted.
- `--noise-threshold`: Volume threshold in dB (default: `-50dB`). Lower values = stricter silence detection.
  - `-30dB`: Very quiet (catches very subtle silence)
  - `-50dB`: Moderate (recommended default)
  - `-60dB`: Permissive (only catches very obvious silence)
- `--min-duration`: Minimum silence duration in seconds (default: `0.5`). Shorter values catch brief pauses.

**Output:**
Creates a JSON file named `<audio_basename>_silence.json` containing:
- All detected silence intervals (start, end, duration)
- Metadata about detection settings
- Summary statistics

**Example:**
```bash
# Detect silence with default settings
python detect_silence.py podcast.mp3

# Use stricter silence detection
python detect_silence.py podcast.mp3 --noise-threshold -40dB --min-duration 1.0
```

### Step 2: Remove Silence (Render)
Run the **Remove Silence (Render)** script to process the audio and remove tagged silence sections.

```bash
python remove_silence.py [silence_tags_json] [--output cleaned_audio.mp3]
```

**Parameters:**
- `silence_tags_json` (optional): Path to the silence tags JSON file. Auto-detects if omitted.
- `--output`: Output file path (default: `<original>_no_silence.mp3`)

**Output:**
Creates a new audio file with silent sections removed, maintaining high audio quality (320kbps MP3).

**Example:**
```bash
# Remove silence using auto-detected tags
python remove_silence.py

# Specify custom output path
python remove_silence.py podcast_silence.json --output podcast_clean.mp3
```

## How It Works

1. **Detection** uses ffmpeg's `silencedetect` filter to analyze the audio waveform
2. **Tags** are saved as JSON with precise timestamps for each silent section
3. **Rendering** uses ffmpeg to extract non-silent segments and concatenate them seamlessly

## Tips

- **Start with defaults**: The default settings (`-50dB`, `0.5s`) work well for most podcasts
- **Adjust threshold**: If too much is removed, increase threshold (e.g., `-40dB`)
- **Adjust duration**: If brief pauses are missed, decrease min-duration (e.g., `0.3`)
- **Preview tags**: Check the `*_silence.json` file before rendering to verify detection
- **Non-destructive**: Original files are never modified; new files are created

## Supported Formats

- MP3, WAV, FLAC, M4A, AAC, OGG
- Output is always high-quality MP3 (320kbps, 48kHz)
