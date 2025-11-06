#!/usr/bin/env python3
"""Run all editing scripts in sequence."""

import os
import subprocess
import sys
from pathlib import Path

def run_script(script_name, description):
    """Run a script and handle errors."""
    script_path = Path(__file__).parent / script_name
    if not script_path.exists():
        print(f"⚠️  {script_name} not found, skipping...")
        return False
    
    print(f"🔄 Running {description}...")
    try:
        result = subprocess.run([sys.executable, str(script_path)], 
                              capture_output=True, text=True, check=True)
        print(f"✅ {description} completed")
        if result.stdout:
            print(f"   Output: {result.stdout.strip()}")
        return True
    except subprocess.CalledProcessError as e:
        print(f"❌ {description} failed")
        if e.stderr:
            print(f"   Error: {e.stderr.strip()}")
        return False
    except Exception as e:
        print(f"❌ {description} failed: {e}")
        return False

def main():
    print("🎬 EDITING PIPELINE - Running all editing scripts")
    print("=" * 50)
    
    scripts = [
        ("extract_audio_from_video.py", "Audio extraction"),
        ("split_video.py", "Video splitting"),
        ("fix_transcription.py", "Transcription fixes"),
    ]
    
    completed = 0
    total = len(scripts)
    
    for script, description in scripts:
        if run_script(script, description):
            completed += 1
        print()  # Add spacing between scripts
    
    print("=" * 50)
    print(f"🎬 EDITING PIPELINE COMPLETE: {completed}/{total} scripts successful")
    
    if completed == total:
        print("✅ All editing scripts completed successfully!")
        return 0
    else:
        print("⚠️  Some scripts failed. Check the output above.")
        return 1

if __name__ == "__main__":
    sys.exit(main())