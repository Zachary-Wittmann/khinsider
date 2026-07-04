#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from dataclasses import dataclass, field
from functools import update_wrapper
from itertools import chain
from pathlib import Path
from typing import Any, Callable, Generic, Iterable, Optional, Sequence, TypeVar
from urllib.parse import unquote, urljoin, urlsplit

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError as error:
    missing_module = "beautifulsoup4" if error.name == "bs4" else error.name
    print(f"Missing dependency: {missing_module}", file=sys.stderr)
    print("Install dependencies with:", file=sys.stderr)
    print("    python -m pip install requests beautifulsoup4", file=sys.stderr)
    sys.exit(1)


# Constants

BASE_URL = "https://downloads.khinsider.com/"
DEFAULT_DOWNLOAD_DIR = "downloads"
__version__ = "1.2.0"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
}

# Current album URLs use /game-soundtracks/album/<album-id>.
ALBUM_PATH_RE = re.compile(r"^/game-soundtracks/album/([^/?#]+)")

# Current direct files are normally hosted under /soundtracks/... or /ost/...
AUDIO_EXTENSIONS = {"mp3", "flac", "ogg", "m4a", "wav", "aac", "opus"}
IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "gif", "webp", "bmp"}

FILENAME_INVALID_RE = re.compile(r'[<>:"/\\|?*]')

# Fixes for old malformed KHInsider HTML that can still appear in places.
REMOVE_RE = re.compile(rb"^</td>\s*$", re.MULTILINE)
BAD_AMPERSAND_RE = re.compile(rb"&#([^0-9x]|x[^0-9A-Fa-f])")

DEFAULT_TIMEOUT = 30.0
DEFAULT_TRIES = 3
DOWNLOAD_CHUNK_SIZE = 1024 * 128

SESSION = requests.Session()
SESSION.headers.update(DEFAULT_HEADERS)

T = TypeVar("T")


class LazyProperty(Generic[T]):
    """Simple cached-property descriptor.

    Notes:
        This keeps the original script's lazy-loading behavior without placing
        helper functions before the class definitions. The descriptor stores the
        computed value on the instance the first time it is accessed.
    """

    def __init__(self, function: Callable[[Any], T]) -> None:
        self.function = function
        self.attribute_name = f"_lazy_{function.__name__}"
        update_wrapper(self, function)

    def __get__(self, instance: Any, owner: Optional[type] = None) -> T:
        if instance is None:
            return self  # type: ignore[return-value]

        if not hasattr(instance, self.attribute_name):
            setattr(instance, self.attribute_name, self.function(instance))

        return getattr(instance, self.attribute_name)


class KhinsiderError(Exception):
    """Base error for this module."""


class SearchError(KhinsiderError):
    """Raised when search cannot be completed."""


class DownloadError(KhinsiderError):
    """Raised when an HTTP request or file download fails."""


class NonexistentSongError(KhinsiderError):
    """Raised when a song page or song file cannot be found."""


class SoundtrackError(KhinsiderError):
    """Base error for soundtrack-specific failures."""

    def __init__(self, soundtrack: "Soundtrack") -> None:
        self.soundtrack = soundtrack


class NonexistentSoundtrackError(SoundtrackError, ValueError):
    """Raised when a soundtrack does not exist."""

    def __str__(self) -> str:
        ost = f'"{self.soundtrack.id}" ' if len(self.soundtrack.id) <= 80 else ""
        return f"The soundtrack {ost}does not exist."


class NonexistentFormatsError(SoundtrackError, ValueError):
    """Raised when requested formats are unavailable."""

    def __init__(
        self, soundtrack: "Soundtrack", requested_formats: Sequence[str]
    ) -> None:
        super().__init__(soundtrack)
        self.requested_formats = requested_formats

    @property
    def requestedFormats(self) -> Sequence[str]:  # legacy API alias
        """Legacy camelCase alias for requested_formats."""
        return self.requested_formats

    def __str__(self) -> str:
        ost = f'"{self.soundtrack.id}" ' if len(self.soundtrack.id) <= 80 else ""
        formats = ", ".join(f'"{extension}"' for extension in self.requested_formats)
        return f"The soundtrack {ost}is not available in the requested formats ({formats})."


@dataclass
class File:
    """A downloadable file belonging to a KHInsider soundtrack.

    Attributes:
        url (str): Full URL of the file.
        referer (Optional[str]): Optional Referer header for direct downloads.
        filename (str): Filename derived from the URL path.
    """

    url: str
    referer: Optional[str] = None
    filename: str = field(init=False)

    def __post_init__(self) -> None:
        filename = unquote(urlsplit(str(self.url)).path.rsplit(str("/"), 1)[-1])
        self.filename = filename or "download.bin"

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}: {self.url}>"

    def download(self, path: str) -> None:
        """Download the file to a local path.

        Args:
            path (str): Local filesystem path where the file should be saved.

        Raises:
            DownloadError: If the server returns HTML instead of a file.
            OSError: If the file cannot be written.
        """
        response = request_get(
            self.url,
            stream=True,
            referer=self.referer or BASE_URL,
        )

        content_type = response.headers.get("Content-Type", "").lower()
        if "text/html" in content_type:
            raise DownloadError(
                f"The server returned HTML instead of a file for {self.url}. "
                "The direct link may have expired or the parser needs another update."
            )

        with open(path, "wb") as output_file:
            for chunk in response.iter_content(chunk_size=DOWNLOAD_CHUNK_SIZE):
                if chunk:
                    output_file.write(chunk)


