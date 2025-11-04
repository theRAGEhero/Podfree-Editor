#!/usr/bin/env python3

import os
import re
import requests
import io
import logging
from PIL import Image, ImageDraw, ImageFont, ImageOps
import glob


logger = logging.getLogger(__name__)
BACKGROUND_REMOVED_FILENAME = "guest-BackgroudRemoved.png"

def find_md_file():
    """Find the .md file containing Guest and Thumbnail sections"""
    md_files = glob.glob("*.md")
    if not md_files:
        raise FileNotFoundError("No .md file found in the current directory")
    
    for md_file in md_files:
        try:
            with open(md_file, 'r', encoding='utf-8') as f:
                content = f.read()
            if '## Guest' in content and '## Thumbnail' in content:
                return md_file
        except:
            continue
    
    raise ValueError(f"No .md file found with '## Guest' and '## Thumbnail' sections in: {md_files}")

def parse_md_file(filename):
    """Parse the markdown file to extract guest name and thumbnail text"""
    with open(filename, 'r', encoding='utf-8') as f:
        content = f.read()
    
    guest_match = re.search(r'^## Guest\s*\n(.*?)(?=^##|\Z)', content, re.MULTILINE | re.DOTALL)

    guest_name = ""
    if guest_match:
        guest_section_lines = [line.strip() for line in guest_match.group(1).splitlines()]
        guest_section_lines = [line for line in guest_section_lines if line]
        if guest_section_lines:
            guest_name = guest_section_lines[0]

    thumbnail_match = re.search(r'^## Thumbnail\s*\n(.*?)(?=^##|\Z)', content, re.MULTILINE | re.DOTALL)

    thumbnail_text = ""
    if thumbnail_match:
        raw_lines = thumbnail_match.group(1).splitlines()

        blocks = []
        current_block = []
        for line in raw_lines:
            stripped = line.strip()

            if not stripped:
                if current_block:
                    blocks.append(current_block)
                    current_block = []
                continue

            if stripped.startswith('-'):
                if current_block:
                    blocks.append(current_block)
                current_block = [line.rstrip()]
            else:
                if current_block:
                    current_block.append(line.rstrip())
                else:
                    current_block = [line.rstrip()]

        if current_block:
            blocks.append(current_block)

        selected_block = None
        for block in blocks:
            first_non_empty = next((line.strip() for line in block if line.strip()), "")
            if first_non_empty.startswith('-'):
                selected_block = block
                break

        if selected_block:
            cleaned_block = []
            for idx, block_line in enumerate(selected_block):
                stripped_line = block_line.strip()
                if idx == 0 and stripped_line.startswith('-'):
                    stripped_line = stripped_line.lstrip('-').strip()
                cleaned_block.append(stripped_line)
            thumbnail_text = "\n".join(cleaned_block).strip()

    return guest_name, thumbnail_text

def parse_title_text(text):
    """Parse title text with highlighting (words between *asterisks*)"""
    lines = text.strip().split('\n')
    parsed_lines = []
    
    for line in lines:
        parts = re.split(r'(\*[^*]+\*)', line)
        line_parts = []
        
        for part in parts:
            if part.startswith('*') and part.endswith('*'):
                line_parts.append({
                    'text': part[1:-1],
                    'highlight': True
                })
            elif part.strip():
                line_parts.append({
                    'text': part,
                    'highlight': False
                })
        
        if line_parts:
            parsed_lines.append(line_parts)
    
    return parsed_lines

def find_guest_image():
    """Find guest image file"""
    image_extensions = ['.jpg', '.jpeg', '.png', '.JPG', '.JPEG', '.PNG']
    possible_names = ['guest', 'gueste']
    
    for name in possible_names:
        for ext in image_extensions:
            filename = name + ext
            if os.path.exists(filename):
                return filename
    
    return None

def remove_background(image_path, api_key="M6rFYefU22wgYvcXuALvRCcY"):
    """Remove background using remove.bg API"""
    if not api_key:
        logger.warning("No remove.bg API key provided, using original image")
        return None

    try:
        with open(image_path, 'rb') as image_file:
            response = requests.post(
                'https://api.remove.bg/v1.0/removebg',
                files={'image_file': image_file},
                data={'size': 'auto'},
                headers={'X-Api-Key': api_key},
                timeout=30
            )
        
        if response.status_code == 200:
            logger.info("Background removed successfully via remove.bg")
            img = Image.open(io.BytesIO(response.content)).convert('RGBA')
            try:
                img.save(BACKGROUND_REMOVED_FILENAME, "PNG")
                logger.info("Cached background-removed image as %s", BACKGROUND_REMOVED_FILENAME)
            except Exception:
                logger.exception("Failed to cache background-removed image to %s", BACKGROUND_REMOVED_FILENAME)
            return img
        else:
            logger.error("remove.bg API error %s: %s", response.status_code, response.text)
            return None
    except Exception:
        logger.exception("Error removing background for %s", image_path)
        return None

