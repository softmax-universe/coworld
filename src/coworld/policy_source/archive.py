import io
import stat
import zipfile
from pathlib import Path, PurePosixPath

from coworld.policy_source.schemas import SOURCE_MAX_BYTES, SOURCE_MAX_ENTRIES

_EXCLUDED = {
    ".git",
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


def source_archive_issue(data: bytes, entry_paths: list[str] | None = None) -> str | None:
    if not data or len(data) > SOURCE_MAX_BYTES or not zipfile.is_zipfile(io.BytesIO(data)):
        return "Source must be a ZIP archive of at most 100 MiB"
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
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


def validate_source_archive(data: bytes, entry_paths: list[str] | None = None) -> None:
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
