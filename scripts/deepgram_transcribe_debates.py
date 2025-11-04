#!/usr/bin/env python3

import asyncio
import glob
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

try:  # New Deepgram SDK v3+
    from deepgram import DeepgramClient, FileSource, PrerecordedOptions

    HAS_SDK_V3 = True
except ImportError:  # Legacy SDK v2
    from deepgram import Deepgram  # type: ignore[no-redef]

    HAS_SDK_V3 = False


def load_env_file(*candidates: Path) -> None:
    """Populate os.environ with values from the first existing candidate .env file."""
    for env_path in candidates:
        if not env_path or not env_path.is_file():
            continue
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if not key:
                continue
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            os.environ.setdefault(key, value)
        break


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
load_env_file(PROJECT_ROOT / ".env", SCRIPT_DIR / ".env")


def clean_filename(filename):
    """Clean filename for use as JSON filename - remove extension and clean characters"""
    # Remove .mp3 extension
    name = os.path.splitext(filename)[0]
    # Replace problematic characters with underscores
    name = re.sub(r'[<>:"/\\|?*]', '_', name)
    # Replace quotes and other special characters
    name = re.sub(r'["""''"]', '', name)
    # Replace multiple spaces/underscores with single underscore
    name = re.sub(r'[_\s]+', '_', name)
    # Remove leading/trailing underscores
    name = name.strip('_')
    return name

def format_timestamp(seconds):
    """Convert seconds to ISO 8601 timestamp format"""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    milliseconds = int((seconds % 1) * 1000)
    return f"PT{hours:02d}H{minutes:02d}M{secs:02d}.{milliseconds:03d}S"

def group_words_by_speaker(words):
    """Group consecutive words by the same speaker into contributions"""
    contributions = []
    if not words:
        return contributions
    
    current_speaker = None
    current_words = []
    current_start = None
    current_end = None
    
    for word in words:
        word_speaker = word.get("speaker", 0)
        word_text = word.get("word", "")
        word_start = word.get("start", 0)
        word_end = word.get("end", 0)
        
        if current_speaker != word_speaker:
            # Save previous contribution if exists
            if current_words and current_speaker is not None:
                contribution_text = " ".join(current_words).strip()
                if contribution_text:
                    contributions.append({
                        "speaker": current_speaker,
                        "text": contribution_text,
                        "start_time": current_start,
                        "end_time": current_end,
                        "words": len(current_words)
                    })
            
            # Start new contribution
            current_speaker = word_speaker
            current_words = [word_text]
            current_start = word_start
            current_end = word_end
        else:
            # Continue current contribution
            current_words.append(word_text)
            current_end = word_end
    
    # Add the last contribution
    if current_words and current_speaker is not None:
        contribution_text = " ".join(current_words).strip()
        if contribution_text:
            contributions.append({
                "speaker": current_speaker,
                "text": contribution_text,
                "start_time": current_start,
                "end_time": current_end,
                "words": len(current_words)
            })
    
    return contributions

def create_deliberation_ontology_json(response_dict, audio_filename):
    """Create JSON structure following Deliberation Ontology format"""
    
    # Extract basic data
    transcript_data = response_dict["results"]["channels"][0]["alternatives"][0]
    words = transcript_data.get("words", [])
    metadata = response_dict.get("metadata", {})
    
    # Group words into contributions
    contributions = group_words_by_speaker(words)
    
    # Get unique speakers
    speakers = list(set(contrib["speaker"] for contrib in contributions))
    speakers.sort()
    
    # Calculate total duration
    total_duration = max((word.get("end", 0) for word in words), default=0)
    
    # Create debate identifier from filename
    clean_name = clean_filename(audio_filename)
    debate_id = f"debate_{clean_name.lower()}"
    
    # Create the deliberation process structure
    readable_name = clean_name.replace('_', ' ')
    deliberation_json = {
        "@context": {
            "del": "https://w3id.org/deliberation/ontology#",
            "xsd": "http://www.w3.org/2001/XMLSchema#"
        },
        "deliberation_process": {
            "@type": "del:DeliberationProcess",
            "identifier": debate_id,
            "name": readable_name.title(),
            "topic": {
                "@type": "del:Topic",
                "identifier": f"topic_{clean_name.lower()}",
                "text": readable_name
            },
            "source_file": audio_filename,
            "duration": format_timestamp(total_duration),
            "transcription_metadata": {
                "model": metadata.get("model", "nova-2"),
                "language": "en",
                "confidence": transcript_data.get("confidence", 0),
                "processed_at": datetime.now(timezone.utc).isoformat(),
                "word_count": len(words),
                "speaker_count": len(speakers)
            }
        },
        "participants": [],
        "contributions": [],
        "statistics": {
            "total_contributions": len(contributions),
            "total_speakers": len(speakers),
            "total_words": len(words),
            "average_contribution_length": sum(c["words"] for c in contributions) / len(contributions) if contributions else 0,
            "duration_seconds": total_duration
        }
    }
    
    # Add participants
    for speaker_id in speakers:
        speaker_contributions = [c for c in contributions if c["speaker"] == speaker_id]
        total_words = sum(c["words"] for c in speaker_contributions)
        speaking_time = sum(c["end_time"] - c["start_time"] for c in speaker_contributions)
        
        participant = {
            "@type": "del:Participant",
            "identifier": f"speaker_{speaker_id}",
            "name": f"Speaker {speaker_id}",
            "role": {
                "@type": "del:Role",
                "identifier": f"debater_{speaker_id}",
                "name": "Debate Participant"
            },
            "statistics": {
                "total_contributions": len(speaker_contributions),
                "total_words": total_words,
                "speaking_time_seconds": speaking_time,
                "average_words_per_contribution": total_words / len(speaker_contributions) if speaker_contributions else 0
            }
        }
        deliberation_json["participants"].append(participant)
    
    # Add contributions
    for i, contrib in enumerate(contributions):
        contribution = {
            "@type": "del:Contribution",
            "identifier": f"contribution_{i+1:04d}",
            "text": contrib["text"],
            "madeBy": f"speaker_{contrib['speaker']}",
            "timestamp": format_timestamp(contrib["start_time"]),
            "duration": format_timestamp(contrib["end_time"] - contrib["start_time"]),
            "start_time_seconds": contrib["start_time"],
            "end_time_seconds": contrib["end_time"],
            "word_count": contrib["words"],
            "sequence_number": i + 1
        }
        deliberation_json["contributions"].append(contribution)
    
    return deliberation_json