def load_guest_image():
    """Load and process guest image"""
    # Prefer cached background-removed image if available
    if os.path.exists(BACKGROUND_REMOVED_FILENAME):
        try:
            logger.info("Using cached background-removed guest image %s", BACKGROUND_REMOVED_FILENAME)
            return Image.open(BACKGROUND_REMOVED_FILENAME).convert('RGBA')
        except Exception:
            logger.exception("Failed to load cached background-removed image %s", BACKGROUND_REMOVED_FILENAME)

    image_path = find_guest_image()
    if not image_path:
        return None

    try:
        # Try to remove background first
        bg_removed_img = remove_background(image_path)
        if bg_removed_img:
            return bg_removed_img
        
        # Fallback to original image
        logger.info("Using original guest image without background removal")
        img = Image.open(image_path)
        return img.convert('RGBA')
    except Exception:
        logger.exception("Error loading guest image %s", image_path)
        return None

def create_circular_mask(size):
    """Create a circular mask for the image"""
    mask = Image.new('L', (size, size), 0)
    draw = ImageDraw.Draw(mask)
    draw.ellipse((0, 0, size, size), fill=255)
    return mask

def get_font(size, bold=False):
    """Get font with fallback options - matching HTML: 'Gill Sans', 'Gill Sans MT', Calibri, 'Trebuchet MS', sans-serif"""
    font_names = [
        # Try to match HTML font family first
        'GillSans-Bold.ttf' if bold else 'GillSans.ttf',
        'GillSansMT-Bold.ttf' if bold else 'GillSansMT.ttf', 
        'calibrib.ttf' if bold else 'calibri.ttf',
        'trebucbd.ttf' if bold else 'trebuc.ttf',
        # Fallbacks
        'DejaVuSans-Bold.ttf' if bold else 'DejaVuSans.ttf',
        'arial.ttf' if not bold else 'arialbd.ttf',
        'helvetica.ttf',
        'Helvetica.ttc'
    ]
    
    for font_name in font_names:
        try:
            return ImageFont.truetype(font_name, size)
        except:
            continue
    
    try:
        return ImageFont.load_default()
    except:
        return None

def draw_text_with_highlights(draw, parsed_lines, start_x, start_y, font_size, max_width, color='white', highlight_color='#a0004a', align='center', line_spacing_multiplier=1.2):
    """Draw text with highlighted parts"""
    font_regular = get_font(font_size, bold=True)
    if not font_regular:
        return start_y
    
    line_height = int(font_size * line_spacing_multiplier)
    current_y = start_y
    
    for line_parts in parsed_lines:
        line_width = 0
        
        # Calculate total line width 
        for part in line_parts:
            text_bbox = draw.textbbox((0, 0), part['text'], font=font_regular)
            text_width = text_bbox[2] - text_bbox[0]
            if part['highlight']:
                line_width += text_width + 40  # padding for highlight
            else:
                line_width += text_width
        
        # Determine starting X position based on alignment
        if align == 'center':
            current_x = start_x + (max_width - line_width) // 2
        else:  # left align
            current_x = start_x
        
        # First pass: Draw all highlight backgrounds
        temp_x = current_x
        for part in line_parts:
            text = part['text']
            text_bbox = draw.textbbox((0, 0), text, font=font_regular)
            text_width = text_bbox[2] - text_bbox[0]
            text_height = text_bbox[3] - text_bbox[1]
            
            if part['highlight']:
                highlight_padding = 20
                # Calculate proper rectangle dimensions based on text bbox
                highlight_rect = [
                    temp_x,
                    current_y + text_bbox[1] - highlight_padding//2,  # Top of text minus padding
                    temp_x + text_width + highlight_padding * 2,
                    current_y + text_bbox[3] + highlight_padding//2   # Bottom of text plus padding
                ]
                draw.rectangle(highlight_rect, fill=highlight_color)
                temp_x += text_width + highlight_padding * 2
            else:
                temp_x += text_width
        
        # Second pass: Draw all text on top
        for part in line_parts:
            text = part['text']
            text_bbox = draw.textbbox((0, 0), text, font=font_regular)
            text_width = text_bbox[2] - text_bbox[0]
            
            if part['highlight']:
                highlight_padding = 20
                # Draw text on top of highlight
                draw.text((current_x + highlight_padding, current_y), text, font=font_regular, fill='white')
                current_x += text_width + highlight_padding * 2
            else:
                # Draw regular text
                draw.text((current_x, current_y), text, font=font_regular, fill=color)
                current_x += text_width
        
        current_y += line_height
    
    return current_y

