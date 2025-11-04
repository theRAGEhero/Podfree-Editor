#!/usr/bin/env python3
import os
import re
import subprocess
import logging
import datetime
from pathlib import Path

def parse_timestamp(timestamp_str):
    """Convert MM:SS or HH:MM:SS to seconds"""
    parts = timestamp_str.split(':')
    if len(parts) == 2:  # MM:SS
        minutes, seconds = map(int, parts)
        return minutes * 60 + seconds
    elif len(parts) == 3:  # HH:MM:SS
        hours, minutes, seconds = map(int, parts)
        return hours * 3600 + minutes * 60 + seconds
    return 0

def extract_chapters_from_md(md_file):
    """Extract chapter information from markdown file"""
    chapters = []
    
    with open(md_file, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # Find the ## Chapters section
    chapters_section = re.search(r'## Chapters\n(.*?)(?=##|\Z)', content, re.DOTALL)
    if not chapters_section:
        print("No ## Chapters section found in markdown file")
        return chapters
    
    # Extract chapters with timestamps
    chapter_lines = chapters_section.group(1).strip().split('\n')
    for line in chapter_lines:
        line = line.strip()
        if not line:
            continue
        
        # Match timestamp and title pattern (e.g., "00:13 About Trollwall AI")
        match = re.match(r'^(\d{1,2}:\d{2})\s+(.+)$', line)
        if match:
            timestamp, title = match.groups()
            chapters.append({
                'timestamp': timestamp,
                'seconds': parse_timestamp(timestamp),
                'title': title.strip()
            })
    
    return chapters

def sanitize_filename(filename):
    """Remove invalid characters from filename"""
    # Replace invalid characters with underscores
    invalid_chars = '<>:"/\\|?*'
    for char in invalid_chars:
        filename = filename.replace(char, '_')
    # Remove multiple underscores and trim
    filename = re.sub(r'_+', '_', filename).strip('_')
    return filename

def setup_logging():
    """Setup logging configuration"""
    log_filename = f"video_split_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_filename),
            logging.StreamHandler()
        ]
    )
    
    return log_filename


def parse_srt_timestamp(timestamp_str):
    """Convert SRT timestamp (HH:MM:SS,mmm) to seconds"""
    hours, minutes, rest = timestamp_str.split(':')
    seconds, milliseconds = rest.split(',')
    total_seconds = int(hours) * 3600 + int(minutes) * 60 + int(seconds)
    return total_seconds + int(milliseconds) / 1000


def load_srt_entries(srt_file):
    """Parse SRT file into a list of entries with timing metadata"""
    entries = []
    if not srt_file:
        return entries

    try:
        with open(srt_file, 'r', encoding='utf-8-sig') as f:
            content = f.read().strip()
    except FileNotFoundError:
        logging.warning(f"SRT file not found: {srt_file}")
        return entries

    if not content:
        logging.warning(f"SRT file is empty: {srt_file}")
        return entries

    blocks = re.split(r'\n\s*\n', content)
    timecode_pattern = re.compile(
        r'^(?P<start>\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(?P<end>\d{2}:\d{2}:\d{2},\d{3})$'
    )

    for block in blocks:
        lines = [line.strip('\ufeff') for line in block.strip().splitlines()]
        if len(lines) < 2:
            continue

        timecode_line = lines[1] if re.match(r'^\d+$', lines[0]) else lines[0]
        match = timecode_pattern.match(timecode_line)
        if not match:
            continue

        text_lines = lines[2:] if re.match(r'^\d+$', lines[0]) else lines[1:]
        start_time = parse_srt_timestamp(match.group('start'))
        end_time = parse_srt_timestamp(match.group('end'))

        entries.append({
            'timecode': timecode_line,
            'start': start_time,
            'end': end_time,
            'text': '\n'.join(text_lines).strip()
        })

    logging.info(f"Loaded {len(entries)} subtitles from {srt_file}")
    return entries


def write_srt_segment(entries, start_time, end_time, output_path):
    """Write a subset of SRT entries that overlap the target chapter range"""
    if not entries:
        return False

    overlapping_entries = []
    for entry in entries:
        if entry['end'] <= start_time:
            continue
        if end_time is not None and entry['start'] >= end_time:
            continue
        overlapping_entries.append(entry)

    if not overlapping_entries:
        return False

    output_lines = []
    for idx, entry in enumerate(overlapping_entries, start=1):
        output_lines.append(str(idx))
        output_lines.append(entry['timecode'])
        if entry['text']:
            output_lines.append(entry['text'])
        output_lines.append('')

    try:
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(output_lines).rstrip() + '\n')
        logging.info(f"Wrote SRT segment: {output_path}")
        return True
    except Exception as exc:
        logging.error(f"Failed to write SRT segment {output_path}: {exc}")
        return False