@dataclass
class Song:
    """A song page on KHInsider.

    Attributes:
        url (str): Full URL of the song page.
        name (str): Song name parsed from the page.
        files (list[File]): Available downloadable files for the song.
    """

    url: str

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}: {self.url}>"

    @LazyProperty
    def soup(self) -> BeautifulSoup:
        """BeautifulSoup representation of the song page."""
        response = request_get(self.url, referer=BASE_URL)
        if urlsplit(response.url).path.rstrip("/").endswith("/404"):
            raise NonexistentSongError("Nonexistent song page (404).")
        return to_soup(response)

    @property
    def _soup(self) -> BeautifulSoup:
        """Legacy alias for soup."""
        return self.soup

    @LazyProperty
    def name(self) -> str:
        """Song name parsed from the current or older page layout."""
        label = self.soup.find(string=re.compile(r"^\s*Song name:\s*", re.I))
        if label:
            parent_text = (
                label.parent.get_text(" ", strip=True) if label.parent else str(label)
            )
            return re.sub(r"^Song name:\s*", "", parent_text, flags=re.I).strip()

        heading = self.soup.find(["h1", "h2"])
        if heading:
            return heading.get_text(" ", strip=True)

        return Path(urlsplit(self.url).path).name

    @LazyProperty
    def files(self) -> list[File]:
        """Direct downloadable audio files exposed by the song page."""
        urls: list[str] = []
        tags = []
        tags.extend(self.soup.find_all(["a", "source"], href=True))
        tags.extend(self.soup.find_all(["audio", "source"], src=True))

        for tag in tags:
            raw_url = tag.get("href") or tag.get("src")
            if not raw_url:
                continue

            url = urljoin(self.url, raw_url)
            if is_direct_audio_url(url):
                urls.append(url)

        urls = dedupe_preserve_order(urls)
        if not urls:
            raise NonexistentSongError(
                f"No downloadable audio links found on {self.url}"
            )

        return [File(url, referer=self.url) for url in urls]


