import ctypes
import errno
import hashlib
import io
import os
import shutil
import stat
import zipfile
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import Literal

from coworld.policy_source.schemas import SOURCE_MAX_BYTES, SOURCE_MAX_ENTRIES, SourceContent, SourceMarker

SOURCE_MARKER = ".cogent-source.json"

_EXCLUDED = {
    ".git",
    SOURCE_MARKER,
    ".softmax",
    ".aws",
    ".ssh",
    ".npmrc",
    ".pypirc",
    ".netrc",
    ".env",
    ".venv",
    "__pycache__",
    "node_modules",
    "target",
    "dist",
}


def source_archive_issue(data: bytes | Path, entry_paths: list[str] | None = None) -> str | None:
    size = len(data) if isinstance(data, bytes) else data.stat().st_size
    source = io.BytesIO(data) if isinstance(data, bytes) else data
    if not size or size > SOURCE_MAX_BYTES or not zipfile.is_zipfile(source):
        return "Source must be a ZIP archive of at most 100 MiB"
    with zipfile.ZipFile(source) as archive:
        entries = archive.infolist()
        if len(entries) > SOURCE_MAX_ENTRIES:
            return "Source archive contains more than 4096 entries"
        paths: set[str] = set()
        files: set[str] = set()
        expanded = 0
        for entry in entries:
            path = PurePosixPath(entry.filename)
            normalized = entry.filename.rstrip("/")
            if (
                not normalized
                or path.is_absolute()
                or any(part in {"", ".", ".."} for part in normalized.split("/"))
                or "\\" in normalized
                or "\x00" in normalized
                or normalized in paths
                or len(normalized) > 1024
                or entry.flag_bits & 1
                or entry.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
            ):
                return "Source archive contains an unsafe, encrypted, duplicate or unsupported entry"
            mode = entry.external_attr >> 16
            if stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR}:
                return "Source archive contains a link or special file"
            expanded += entry.file_size
            if expanded > SOURCE_MAX_BYTES:
                return "Expanded source exceeds 100 MiB"
            paths.add(normalized)
            if not entry.is_dir():
                files.add(normalized)
        if any(str(parent) in files for name in paths for parent in PurePosixPath(name).parents):
            return "Source archive contains a file used as a directory"
        if not files or any(path not in files for path in entry_paths or []):
            return "Source entry paths must identify files in the archive"
        if archive.testzip() is not None:
            return "Source archive failed its checksum"

    return None


def validate_source_archive(data: bytes | Path, entry_paths: list[str] | None = None) -> None:
    if issue := source_archive_issue(data, entry_paths):
        raise ValueError(issue)


def pack_source(source: Path) -> bytes:
    if source.is_symlink():
        raise ValueError("Source must not be a symlink")
    if source.is_file():
        data = source.read_bytes()
        validate_source_archive(data)
        return data
    if not source.is_dir():
        raise ValueError(f"Source directory does not exist: {source}")
    files: list[Path] = []
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if any(part in _EXCLUDED or part.startswith(".env.") for part in relative.parts):
            continue
        if path.is_symlink():
            raise ValueError(f"Source cannot contain symlinks: {relative}")
        if path.is_file():
            files.append(path)
    if len(files) > SOURCE_MAX_ENTRIES or sum(path.stat().st_size for path in files) > SOURCE_MAX_BYTES:
        raise ValueError("Source exceeds the archive size or file count limit")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            member = zipfile.ZipInfo(path.relative_to(source).as_posix(), date_time=(1980, 1, 1, 0, 0, 0))
            member.compress_type = zipfile.ZIP_DEFLATED
            member.external_attr = (stat.S_IFREG | (path.stat().st_mode & 0o777)) << 16
            archive.writestr(member, path.read_bytes())
    data = output.getvalue()
    validate_source_archive(data)
    return data


def existing_source_directory(destination: Path, identity: SourceMarker) -> bool:
    for path in (destination, *destination.parents):
        if path.is_symlink():
            raise ValueError(f"Source destination cannot traverse symlinks: {path}")
    if not destination.exists():
        return False
    marker = destination / SOURCE_MARKER
    if destination.is_dir() and marker.is_file() and not marker.is_symlink():
        if SourceMarker.model_validate_json(marker.read_text()) == identity:
            return True
    raise FileExistsError(f"Source destination contains another import or unrelated files: {destination}")


def materialize_source(
    source: Path, *, content: SourceContent, policy_version_id: str, destination: Path
) -> Literal["created", "already_open"]:
    identity = SourceMarker(source_id=policy_version_id, content_hash=content.content_hash)
    if existing_source_directory(destination, identity):
        return "already_open"
    if source.stat().st_size != content.size_bytes:
        raise ValueError("Source download has an unexpected size")
    with source.open("rb") as handle:
        if hashlib.file_digest(handle, "sha256").hexdigest() != content.content_hash:
            raise ValueError("Source download failed its SHA-256 check")
    is_archive = zipfile.is_zipfile(source)
    if content.kind == "source_archive" or is_archive:
        validate_source_archive(source)
        with zipfile.ZipFile(source) as archive:
            if any(SOURCE_MARKER in PurePosixPath(entry.filename).parts for entry in archive.infolist()):
                raise ValueError(f"Source archive contains reserved path {SOURCE_MARKER}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".coworld-pull-", dir=destination.parent) as temporary:
        staging = Path(temporary) / "source"
        staging.mkdir()
        if is_archive:
            with zipfile.ZipFile(source) as archive:
                for entry in archive.infolist():
                    target = staging / entry.filename
                    if entry.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with archive.open(entry) as input_file, target.open("xb") as output:
                            shutil.copyfileobj(input_file, output, length=1024 * 1024)
                        mode = entry.external_attr >> 16
                        target.chmod(mode & 0o777 if mode else 0o644)
                for entry in reversed(archive.infolist()):
                    if entry.is_dir():
                        mode = entry.external_attr >> 16
                        (staging / entry.filename).chmod(mode & 0o777 if mode else 0o755)
        else:
            shutil.copyfile(source, staging / "player-file")
            (staging / "player-file").chmod(0o644)
        (staging / SOURCE_MARKER).write_text(identity.model_dump_json(by_alias=True))
        if existing_source_directory(destination, identity):
            return "already_open"
        # Completed imports are nonempty, so POSIX rename preserves a racing working copy.
        # Archil supports this operation but rejects renameat2(RENAME_NOREPLACE).
        libc = ctypes.CDLL(None, use_errno=True)
        result = libc.rename(os.fsencode(staging), os.fsencode(destination))
        if result != 0:
            code = ctypes.get_errno()
            if code in {errno.EEXIST, errno.ENOTEMPTY, errno.ENOTDIR}:
                if existing_source_directory(destination, identity):
                    return "already_open"
                raise FileExistsError(f"Source destination was created during import: {destination}")
            raise OSError(code, os.strerror(code), str(destination))
    return "created"