def generate_youtube_cover(guest_name, title_text, guest_image=None):
    """Generate YouTube cover (1920x1080) - following original HTML grid layout"""
    width, height = 1920, 1080
    img = Image.new('RGB', (width, height), '#01013d')
    draw = ImageDraw.Draw(img)
    
    # Remove both top and bottom padding, keep horizontal padding and gap
    padding_horizontal = 180
    padding_vertical_top = 0    # Remove top spacing
    padding_vertical_bottom = 0  # Remove bottom spacing too
    gap = 96
    
    # Calculate grid areas
    available_width = width - (2 * padding_horizontal) - gap  # 1920 - 360 - 96 = 1464
    text_area_width = (available_width * 2) // 3  # 2fr = 976px
    image_area_width = available_width // 3       # 1fr = 488px
    available_height = height - padding_vertical_top - padding_vertical_bottom  # 1080 - 0 - 0 = 1080px (full height)
    
    # Text area positioning
    text_x = padding_horizontal  # 180px
    text_area_right = text_x + text_area_width  # 180 + 976 = 1156px
    
    # Image area positioning  
    image_x = text_area_right + gap  # 1156 + 96 = 1252px
    
    # Parse title
    parsed_title = parse_title_text(title_text)
    
    # Start text near the very top with minimal spacing
    font_size = int(105 * 1.05)  # Increase by 5%: 105 * 1.05 = 110.25 → 110px
    line_height = int(font_size * 1.5)  # Further increased line spacing from 1.3 to 1.5
    text_y = 50 + font_size  # Start very close to top with minimal padding
    
    # Draw title in left column (2fr) - left aligned with increased line spacing
    draw_text_with_highlights(draw, parsed_title, text_x, text_y, font_size, text_area_width, align='left', line_spacing_multiplier=1.5)
    
    # Guest image in right column (1fr) - even bigger with horizontal centering
    if guest_image:
        img_size = 520  # Even bigger - increased from 450px
        # Center image horizontally within the right column (1fr area)
        # image_x = 1252px, image_area_width = 488px, img_size = 520px
        # Since img_size > image_area_width, center within available space
        img_x = image_x + (image_area_width - img_size) // 2  # This will be: 1252 + (488-520)//2 = 1252 - 16 = 1236px
        
        # Add more spacing from the top for better positioning
        img_y = 200  # More spacing from top - moved down from 50px
        
        # Fit guest image into target square without stretching
        guest_resized = ImageOps.fit(guest_image, (img_size, img_size), method=Image.Resampling.LANCZOS)
        
        # Handle transparency properly
        if guest_image.mode == 'RGBA':
            # Create a background color for transparent areas - using main cover blue
            background = Image.new('RGB', (img_size, img_size), '#01013d')  # Main cover background color
            background.paste(guest_resized, mask=guest_resized.split()[-1] if guest_resized.mode == 'RGBA' else None)
            guest_resized = background
        
        # Create circular image
        mask = create_circular_mask(img_size)
        guest_circular = Image.new('RGBA', (img_size, img_size), (0, 0, 0, 0))
        guest_circular.paste(guest_resized, (0, 0))
        guest_circular.putalpha(mask)
        
        # Paste guest image with proper alpha handling
        img.paste(guest_circular, (img_x, img_y), guest_circular)
        
        # Draw border
        border_width = 4
        draw.ellipse([
            img_x - border_width,
            img_y - border_width,
            img_x + img_size + border_width,
            img_y + img_size + border_width
        ], outline=(255, 255, 255, 40), width=border_width)
        
        guest_text_y = img_y + img_size + 80  # More spacing below image - increased from 48px
        guest_text_x = img_x + img_size // 2  # Center under image
    else:
        guest_text_y = height - 150  # Move up when no image
        guest_text_x = image_x + image_area_width // 2  # Center in image area
    
    # Guest info (centered in image column)
    if guest_name:
        guest_font = int(40 * 1.2)  # Increase "with" by 20%: 40 * 1.2 = 48px
        guest_name_font = int(50 * 1.2)  # Increase guest name by 20%: 50 * 1.2 = 60px
        guest_font = get_font(guest_font)
        guest_name_font = get_font(guest_name_font, bold=True)
        
        with_text = "with "
        with_bbox = draw.textbbox((0, 0), with_text, font=guest_font)
        with_width = with_bbox[2] - with_bbox[0]
        
        name_bbox = draw.textbbox((0, 0), guest_name, font=guest_name_font)
        name_width = name_bbox[2] - name_bbox[0]
        
        total_width = with_width + name_width
        start_x = guest_text_x - total_width // 2
        
        draw.text((start_x, guest_text_y), with_text, font=guest_font, fill='#e0e0e0')
        draw.text((start_x + with_width, guest_text_y), guest_name, font=guest_name_font, fill='white')
    
    return img