@dataclass
class Soundtrack:
    """A KHInsider soundtrack initialized with an album ID or album URL.

    Attributes:
        id (str): Unique album ID used at the end of the KHInsider album URL.
        url (str): Full KHInsider album URL.
        name (str): Textual title of the soundtrack.
        available_formats (list[str]): Available audio formats.
        songs (list[Song]): Songs in the soundtrack.
        images (list[File]): Cover/artwork files in the soundtrack.
    """

    soundtrack_id: str
    id: str = field(init=False)
    url: str = field(init=False)

    def __post_init__(self) -> None:
        self.id = soundtrack_id_from_url_or_id(self.soundtrack_id)
        self.url = urljoin(BASE_URL, "game-soundtracks/album/" + self.id)

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}: {self.id}>"

    def is_loaded(self, property_name: str) -> bool:
        """Return whether a lazy property has already been loaded.

        Args:
            property_name (str): Name of the lazy property to check.

        Returns:
            bool: True if the cached property value exists.
        """
        return hasattr(self, f"_lazy_{property_name}")

    def _isLoaded(self, propertyName: str) -> bool:  # legacy API alias
        """Legacy camelCase alias for is_loaded()."""
        return self.is_loaded(propertyName)

    @LazyProperty
    def content_soup(self) -> BeautifulSoup:
        """BeautifulSoup representation of the album content area."""
        soup = get_soup(self.url)
        content_soup = soup.find(id="pageContent") or soup
        page_text = content_soup.get_text(" ", strip=True)

        if re.search(r"\bNo such album\b", page_text, re.I):
            raise NonexistentSoundtrackError(self)
        if not content_soup.find(["h1", "h2"]):
            raise NonexistentSoundtrackError(self)

        return content_soup

    @property
    def _contentSoup(self) -> BeautifulSoup:  # legacy API alias
        """Legacy camelCase alias for content_soup."""
        return self.content_soup

    @LazyProperty
    def name(self) -> str:
        """Album title parsed from the album page."""
        heading = self.content_soup.find("h2") or self.content_soup.find("h1")
        return heading.get_text(" ", strip=True) if heading else self.id

    @LazyProperty
    def song_table(self) -> Any:
        """HTML table containing the album track list."""
        table = self.content_soup.find("table", id="songlist")
        if table:
            return table

        # Fallback for layout changes: choose the first table whose text and
        # links look like the track list.
        for candidate in self.content_soup.find_all("table"):
            text = candidate.get_text(" ", strip=True).lower()
            links = [
                urljoin(self.url, anchor.get("href", ""))
                for anchor in candidate.find_all("a", href=True)
            ]
            if "song name" in text and any(
                is_album_track_url(link, self.id) for link in links
            ):
                return candidate

        raise NonexistentSoundtrackError(self)

    @property
    def _songTable(self) -> Any:  # legacy API alias
        """Legacy camelCase alias for song_table."""
        return self.song_table

    @LazyProperty
    def available_formats(self) -> list[str]:
        """Audio formats listed in the album table header."""
        header = self.song_table.find("tr")
        cells = header.find_all(["th", "td"]) if header else []

        formats: list[str] = []
        for cell in cells:
            value = cell.get_text(" ", strip=True).lower()
            if value in AUDIO_EXTENSIONS:
                formats.append(value)

        return dedupe_preserve_order(formats) or ["mp3"]

    @property
    def availableFormats(self) -> list[str]:  # legacy API alias
        """Legacy camelCase alias for available_formats."""
        return self.available_formats

    @LazyProperty
    def songs(self) -> list[Song]:
        """Songs parsed from the album table."""
        urls: list[str] = []

        for row in self.song_table.find_all("tr"):
            if row.find("th"):
                continue

            row_text = row.get_text(" ", strip=True).lower()
            if row_text.startswith("total:"):
                continue

            for anchor in row.find_all("a", href=True):
                url = urljoin(self.url, anchor["href"])
                if is_album_track_url(url, self.id):
                    urls.append(url)
                    break

        urls = dedupe_preserve_order(urls)
        if not urls:
            raise NonexistentSoundtrackError(self)

        return [Song(url) for url in urls]

    @LazyProperty
    def images(self) -> list[File]:
        """Cover/artwork image files linked from the album page."""
        image_urls: list[str] = []

        for anchor in self.content_soup.find_all("a", href=True):
            if not anchor.find("img"):
                continue

            url = urljoin(self.url, anchor["href"])
            if extension_from_url_or_name(url) in IMAGE_EXTENSIONS:
                image_urls.append(url)

        return [
            File(url, referer=self.url) for url in dedupe_preserve_order(image_urls)
        ]

    def download(
        self,
        path: str = "",
        make_dirs: bool = True,
        format_order: Optional[Sequence[str]] = None,
        verbose: bool = False,
        include_images: bool = True,
        force: bool = False,
        delay: float = 0.0,
    ) -> bool:
        """Download the soundtrack to a directory.

        Args:
            path (str): Subdirectory under downloads/. Defaults to the album ID.
            make_dirs (bool): Create missing directories when True.
            format_order (Optional[Sequence[str]]): Preferred extensions in order.
                Example: ["flac", "mp3"].
            verbose (bool): Print progress when True.
            include_images (bool): Download album artwork/images when True.
            force (bool): Re-download existing files when True.
            delay (float): Seconds to wait between sequential downloads.

        Returns:
            bool: True if every file was downloaded or already existed; False
            if one or more files failed.

        Raises:
            NonexistentFormatsError: If none of the requested formats exist.
        """
        download_path = resolve_download_path(path, self.id)

        if format_order:
            format_order = [extension.lstrip(".").lower() for extension in format_order]
            if not set(self.available_formats) & set(format_order):
                raise NonexistentFormatsError(self, format_order)

        if verbose and not self.is_loaded("songs"):
            print("Getting song list...")

        files: list[Optional[File]] = []
        for song in self.songs:
            try:
                files.append(get_appropriate_file(song, format_order))
            except NonexistentSongError as error:
                if verbose:
                    print(
                        f"Could not find a downloadable file for {song.url}: {error}",
                        file=sys.stderr,
                    )
                files.append(None)

        if include_images:
            files.extend(self.images)

        if make_dirs and not os.path.isdir(download_path):
            os.makedirs(download_path, exist_ok=True)

        success = True
        total_files = len(files)
        for file_number, file in enumerate(files, 1):
            if not friendly_download_file(
                file,
                download_path,
                file_number,
                total_files,
                verbose,
                force=force,
            ):
                success = False

            if delay > 0 and file_number < total_files:
                time.sleep(delay)

        return success


# Request and parsing helpers


def request_get(
    url: str,
    stream: bool = False,
    referer: Optional[str] = None,
    tries: int = DEFAULT_TRIES,
    timeout: Optional[float] = None,
) -> requests.Response:
    """GET a URL with retry behavior.

    Args:
        url (str): URL to request.
        stream (bool): Stream the response body when True.
        referer (Optional[str]): Optional Referer header to send.
        tries (int): Number of attempts before raising DownloadError.
        timeout (int): Per-request timeout in seconds.

    Returns:
        requests.Response: Completed response object.

    Raises:
        DownloadError: If the request fails after all retry attempts.
    """
    headers = {"Referer": referer} if referer else None
    request_timeout = DEFAULT_TIMEOUT if timeout is None else timeout
    last_error: Optional[BaseException] = None

    for try_number in range(1, tries + 1):
        try:
            response = SESSION.get(
                url,
                stream=stream,
                timeout=request_timeout,
                headers=headers,
            )
            response.raise_for_status()
            return response
        except (
            requests.ConnectionError,
            requests.Timeout,
            requests.HTTPError,
        ) as error:
            last_error = error
            if try_number < tries:
                time.sleep(1.5 * try_number)

    raise DownloadError(f"Unable to retrieve {url}: {last_error}")