def split_video(mp4_file, chapters, srt_entries=None):
    """Split MP4 file based on chapters"""
    if not chapters:
        logging.error("No chapters found to split")
        print("❌ No chapters found to split")
        return
    
    # Sort chapters by timestamp
    chapters.sort(key=lambda x: x['seconds'])
    
    total_chapters = len(chapters)
    logging.info(f"Starting to process {total_chapters} chapters")
    print(f"\n🎬 Starting video splitting process...")
    print(f"📊 Total chapters to process: {total_chapters}")
    print("=" * 50)
    
    for i, chapter in enumerate(chapters, 1):
        start_time = chapter['seconds']
        
        print(f"\n📹 Processing chapter {i}/{total_chapters}")
        print(f"🏷️  Title: {chapter['title']}")
        print(f"⏰ Timestamp: {chapter['timestamp']} ({start_time}s)")
        
        logging.info(f"Processing chapter {i}/{total_chapters}: {chapter['title']}")
        
        # Calculate duration (next chapter start - current chapter start)
        next_chapter_start = chapters[i]['seconds'] if i < len(chapters) else None
        duration = (next_chapter_start - start_time) if next_chapter_start is not None else None
        
        if duration:
            print(f"⏳ Duration: {duration}s")
        else:
            print("⏳ Duration: Until end of video")
        
        # Create folder name from chapter title
        folder_name = sanitize_filename(chapter['title'])
        folder_path = Path(folder_name)
        
        print(f"📁 Creating folder: {folder_name}")
        logging.info(f"Creating folder: {folder_name}")
        
        try:
            folder_path.mkdir(exist_ok=True)
            print(f"✅ Folder created successfully")
        except Exception as e:
            logging.error(f"Failed to create folder {folder_name}: {e}")
            print(f"❌ Failed to create folder: {e}")
            continue
        
        # Output filenames
        output_file = folder_path / f"{folder_name}.mp4"
        srt_output_file = folder_path / f"{folder_name}.srt"
        
        # Check if file already exists and has reasonable size
        if output_file.exists() and output_file.stat().st_size > 1024 * 1024:  # At least 1MB
            file_size = output_file.stat().st_size / (1024 * 1024)  # MB
            print(f"⏭️  Skipping - file already exists: {output_file}")
            print(f"📦 Existing file size: {file_size:.2f} MB")
            logging.info(f"Skipping {chapter['title']} - file already exists ({file_size:.2f} MB)")
            
            if srt_entries:
                srt_exists = srt_output_file.exists() and srt_output_file.stat().st_size > 0
                if not srt_exists:
                    created = write_srt_segment(srt_entries, start_time, next_chapter_start, srt_output_file)
                    if created:
                        print(f"📝 Created missing subtitle segment: {srt_output_file.name}")
                        logging.info(f"Subtitle segment created for skipped chapter {chapter['title']}")
                    else:
                        print("⚠️  Unable to create subtitle segment for this chapter")
                        logging.warning(f"Subtitle segment missing for {chapter['title']}")
            
            print(f"✅ Chapter {i}/{total_chapters} skipped")
            print("-" * 40)
            continue
        
        # Build ffmpeg command with high quality settings
        cmd = [
            'ffmpeg',
            '-i', str(mp4_file),
            '-ss', str(start_time),
            '-c:v', 'libx264',  # High quality video codec
            '-crf', '18',       # Constant Rate Factor (18 = visually lossless)
            '-preset', 'slow',  # Better compression efficiency
            '-c:a', 'aac',      # High quality audio codec
            '-b:a', '320k',     # High audio bitrate (320 kbps)
            '-ar', '48000',     # High sample rate (48 kHz)
            '-avoid_negative_ts', 'make_zero'
            # Removed -y flag since we now check for existing files
        ]
        
        if duration:
            cmd.extend(['-t', str(duration)])
        
        cmd.append(str(output_file))
        
        print(f"🎞️  Splitting video...")
        logging.info(f"Running ffmpeg command: {' '.join(cmd)}")
        
        try:
            result = subprocess.run(cmd, check=True, capture_output=True, text=True)
            print(f"✅ Successfully created: {output_file}")
            logging.info(f"Successfully created {output_file}")
            
            # Check if file actually exists and has reasonable size
            if output_file.exists() and output_file.stat().st_size > 1000:  # At least 1KB
                file_size = output_file.stat().st_size / (1024 * 1024)  # MB
                print(f"📦 File size: {file_size:.2f} MB")
                logging.info(f"Output file size: {file_size:.2f} MB")
            else:
                logging.warning(f"Output file {output_file} seems too small or doesn't exist")
                print(f"⚠️  Warning: Output file seems too small")

            if srt_entries:
                created = write_srt_segment(srt_entries, start_time, next_chapter_start, srt_output_file)
                if created:
                    print(f"📝 Subtitle segment saved as: {srt_output_file.name}")
                else:
                    print("⚠️  No subtitle lines found for this chapter range")
                    logging.warning(f"No subtitle segment created for {chapter['title']}")
                
        except subprocess.CalledProcessError as e:
            error_msg = f"Error creating {output_file}: {e.stderr}"
            logging.error(error_msg)
            print(f"❌ Error creating video: {e.stderr}")
            print(f"🔧 Command that failed: {' '.join(cmd)}")
            continue
        except Exception as e:
            error_msg = f"Unexpected error processing {chapter['title']}: {e}"
            logging.error(error_msg)
            print(f"❌ Unexpected error: {e}")
            continue
        
        print(f"✅ Chapter {i}/{total_chapters} completed")
        print("-" * 40)