def transcribe_single_file(input_file, api_key):
    """Transcribe a single MP3 file"""
    print(f"\n=== Processing: {input_file} ===")
    
    # Ensure Deliberation Json folder exists
    os.makedirs("Deliberation Json", exist_ok=True)
    
    # Generate output filename
    clean_name = clean_filename(input_file)
    output_file = f"Deliberation Json/{clean_name}_deliberation.json"
    raw_file = f"{clean_name}_raw.json"
    
    # Check if output already exists
    if os.path.exists(output_file):
        print(f"Output file {output_file} already exists. Skipping...")
        return True
    
    try:
        # Check file size
        file_size = os.path.getsize(input_file) / (1024 * 1024)  # MB
        print(f"File size: {file_size:.1f} MB")

        if HAS_SDK_V3:
            deepgram = DeepgramClient(api_key)
            print("Reading audio file...")
            with open(input_file, "rb") as file:
                buffer_data = file.read()

            payload = FileSource(buffer=buffer_data)
            options = PrerecordedOptions(
                model="nova-2",
                language="en",
                diarize=True,
                punctuate=True,
                paragraphs=True,
                utterances=True,
                smart_format=True,
            )
            print("Sending to Deepgram (SDK v3) for transcription...")
            response = deepgram.listen.rest.v("1").transcribe_file(payload, options)
            response_dict = response.to_dict() if hasattr(response, "to_dict") else response
        else:
            deepgram = Deepgram(api_key)  # type: ignore[call-arg]

            async def transcribe_legacy():
                print("Reading audio file...")
                with open(input_file, "rb") as audio_file:
                    source = {"buffer": audio_file, "mimetype": "audio/mp3"}
                    options = {
                        "model": "nova",
                        "punctuate": True,
                        "paragraphs": True,
                        "utterances": True,
                        "diarize": True,
                        "smart_format": True,
                    }
                    print("Sending to Deepgram (SDK v2) for transcription...")
                    return await deepgram.transcription.prerecorded(source, options)

            response_dict = asyncio.run(transcribe_legacy())
        
        print("Processing into Deliberation Ontology format...")
        
        # Create the structured JSON
        deliberation_data = create_deliberation_ontology_json(response_dict, input_file)
        
        # Write structured JSON output
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(deliberation_data, f, indent=2, ensure_ascii=False)
        
        # Save raw response for reference
        with open(raw_file, "w", encoding="utf-8") as f:
            json.dump(response_dict, f, indent=2, ensure_ascii=False)
        
        print(f"✅ Completed successfully!")
        print(f"   Structured output: {output_file}")
        print(f"   Statistics:")
        print(f"   - Speakers: {deliberation_data['statistics']['total_speakers']}")
        print(f"   - Contributions: {deliberation_data['statistics']['total_contributions']}")
        print(f"   - Words: {deliberation_data['statistics']['total_words']}")
        print(f"   - Duration: {deliberation_data['statistics']['duration_seconds']:.1f}s")
        
        return True
        
    except Exception as e:
        print(f"❌ Error processing {input_file}: {str(e)}")
        import traceback
        traceback.print_exc()
        return False

def batch_transcribe_debates():
    """Process the single MP3 file in the current directory."""
    api_key = os.getenv("DEEPGRAM_API_KEY")
    if not api_key:
        print(
            "Missing DEEPGRAM_API_KEY. Add it to the project .env file or export it "
            "before running this script."
        )
        return

    mp3_files = glob.glob("*.mp3")

    if not mp3_files:
        print("No MP3 files found in current directory.")
        return

    if len(mp3_files) > 1:
        print("Multiple MP3 files detected. Please leave only the file you want to transcribe:")
        for file in sorted(mp3_files):
            print(f" - {file}")
        return

    mp3_file = mp3_files[0]
    print("Found MP3 file to transcribe:")
    print(f" - {mp3_file}")
    print("\nStarting transcription...")
    print("=" * 80)

    clean_name = clean_filename(mp3_file)
    output_file = f"Deliberation Json/{clean_name}_deliberation.json"

    if os.path.exists(output_file):
        print(f"Output file {output_file} already exists. Delete it if you need to re-run the transcription.")
        return

    success = transcribe_single_file(mp3_file, api_key)

    print("\n" + "=" * 80)
    if success:
        print("TRANSCRIPTION COMPLETE ✅")
    else:
        print("TRANSCRIPTION FAILED ❌")

if __name__ == "__main__":
    batch_transcribe_debates()