def to_soup(response: requests.Response) -> BeautifulSoup:
    """Convert a requests response to a BeautifulSoup document.

    Args:
        response (requests.Response): Response whose content should be parsed.

    Returns:
        BeautifulSoup: Parsed HTML content.
    """
    content = response.content
    content = REMOVE_RE.sub(b"", content)
    content = BAD_AMPERSAND_RE.sub(b"&amp;#\1", content)
    return BeautifulSoup(content, "html.parser")


def get_soup(url: str, referer: Optional[str] = None) -> BeautifulSoup:
    """GET a URL and parse it as HTML.

    Args:
        url (str): Page URL to request.
        referer (Optional[str]): Optional Referer header to send.

    Returns:
        BeautifulSoup: Parsed HTML content.
    """
    return to_soup(request_get(url, referer=referer))


# Utility helpers


def to_valid_filename(value: str) -> str:
    """Convert text into a safe filename.

    Args:
        value (str): Raw filename text.

    Returns:
        str: Filename with invalid Windows/WSL characters replaced.
    """
    value = value.rstrip(" .")

    if value.upper() in {
        "",
        ".",
        "..",
        "~",
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "COM1",
        "COM2",
        "COM3",
        "COM4",
        "COM5",
        "COM6",
        "COM7",
        "COM8",
        "COM9",
        "LPT1",
        "LPT2",
        "LPT3",
        "LPT4",
        "LPT5",
        "LPT6",
        "LPT7",
        "LPT8",
        "LPT9",
    }:
        return value + "_"

    return FILENAME_INVALID_RE.sub("-", value)


def unicode_print(*args: Any, **kwargs: Any) -> None:
    """Print text using a named helper kept from the original script.

    Args:
        *args (Any): Values to print.
        **kwargs (Any): Keyword arguments forwarded to print().
    """
    print(*args, **kwargs)


def extension_from_url_or_name(value: str) -> str:
    """Return a lowercase file extension from a URL or filename.

    Args:
        value (str): URL or filename.

    Returns:
        str: Extension without the leading dot, or an empty string.
    """
    path = urlsplit(value).path
    basename = unquote(path.rsplit("/", 1)[-1])
    if "." not in basename:
        return ""
    return basename.rsplit(".", 1)[-1].lower()


def soundtrack_id_from_url_or_id(value: str) -> str:
    """Normalize an album URL or album ID into a KHInsider album ID.

    Args:
        value (str): Album ID or full KHInsider album URL.

    Returns:
        str: Normalized album ID.

    Raises:
        ValueError: If a full URL is passed but it is not a KHInsider album URL.
    """
    parsed = urlsplit(value)
    if parsed.scheme and parsed.netloc:
        match = ALBUM_PATH_RE.match(parsed.path)
        if not match:
            raise ValueError(f"Not a KHInsider album URL: {value}")
        return unquote(match.group(1)).strip("/")

    return value.strip().strip("/")


def resolve_download_path(path: Optional[str], default_folder: str) -> str:
    """Resolve an output path that always stays under downloads/.

    Args:
        path (Optional[str]): User-requested subdirectory. Absolute paths are
            treated as relative folder names so they cannot escape downloads/.
        default_folder (str): Folder name to use when path is empty.

    Returns:
        str: Absolute path under the repository's downloads/ directory.
    """
    requested_path = Path(path or default_folder)

    # If someone passes an absolute path, keep only the useful path parts and
    # still place them under downloads/.
    if requested_path.is_absolute():
        requested_parts = requested_path.parts[1:]
    else:
        requested_parts = requested_path.parts

    safe_parts = [
        to_valid_filename(part)
        for part in requested_parts
        if part not in {"", ".", ".."}
    ]

    if not safe_parts:
        safe_parts = [to_valid_filename(default_folder)]

    return str((Path.cwd() / DEFAULT_DOWNLOAD_DIR / Path(*safe_parts)).resolve())


def is_album_track_url(url: str, album_id: str) -> bool:
    """Return whether a URL points to a track page inside an album.

    Args:
        url (str): URL to inspect.
        album_id (str): Expected album ID.

    Returns:
        bool: True when the URL appears to be an album track page.
    """
    path = urlsplit(url).path
    if f"/game-soundtracks/album/{album_id}/" not in path:
        return False
    if path.endswith("/change_log") or "/change_log" in path:
        return False
    return extension_from_url_or_name(url) in AUDIO_EXTENSIONS


def is_direct_audio_url(url: str) -> bool:
    """Return whether a URL points directly to an audio file.

    Args:
        url (str): URL to inspect.

    Returns:
        bool: True when the URL looks like a direct downloadable audio file.
    """
    parsed = urlsplit(url)
    extension = extension_from_url_or_name(url)
    return bool(
        parsed.scheme
        and parsed.netloc
        and extension in AUDIO_EXTENSIONS
        and re.search(r"/(soundtracks|ost)/", parsed.path)
    )


