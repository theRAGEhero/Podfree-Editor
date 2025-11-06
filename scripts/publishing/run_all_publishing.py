#!/usr/bin/env python3
"""Run all publishing scripts in sequence."""

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
    
    print(f"📤 Running {description}...")
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
    print("📤 PUBLISHING PIPELINE - Running all publishing scripts")
    print("=" * 52)
    
    scripts = [
        ("export_castopod_chapters.py", "Castopod chapters export"),
        ("create_ghost_post.py", "Ghost blog post creation"),
        ("castopod_post.py", "Castopod episode posting"),
        ("post_to_linkedin.py", "LinkedIn posting"),
    ]
    
    completed = 0
    total = len(scripts)
    
    for script, description in scripts:
        if run_script(script, description):
            completed += 1
        print()  # Add spacing between scripts
    
    print("=" * 52)
    print(f"📤 PUBLISHING PIPELINE COMPLETE: {completed}/{total} scripts successful")
    
    if completed == total:
        print("✅ All publishing scripts completed successfully!")
        return 0
    else:
        print("⚠️  Some scripts failed. Check the output above.")
        return 1

if __name__ == "__main__":
    sys.exit(main())