def main():
    # Setup logging
    log_filename = setup_logging()
    
    print("🚀 Video Splitting Script Started")
    print("=" * 50)
    logging.info("Video splitting script started")
    logging.info(f"Log file: {log_filename}")
    
    # Find .md, .mp4, and .srt files in current directory
    current_dir = Path('.')
    
    print("🔍 Scanning current directory for files...")
    logging.info("Scanning for .md, .mp4, and .srt files")
    
    md_files = list(current_dir.glob('*.md'))
    mp4_files = list(current_dir.glob('*.mp4'))
    srt_files = list(current_dir.glob('*.srt'))
    
    if not md_files:
        error_msg = "No .md file found in current directory"
        logging.error(error_msg)
        print(f"❌ {error_msg}")
        return
    
    if not mp4_files:
        error_msg = "No .mp4 file found in current directory"
        logging.error(error_msg)
        print(f"❌ {error_msg}")
        return
    
    if len(md_files) > 1:
        warning_msg = f"Multiple .md files found: {[f.name for f in md_files]}"
        logging.warning(warning_msg)
        print(f"⚠️  {warning_msg}")
        print(f"📄 Using: {md_files[0].name}")
    
    if len(mp4_files) > 1:
        warning_msg = f"Multiple .mp4 files found: {[f.name for f in mp4_files]}"
        logging.warning(warning_msg)
        print(f"⚠️  {warning_msg}")
        print(f"🎥 Using: {mp4_files[0].name}")
    
    md_file = md_files[0]
    mp4_file = mp4_files[0]
    srt_file = srt_files[0] if srt_files else None
    
    print(f"\n📄 Processing markdown file: {md_file.name}")
    print(f"🎥 Processing video file: {mp4_file.name}")
    if srt_file:
        print(f"🗒️ Subtitle file: {srt_file.name}")
    else:
        print("ℹ️  No subtitle (.srt) file detected")
    
    logging.info(f"Using markdown file: {md_file.name}")
    logging.info(f"Using video file: {mp4_file.name}")
    if srt_file:
        logging.info(f"Using subtitle file: {srt_file.name}")
    else:
        logging.info("No subtitle file detected")
    
    # Check if ffmpeg is available
    print("\n🔧 Checking ffmpeg availability...")
    try:
        subprocess.run(['ffmpeg', '-version'], capture_output=True, check=True)
        print("✅ ffmpeg is available")
        logging.info("ffmpeg is available")
    except (subprocess.CalledProcessError, FileNotFoundError):
        error_msg = "ffmpeg is not installed or not available in PATH"
        logging.error(error_msg)
        print(f"❌ {error_msg}")
        print("💡 Please install ffmpeg to continue")
        return
    
    # Extract chapters
    print("\n📖 Extracting chapters from markdown file...")
    logging.info("Extracting chapters from markdown file")
    
    chapters = extract_chapters_from_md(md_file)
    
    if not chapters:
        error_msg = "No chapters found in markdown file"
        logging.error(error_msg)
        print(f"❌ {error_msg}")
        return
    
    print(f"✅ Found {len(chapters)} chapters:")
    logging.info(f"Found {len(chapters)} chapters")
    
    for chapter in chapters:
        print(f"   ⏰ {chapter['timestamp']} - {chapter['title']}")
        logging.info(f"Chapter: {chapter['timestamp']} - {chapter['title']}")

    # Load subtitle entries if available
    srt_entries = []
    if srt_file:
        print("\n📝 Loading subtitles...")
        logging.info("Loading subtitle file")
        srt_entries = load_srt_entries(srt_file)
        if srt_entries:
            print(f"✅ {len(srt_entries)} subtitle entries loaded")
            logging.info(f"{len(srt_entries)} subtitle entries loaded")
        else:
            print("⚠️  No subtitle entries found in the provided .srt file")
            logging.warning(f"No entries found in subtitle file {srt_file}")
    
    # Get video file size for reference
    video_size = mp4_file.stat().st_size / (1024 * 1024 * 1024)  # GB
    print(f"\n📊 Original video size: {video_size:.2f} GB")
    logging.info(f"Original video size: {video_size:.2f} GB")
    
    # Split video
    split_video(mp4_file, chapters, srt_entries)
    
    print("\n" + "=" * 50)
    print("🎉 Video splitting completed!")
    print(f"📝 Log file saved as: {log_filename}")
    logging.info("Video splitting process completed successfully")

if __name__ == "__main__":
    main()