def dedupe_preserve_order(values: Iterable[str]) -> list[str]:
    """Remove duplicates without changing order.

    Args:
        values (Iterable[str]): Values to deduplicate.

    Returns:
        list[str]: Unique values in original order.
    """
    seen: set[str] = set()
    deduped_values: list[str] = []

    for value in values:
        if value in seen:
            continue
        seen.add(value)
        deduped_values.append(value)

    return deduped_values


# Download helpers


def get_appropriate_file(song: Song, format_order: Optional[Sequence[str]]) -> File:
    """Choose the best matching file for a song.

    Args:
        song (Song): Song whose files should be inspected.
        format_order (Optional[Sequence[str]]): Preferred extensions in order.

    Returns:
        File: Selected downloadable file.
    """
    if format_order is None:
        return song.files[0]

    for extension in format_order:
        normalized_extension = extension.lstrip(".").lower()
        for file in song.files:
            if extension_from_url_or_name(file.filename) == normalized_extension:
                return file

    return song.files[0]


def friendly_download_file(
    file: Optional[File],
    path: str,
    index: int,
    total: int,
    verbose: bool = False,
    force: bool = False,
) -> bool:
    """Download a file with user-friendly progress/error output.

    Args:
        file (Optional[File]): File object to download; None means unavailable.
        path (str): Directory to download into.
        index (int): Current file number.
        total (int): Total number of files.
        verbose (bool): Print progress when True.
        force (bool): Re-download an existing file when True.

    Returns:
        bool: True if the file was downloaded or already exists; False otherwise.
    """
    number_text = f"{str(index).zfill(len(str(total)))}/{total}"

    if file is None:
        if verbose:
            print(f"Song {number_text} is unavailable. Skipping over.", file=sys.stderr)
        return False

    filename = file.filename.encode("utf-8", "replace").decode("utf-8")
    filename = to_valid_filename(filename)
    target_path = os.path.join(path, filename)

    if os.path.exists(target_path) and os.path.getsize(target_path) > 0 and not force:
        if verbose:
            unicode_print(f"Skipping over {number_text}: {filename}. Already exists.")
        return True

    if verbose:
        if os.path.exists(target_path) and force:
            unicode_print(f"Re-downloading {number_text}: {filename}...")
        else:
            unicode_print(f"Downloading {number_text}: {filename}...")

    try:
        file.download(target_path)
    except (requests.RequestException, DownloadError, OSError) as error:
        if os.path.exists(target_path) and os.path.getsize(target_path) == 0:
            try:
                os.remove(target_path)
            except OSError:
                pass

        if verbose:
            unicode_print(f"Couldn't download {filename}: {error}", file=sys.stderr)
        return False

    return True


# Public API functions


def download_soundtrack(
    soundtrack_id: str,
    path: Optional[str] = "",
    make_dirs: bool = True,
    format_order: Optional[Sequence[str]] = None,
    verbose: bool = False,
    include_images: bool = True,
    force: bool = False,
    delay: float = 0.0,
) -> bool:
    """Download a soundtrack by album ID or full album URL.

    Args:
        soundtrack_id (str): KHInsider album ID or full album URL.
        path (Optional[str]): Subdirectory under downloads/. If None, use the album title.
        make_dirs (bool): Create missing directories when True.
        format_order (Optional[Sequence[str]]): Preferred extensions in order.
        verbose (bool): Print progress when True.
        include_images (bool): Download album artwork/images when True.
        force (bool): Re-download existing files when True.
        delay (float): Seconds to wait between sequential downloads.

    Returns:
        bool: True if all files downloaded successfully; False otherwise.
    """
    soundtrack = Soundtrack(soundtrack_id)
    _ = soundtrack.name  # Force consistent early album validation.
    output_path = to_valid_filename(soundtrack.name) if path is None else path

    if verbose:
        unicode_print(
            f'Downloading under "{DEFAULT_DOWNLOAD_DIR}/{output_path or soundtrack.id}".'
        )

    return soundtrack.download(
        output_path,
        make_dirs,
        format_order,
        verbose,
        include_images,
        force=force,
        delay=delay,
    )