def generate_podcast_cover(guest_name, title_text, guest_image=None):
    """Generate Podcast cover (3000x3000) - text in top 2/3, image in bottom 1/3"""
    size = 3000
    img = Image.new('RGB', (size, size), '#01013d')
    draw = ImageDraw.Draw(img)
    
    # Parse title
    parsed_title = parse_title_text(title_text)
    
    # Calculate layout: top 2/3 for text, bottom 1/3 for image - starting from top
    text_area_height = (size * 2) // 3  # 2000px
    image_area_height = size // 3       # 1000px
    image_area_start_y = text_area_height  # 2000px
    
    # Start text from near the top with even less padding
    font_size = int(250 * 1.05)  # Increase by 5%: 250 * 1.05 = 262.5 → 262px
    line_height = int(font_size * 1.2)
    title_y = 50 + font_size  # Further reduced top padding from 100px to 50px
    
    # Draw title starting from top - center aligned
    draw_text_with_highlights(draw, parsed_title, 0, title_y, font_size, size, align='center')
    
    # Guest image in bottom 1/3 - move closer to text by reducing spacing
    if guest_image:
        img_size = 800  # Fit better in bottom 1/3
        img_x = (size - img_size) // 2
        img_y = image_area_start_y - 150  # Move up by reducing spacing - picture goes up into text area
        
        # Fit guest image into target square without stretching
        guest_resized = ImageOps.fit(guest_image, (img_size, img_size), method=Image.Resampling.LANCZOS)
        
        # Handle transparency properly
        if guest_image.mode == 'RGBA':
            # Create a background color for transparent areas - using main cover blue
            background = Image.new('RGB', (img_size, img_size), '#01013d')  # Main cover background color
            background.paste(guest_resized, mask=guest_resized.split()[-1] if guest_resized.mode == 'RGBA' else None)
            guest_resized = background
        
        # Create circular image
        mask = create_circular_mask(img_size)
        guest_circular = Image.new('RGBA', (img_size, img_size), (0, 0, 0, 0))
        guest_circular.paste(guest_resized, (0, 0))
        guest_circular.putalpha(mask)
        
        # Paste guest image with proper alpha handling
        img.paste(guest_circular, (img_x, img_y), guest_circular)
        
        # Draw border
        border_width = 26
        draw.ellipse([
            img_x - border_width,
            img_y - border_width,
            img_x + img_size + border_width,
            img_y + img_size + border_width
        ], outline=(255, 255, 255, 46), width=border_width)
        
        # Guest text below image in bottom 1/3
        guest_text_y = img_y + img_size + 80
    else:
        guest_text_y = image_area_start_y + 500  # Position in bottom section if no image
    
    # Guest info (increased font sizes)
    if guest_name:
        guest_font = get_font(110)  # Increased from 96px
        guest_name_font = get_font(110, bold=True)  # Increased from 96px
        
        with_text = "with "
        with_bbox = draw.textbbox((0, 0), with_text, font=guest_font)
        with_width = with_bbox[2] - with_bbox[0]
        
        name_bbox = draw.textbbox((0, 0), guest_name, font=guest_name_font)
        name_width = name_bbox[2] - name_bbox[0]
        
        total_width = with_width + name_width
        start_x = (size - total_width) // 2
        
        draw.text((start_x, guest_text_y), with_text, font=guest_font, fill='#e0e0e0')
        draw.text((start_x + with_width, guest_text_y), guest_name, font=guest_name_font, fill='white')
    
    return img

def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    try:
        # Find and parse MD file
        md_file = find_md_file()
        guest_name, thumbnail_text = parse_md_file(md_file)

        logger.info("Found markdown file: %s", md_file)

        if not guest_name:
            raise ValueError("Guest name not found in markdown file")
        logger.info("Guest name: %s", guest_name)

        if not thumbnail_text:
            raise ValueError("Thumbnail title not found in markdown file")
        logger.info("Selected thumbnail text:\n%s", thumbnail_text)

        # Load guest image
        guest_image = load_guest_image()
        if not guest_image:
            raise FileNotFoundError("Guest picture not found. Expected a file like guest.jpg/png")
        logger.info("Guest image loaded successfully")
        
        # Generate YouTube cover
        youtube_cover = generate_youtube_cover(guest_name, thumbnail_text, guest_image)
        youtube_filename = "youtube-cover.jpg"
        youtube_cover.save(youtube_filename, "JPEG", quality=95)
        logger.info("YouTube cover saved as %s", youtube_filename)
        
        # Generate Podcast cover
        podcast_cover = generate_podcast_cover(guest_name, thumbnail_text, guest_image)
        podcast_filename = "podcast-cover.jpg"
        podcast_cover.save(podcast_filename, "JPEG", quality=95)
        logger.info("Podcast cover saved as %s", podcast_filename)
        
    except Exception as e:
        logger.error("Script halted: %s", e)
        return 1
    
    return 0

if __name__ == "__main__":
    exit(main())
