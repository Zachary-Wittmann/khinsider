# KHInsider Personal Downloader

The **KHInsider Personal Downloader** is a maintained personal fork of the original `khinsider.py` downloader. It is designed to download publicly accessible KHInsider soundtrack albums by album ID or album URL while keeping downloaded files organized under a local `downloads/` directory.

This version was updated for personal use, local archival workflows, clearer Python structure, and safer repository hygiene. It is not intended as an upstream replacement or a request to merge changes back into the original project.

## Current Version: **1.0.0.0**

This current version supports album downloads by ID or URL, preferred audio format selection, optional image downloads, search mode, preview/list-only mode, forced re-downloads, timeout control, delay control, and output paths that remain rooted under `downloads/`.

## Table of Contents

1. [Project Introduction](#project-introduction)
2. [Project Purpose](#project-purpose)
3. [Project Contents](#project-contents)
4. [Installation](#installation)
5. [Usage](#usage)
6. [Command Line Options](#command-line-options)
7. [Download Directory Behavior](#download-directory-behavior)
8. [Examples](#examples)
9. [Attribution](#attribution)

### Project Introduction

This project provides a Python-based command line downloader for KHInsider soundtrack albums. The script accepts either the album slug from a KHInsider URL or the full album URL, then downloads the available track files into a local project directory.

Unlike the original script behavior, this personal version defaults all downloaded albums to the `downloads/` folder. This keeps generated album files separate from source code and prevents large audio files from being accidentally committed to version control.

### Project Purpose

This fork was created to keep a working, personally maintained version of the KHInsider downloader after changes to the live website made older parsing logic unreliable. The main goals of this version are:

- Preserve the simple command line workflow of the original downloader.
- Update parsing and request handling for the current KHInsider page structure.
- Keep all downloaded albums under `downloads/` by default.
- Add clearer CLI options for output, format preference, image handling, previewing, and re-download behavior.
- Improve Python code organization, naming, and type hints for easier personal maintenance.

### Project Contents

| Filename | Type | Description |
| --------------- | --------------- | --------------- |
| `README` | `.md` | Project overview, setup instructions, usage examples, and maintenance notes. |
| `khinsider` | `.py` | Main downloader script. Downloads albums by ID or URL and supports CLI options such as output, format, search, image toggles, and list-only mode. |
| `requirements` | `.txt` | Python dependency list used to install the packages required by the downloader. |
| `.gitignore` | file | Recommended ignore file for excluding `downloads/`, Python caches, virtual environments, and other local-only files. |
| `downloads` | directory | Local output folder created/used by the downloader. This should not be committed to Git. |

### Installation

Clone or download this repository, then install the Python requirements:

```bash
python -m pip install -r requirements.txt
```

The project requires Python 3.9 or newer.

### Usage

The basic command format is:

```bash
python khinsider.py <album-id-or-url> [options]
```

For example:

```bash
python khinsider.py minecraft
```

or:

```bash
python khinsider.py https://downloads.khinsider.com/game-soundtracks/album/minecraft
```

By default, the album is downloaded under:

```text
downloads/<album-name-or-output-name>/
```

### Command Line Options

| Option | Description |
| --------------- | --------------- |
| `-o`, `--output DIR` | Sets the album output subdirectory under `downloads/`. |
| `-f`, `--format LIST` | Sets preferred audio formats in priority order, such as `flac,mp3`. |
| `-s`, `--search` | Searches for albums/songs instead of downloading immediately. |
| `-i`, `--images` | Enables album image downloads. Images are enabled by default. |
| `--no-images` | Skips album image downloads. |
| `-v`, `--verbose` | Enables progress output. Verbose output is enabled by default. |
| `--no-verbose` | Reduces progress/status output. |
| `--force` | Re-downloads files even if they already exist. |
| `--list-only` | Shows what would be downloaded without writing files. |
| `--timeout SECONDS` | Sets the HTTP timeout value. |
| `--delay SECONDS` | Adds a delay between sequential downloads. |
| `--version` | Prints the script version and exits. |
| `-h`, `--help` | Shows help information. |

### Download Directory Behavior

This version intentionally keeps all album output inside the repository-local `downloads/` folder.

For example:

```bash
python khinsider.py minecraft -o "Minecraft OST"
```

will save files under:

```text
downloads/Minecraft OST/
```

The `-o` or `--output` value is treated as a subdirectory name under `downloads/`, not as a free-form path outside the project. This helps keep downloaded albums predictable and makes the `.gitignore` easier to manage.

### Examples

Download an album by ID:

```bash
python khinsider.py plants-vs.-zombies
```

Download an album by full URL:

```bash
python khinsider.py https://downloads.khinsider.com/game-soundtracks/album/plants-vs.-zombies
```

Prefer FLAC, but fall back to MP3 if FLAC is unavailable:

```bash
python khinsider.py minecraft -f flac,mp3
```

Choose a custom output folder under `downloads/`:

```bash
python khinsider.py minecraft -o "Minecraft OST"
```

Search instead of downloading:

```bash
python khinsider.py -s persona
```

Preview files without downloading:

```bash
python khinsider.py minecraft --list-only
```

Skip album images:

```bash
python khinsider.py minecraft --no-images
```

Force a re-download of existing files:

```bash
python khinsider.py minecraft --force
```

### Attribution

This project is a personal maintenance fork based on the original `khinsider.py` project by `obskyr`. The goal of this repository is personal use and local maintenance, not upstream replacement.

The original project concept, public KHInsider downloader workflow, and earlier interface ideas belong to the original project. This fork updates the downloader behavior, CLI ergonomics, code organization, and local output handling for a personal repository workflow.