def search(term: str) -> list[list[Soundtrack]]:
    """Return Soundtrack objects for a search term.

    Args:
        term (str): Search term to send to KHInsider.

    Returns:
        list[list[Soundtrack]]: First list is album-title results; second list
        is song-name results when the site separates them.

    Raises:
        SearchError: If no usable search results can be parsed.
    """
    request = requests.Request(
        "GET", urljoin(BASE_URL, "search"), params={"search": term}
    )
    prepared = request.prepare()
    response = request_get(
        prepared.url or urljoin(BASE_URL, "search"), referer=BASE_URL
    )

    path = urlsplit(response.url).path
    match = ALBUM_PATH_RE.match(path)
    if match:
        return [[Soundtrack(match.group(1))], []]

    soup = to_soup(response)
    content = soup.find(id="pageContent") or soup

    tables = content.find_all("table", class_="albumList")
    if tables:
        soundtracks = [soundtracks_in_search_table(table) for table in tables]
        if len(soundtracks) == 1:
            paragraph = content.find("p")
            paragraph_text = (
                paragraph.get_text(" ", strip=True).lower() if paragraph else ""
            )
            if "song" in paragraph_text:
                soundtracks.insert(0, [])
            else:
                soundtracks.append([])
        return soundtracks

    fallback_results: list[Soundtrack] = []
    seen: set[str] = set()

    for anchor in content.find_all("a", href=True):
        href = urljoin(BASE_URL, anchor["href"])
        match = ALBUM_PATH_RE.match(urlsplit(href).path)
        if not match:
            continue

        found_soundtrack_id = match.group(1)
        if found_soundtrack_id in seen:
            continue

        seen.add(found_soundtrack_id)
        soundtrack = Soundtrack(found_soundtrack_id)
        text = anchor.get_text(" ", strip=True)
        if text:
            soundtrack._lazy_name = text
        fallback_results.append(soundtrack)

    if fallback_results:
        return [fallback_results, []]

    paragraph = content.find("p")
    message = (
        paragraph.get_text(" ", strip=True) if paragraph else "No search results found."
    )
    raise SearchError(message)


def soundtracks_in_search_table(table: Any) -> list[Soundtrack]:
    """Parse a KHInsider search-results table.

    Args:
        table (Any): BeautifulSoup table element.

    Returns:
        list[Soundtrack]: Parsed soundtrack results.
    """
    soundtracks: list[Soundtrack] = []

    for anchor in table.find_all("a", href=True):
        match = ALBUM_PATH_RE.match(urlsplit(urljoin(BASE_URL, anchor["href"])).path)
        if not match:
            continue

        soundtrack = Soundtrack(match.group(1))
        name = anchor.get_text(" ", strip=True)
        if name:
            soundtrack._lazy_name = name
        soundtracks.append(soundtrack)

    unique_soundtracks: list[Soundtrack] = []
    seen: set[str] = set()

    for soundtrack in soundtracks:
        if soundtrack.id in seen:
            continue
        unique_soundtracks.append(soundtrack)
        seen.add(soundtrack.id)

    return unique_soundtracks


def print_search_results(
    search_results: Sequence[Sequence[Soundtrack]], file: Any = sys.stdout
) -> None:
    """Print search results in the original script's format.

    Args:
        search_results (Sequence[Sequence[Soundtrack]]): Search results to print.
        file (Any): File-like output target.
    """
    flattened_results = list(chain.from_iterable(search_results))
    if not flattened_results:
        print("No soundtracks found.", file=file)
        return

    padding_length = max(len(soundtrack.id) for soundtrack in flattened_results)
    output = ""
    has_previous_list = False

    for heading, soundtracks in zip(
        ("Album title results:", "Song name results:"),
        search_results,
    ):
        if not soundtracks:
            continue

        if has_previous_list:
            output += "\n"

        output += heading + "\n"
        for soundtrack in soundtracks:
            dots = "." * (padding_length - len(soundtrack.id))
            output += f"{soundtrack.id} {dots}. {soundtrack.name}\n"

        has_previous_list = True

    unicode_print(output, end="", file=file)


# CLI


def parse_format_order(value: Optional[str]) -> Optional[list[str]]:
    """Parse the --format argument.

    Args:
        value (Optional[str]): Comma-separated format list, e.g. "flac,mp3".

    Returns:
        Optional[list[str]]: Normalized list of extensions, or None.
    """
    if not value:
        return None

    return [
        extension.strip().lstrip(".").lower()
        for extension in re.split(r",\s*", value)
        if extension.strip()
    ]


def positive_float(value: str) -> float:
    """Parse a positive floating-point CLI value.

    Args:
        value (str): Raw CLI value.

    Returns:
        float: Parsed value.

    Raises:
        argparse.ArgumentTypeError: If value is not greater than zero.
    """
    try:
        parsed_value = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"expected a number, got {value!r}") from error

    if parsed_value <= 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")

    return parsed_value


def non_negative_float(value: str) -> float:
    """Parse a non-negative floating-point CLI value.

    Args:
        value (str): Raw CLI value.

    Returns:
        float: Parsed value.

    Raises:
        argparse.ArgumentTypeError: If value is negative.
    """
    try:
        parsed_value = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"expected a number, got {value!r}") from error

    if parsed_value < 0:
        raise argparse.ArgumentTypeError("value must be zero or greater")

    return parsed_value


def selected_output_path(
    output_option: Optional[str],
    positional_output: Optional[str],
) -> Optional[str]:
    """Choose the CLI output value while preserving downloads/ as the root.

    Args:
        output_option (Optional[str]): Value supplied through -o/--output.
        positional_output (Optional[str]): Legacy positional output argument.

    Returns:
        Optional[str]: User-requested subdirectory, or None for album-name default.
    """
    return output_option if output_option is not None else positional_output


