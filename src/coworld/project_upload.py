import json
import os
import tempfile
from pathlib import Path

from pydantic import TypeAdapter


def upload_binding_path(manifest_path: Path) -> Path:
    directory = manifest_path.resolve().parent
    for candidate in (directory, *directory.parents):
        if (candidate / "coworld_manifest_template.json").is_file():
            return candidate / ".coworld-upload.json"
        if (candidate / ".git").exists() or candidate == Path.home():
            break
    return directory / f".{manifest_path.stem}-upload.json"


def load_upload_bindings(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    return TypeAdapter(dict[str, str]).validate_json(path.read_bytes())


def save_upload_binding(path: Path, *, server: str, game_id: str) -> None:
    bindings = load_upload_bindings(path)
    bindings[server.rstrip("/")] = game_id
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(json.dumps(bindings, indent=2, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
