> [!WARNING] 
> This project is 100% vibe coded

# Photo Scanner

A Python desktop app for organizing scanned photos. Watches a directory for new scans, then automatically renames, moves, and tags them with EXIF metadata. Also includes a manual mode for batch-processing existing image files.

## Features

### Auto Mode
- Watches a directory for newly scanned images
- Automatically moves files to a target directory
- Renames files using a configurable pattern: `<Box Name> - <Caption> (<Date Caption>)`
- Handles duplicate filenames by appending `(2)`, `(3)`, etc.
- Sets `DateTimeOriginal` and `DateTimeDigitized` EXIF tags to the specified date at 12:00 PM
- Displays the last scanned image with a full image editor

### Manual Mode
- Import images by clicking to browse or drag-and-drop
- Apply the same rename and metadata update to multiple images at once
- Supports batch processing with a shared filename and date

### Image Editor (Auto Mode)
Accessible in the "Last Scanned Image" panel after a scan is processed.

**Toolbar buttons:**

| Button | Shortcut | Action |
|--------|----------|--------|
| Rotate CCW | Cmd+Shift+R | Rotate 90° counter-clockwise |
| Rotate CW | Cmd+R | Rotate 90° clockwise |
| Rotate 180° | Cmd+Option+Shift+R | Rotate 180° |
| Flip Horizontal | Cmd+F | Flip left/right |
| Flip Vertical | Cmd+Shift+F | Flip top/bottom |
| Punch In | Cmd+I | Shrink crop box by 10px (5px per side) |
| Punch Out | Cmd+O | Expand crop box by 10px (5px per side) |
| Save | Cmd+S | Save edits to the original file |

> On Windows, substitute Cmd with Ctrl (except Rotate 180°, which uses Ctrl+Alt+Shift+R).

**Crop nudge shortcuts:**

| Shortcut | Action |
|----------|--------|
| Cmd+Left | Move right edge left (shrink right) |
| Cmd+Shift+Left | Move right edge right (expand right) |
| Cmd+Right | Move left edge right (shrink left) |
| Cmd+Shift+Right | Move left edge left (expand left) |
| Cmd+Up | Move bottom edge up (shrink bottom) |
| Cmd+Shift+Up | Move bottom edge down (expand bottom) |
| Cmd+Down | Move top edge down (shrink top) |
| Cmd+Shift+Down | Move top edge up (expand top) |

**Rotation slider:** Rotates the image from -360° to 360°. The preview uses a downsampled image for performance; the full-resolution image is used when saving.

**Saving:** Writes the cropped and rotated full-resolution image back to the original file, preserving the file's original modification date.

### Smart Date Detection
The Date Caption field auto-detects dates in many formats:

- Standard dates: `December 25, 2000`, `2000-12-25`, `12/25/2000`
- Partial dates: `August 2008` → August 1, 2008 · `2008` → January 1, 2008
- Holidays: `Christmas 2000` → December 25, 2000

Supported holidays include Christmas, Thanksgiving, Easter, New Year's, Independence Day, Halloween, Mother's Day, Father's Day, Valentine's Day, Memorial Day, Labor Day, and more.

### Preferences
All settings (watch directory, target directory, box name, caption, date) are saved automatically to `~/.photo_scanner_prefs.json` and restored on next launch.

## Requirements

- Python 3.9+
- macOS or Windows

## Installation

```bash
pip install -r requirements.txt
```

### Dependencies

| Package | Purpose |
|---------|---------|
| Pillow | Image loading, editing, and saving |
| piexif | Reading and writing EXIF metadata |
| watchdog | Filesystem watching for Auto Mode |
| tkinterdnd2 | Drag-and-drop support in Manual Mode |

## Usage

```bash
python photo_scanner.py
```

## Supported Image Formats

`.jpg`, `.jpeg`, `.png`, `.tiff`, `.tif`, `.bmp`