def build_argument_parser() -> argparse.ArgumentParser:
    """Build the command-line argument parser.

    Returns:
        argparse.ArgumentParser: Configured parser for the CLI.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Download entire soundtracks from KHInsider.\n\n"
            "Examples:\n"
            "%(prog)s jumping-flash-ps1-gamerip-1995\n"
            '%(prog)s -o "Jumping Flash OST" jumping-flash-ps1-gamerip-1995\n'
            '%(prog)s -f flac,mp3 "jumping-flash-ps1-gamerip-1995"\n'
            '%(prog)s --list-only "jumping-flash-ps1-gamerip-1995"\n'
            '%(prog)s --search "jumping flash"'
        ),
        epilog=("Output paths are always treated as subdirectories under downloads/. "),
        formatter_class=argparse.RawTextHelpFormatter,
    )

    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "soundtrack",
        help=(
            "The album ID from the end of the KHInsider URL, the full album URL, "
            "or a search term with --search."
        ),
    )
    parser.add_argument(
        "out_path",
        metavar="download directory",
        nargs="?",
        help=(
            "Legacy positional output subdirectory under downloads/. "
            "Prefer -o/--output for new usage."
        ),
    )
    parser.add_argument(
        "-o",
        "--output",
        dest="output_path",
        metavar="DIR",
        help=(
            "Output subdirectory under downloads/. Defaults to the sanitized "
            "album title."
        ),
    )
    parser.add_argument(
        "-f",
        "--format",
        default=None,
        metavar="LIST",
        help='Preferred file format or comma-separated fallback list, e.g. "flac,mp3".',
    )
    parser.add_argument(
        "-s",
        "--search",
        action="store_true",
        help="Search for soundtracks instead of downloading.",
    )
    parser.add_argument(
        "-i",
        "--images",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Download album images. Enabled by default; use --no-images to disable.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Show download progress. Enabled by default; use --no-verbose to disable.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-download files even when they already exist.",
    )
    parser.add_argument(
        "--list-only",
        action="store_true",
        help="Show what would be downloaded without writing any files.",
    )
    parser.add_argument(
        "--timeout",
        type=positive_float,
        default=DEFAULT_TIMEOUT,
        metavar="SECONDS",
        help=f"HTTP timeout in seconds. Default: {DEFAULT_TIMEOUT:g}.",
    )
    parser.add_argument(
        "--delay",
        type=non_negative_float,
        default=0.0,
        metavar="SECONDS",
        help="Seconds to wait between sequential downloads. Default: 0.",
    )

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the command-line interface.

    Args:
        argv (Optional[Sequence[str]]): Optional argument list for testing.
            Defaults to sys.argv when None.

    Returns:
        int: Process exit code.
    """
    global DEFAULT_TIMEOUT

    script_name = os.path.split(sys.argv[0])[-1]
    parser = build_argument_parser()
    arguments, trailing_arguments = parser.parse_known_args(argv)
    if any(argument.startswith("-") for argument in trailing_arguments):
        parser.error("unrecognized arguments: " + " ".join(trailing_arguments))

    # Unquoted extra words become a search term instead of being treated as invalid arguments.
    only_search = arguments.search or len(trailing_arguments) > 1

    if (
        arguments.output_path is not None
        and arguments.out_path is not None
        and not only_search
    ):
        parser.error(
            "Use either -o/--output or the positional output directory, not both."
        )

    DEFAULT_TIMEOUT = arguments.timeout

    soundtrack = arguments.soundtrack
    out_path = selected_output_path(arguments.output_path, arguments.out_path)
    search_term_parts = [soundtrack]
    if arguments.output_path is None and arguments.out_path is not None:
        search_term_parts.append(arguments.out_path)
    search_term_parts += trailing_arguments
    search_term = " ".join(search_term_parts).replace("-", " ").strip()

    format_order = parse_format_order(arguments.format)

    try:
        if only_search:
            return run_search_mode(search_term, script_name)

        if arguments.list_only:
            return run_list_only_mode(
                soundtrack=soundtrack,
                out_path=out_path,
                format_order=format_order,
                include_images=arguments.images,
            )

        return run_download_mode(
            soundtrack=soundtrack,
            out_path=out_path,
            search_term=search_term,
            format_order=format_order,
            include_images=arguments.images,
            verbose=arguments.verbose,
            force=arguments.force,
            delay=arguments.delay,
        )
    except (requests.ConnectionError, requests.Timeout, DownloadError) as error:
        print(
            f"Could not connect to KHInsider or its file host: {error}", file=sys.stderr
        )
        return 1
    except Exception:
        print(file=sys.stderr)
        print("An unexpected error occurred!", file=sys.stderr)
        print("Attach the following error message:", file=sys.stderr)
        print(file=sys.stderr)
        raise


def run_search_mode(search_term: str, script_name: str) -> int:
    """Run CLI search mode.

    Args:
        search_term (str): Search term to send to KHInsider.
        script_name (str): Name of the running script for help text.

    Returns:
        int: Process exit code.
    """
    try:
        search_results = search(search_term)
    except SearchError as error:
        print(f"Couldn't search. {error}", file=sys.stderr)
        return 1

    if search_results:
        print(
            f'Soundtracks found (to download, run "{script_name} soundtrack-name")!\n'
        )
        print_search_results(search_results)
    else:
        print("No soundtracks found.")

    return 0


def run_list_only_mode(
    soundtrack: str,
    out_path: Optional[str],
    format_order: Optional[Sequence[str]],
    include_images: bool,
) -> int:
    """Preview the files that would be downloaded.

    Args:
        soundtrack (str): Album ID or full album URL to inspect.
        out_path (Optional[str]): Requested output subdirectory under downloads/.
        format_order (Optional[Sequence[str]]): Preferred extensions in order.
        include_images (bool): Include album artwork/images when True.

    Returns:
        int: Process exit code.
    """
    try:
        soundtrack_object = Soundtrack(soundtrack)
        _ = soundtrack_object.name
        output_path = (
            to_valid_filename(soundtrack_object.name) if out_path is None else out_path
        )
        download_path = resolve_download_path(output_path, soundtrack_object.id)

        if format_order:
            normalized_formats = [
                extension.lstrip(".").lower() for extension in format_order
            ]
            if not set(soundtrack_object.available_formats) & set(normalized_formats):
                raise NonexistentFormatsError(soundtrack_object, normalized_formats)
        else:
            normalized_formats = None

        print(f"Album:   {soundtrack_object.name}")
        print(f"Output:  {download_path}")
        print(f"Formats: {', '.join(soundtrack_object.available_formats)}")
        print()

        files: list[tuple[str, Optional[File]]] = []
        for song in soundtrack_object.songs:
            try:
                files.append(
                    (song.name, get_appropriate_file(song, normalized_formats))
                )
            except NonexistentSongError:
                files.append((song.name, None))

        print(f"Tracks ({len(files)}):")
        for index, (song_name, file) in enumerate(files, 1):
            if file is None:
                print(f"  {index:>3}. {song_name} [unavailable]")
            else:
                print(f"  {index:>3}. {file.filename}")

        if include_images:
            print()
            print(f"Images ({len(soundtrack_object.images)}):")
            for index, file in enumerate(soundtrack_object.images, 1):
                print(f"  {index:>3}. {file.filename}")

    except NonexistentSoundtrackError:
        print(f'The soundtrack "{soundtrack}" does not seem to exist.', file=sys.stderr)
        return 1
    except NonexistentFormatsError as error:
        formats = ", ".join(error.soundtrack.available_formats)
        print(
            f"Requested format not available. The soundtrack is available as: {formats}",
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        print("Stopped list operation.", file=sys.stderr)
        return 1

    return 0


def run_download_mode(
    soundtrack: str,
    out_path: Optional[str],
    search_term: str,
    format_order: Optional[Sequence[str]],
    include_images: bool,
    verbose: bool,
    force: bool,
    delay: float,
) -> int:
    """Run CLI download mode.

    Args:
        soundtrack (str): Album ID or full album URL to download.
        out_path (Optional[str]): Output directory from the CLI.
        search_term (str): Search fallback term.
        format_order (Optional[Sequence[str]]): Preferred extensions in order.
        include_images (bool): Download album artwork/images when True.
        verbose (bool): Print progress when True.
        force (bool): Re-download existing files when True.
        delay (float): Seconds to wait between sequential downloads.

    Returns:
        int: Process exit code.
    """
    try:
        success = download_soundtrack(
            soundtrack,
            out_path,
            format_order=format_order,
            verbose=verbose,
            include_images=include_images,
            force=force,
            delay=delay,
        )
        if not success:
            print("\nNot all files could be downloaded.", file=sys.stderr)
            return 1
    except NonexistentSoundtrackError:
        return handle_nonexistent_soundtrack(soundtrack, search_term)
    except NonexistentFormatsError as error:
        formats = ", ".join(error.soundtrack.available_formats)
        print(
            f"Requested format not available. The soundtrack is available as: {formats}",
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        print("Stopped download.", file=sys.stderr)
        return 1

    return 0


def handle_nonexistent_soundtrack(soundtrack: str, search_term: str) -> int:
    """Print helpful fallback output when an album ID does not exist.

    Args:
        soundtrack (str): Album ID or URL that failed.
        search_term (str): Search fallback term.

    Returns:
        int: Process exit code.
    """
    try:
        search_results = search(search_term)
    except SearchError:
        search_results = None

    print(f'The soundtrack "{soundtrack}" does not seem to exist.', file=sys.stderr)

    if search_results:
        print("\nThese exist, though:", file=sys.stderr)
        print_search_results(search_results, file=sys.stderr)
    elif search_results is None:
        print(
            f'A search for "{search_term}" could not be performed either.',
            file=sys.stderr,
        )

    return 1


if __name__ == "__main__":
    sys.exit(main())
