import os
import shutil
import tempfile
import uuid
from pathlib import Path

import requests
from app.router.config_pb2 import GeoIPList, GeoSiteList
from google.protobuf.message import DecodeError

MAX_GEO_BYTES = 256 * 1024 * 1024


def _sync_directory(directory):
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _replace_link(path, target):
    candidate = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        candidate.symlink_to(target)
        os.replace(candidate, path)
        _sync_directory(path.parent)
    finally:
        candidate.unlink(missing_ok=True)


def _validate_geo(path, message_type):
    message = message_type()
    try:
        message.ParseFromString(path.read_bytes())
    except DecodeError as exc:
        raise ValueError(f"Invalid {path.name} protobuf") from exc
    if not message.entry or any(not entry.country_code for entry in message.entry):
        raise ValueError(f"Empty or invalid {path.name}")
    if message_type is GeoIPList:
        for entry in message.entry:
            for cidr in entry.cidr:
                if len(cidr.ip) not in (4, 16) or cidr.prefix > len(cidr.ip) * 8:
                    raise ValueError("Invalid GeoIP network")


def update_geo_pair(asset_directory, urls, validate_config, restart):
    assets = Path(asset_directory)
    generations = assets / ".geo"
    generations.mkdir(exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="generation-", dir=generations))
    try:
        for filename, url in urls.items():
            size = 0
            with requests.get(url, stream=True, timeout=(10, 30)) as response:
                response.raise_for_status()
                with (stage / filename).open("wb") as destination:
                    for chunk in response.iter_content(chunk_size=65536):
                        size += len(chunk)
                        if size > MAX_GEO_BYTES:
                            raise ValueError(f"{filename} exceeds the size limit")
                        destination.write(chunk)
                    destination.flush()
                    os.fsync(destination.fileno())
                content_length = getattr(response, "headers", {}).get("Content-Length")
                content_encoding = getattr(response, "headers", {}).get("Content-Encoding")
                if content_length and not content_encoding and size != int(content_length):
                    raise ValueError(f"Incomplete {filename} download")
        _validate_geo(stage / "geoip.dat", GeoIPList)
        _validate_geo(stage / "geosite.dat", GeoSiteList)
        validate_config(str(stage))
        _sync_directory(stage)
        current = generations / "current"
        if not current.is_symlink():
            previous = Path(tempfile.mkdtemp(prefix="generation-", dir=generations))
            for filename in urls:
                source = assets / filename
                if source.exists():
                    shutil.copy2(source, previous / filename)
                    with (previous / filename).open("rb") as stream:
                        os.fsync(stream.fileno())
            _sync_directory(previous)
            _replace_link(current, previous.name)
        previous_target = os.readlink(current)
        for filename in urls:
            destination = assets / filename
            target = f".geo/current/{filename}"
            if not destination.is_symlink() or os.readlink(destination) != target:
                _replace_link(destination, target)
        _replace_link(current, stage.name)
        try:
            restart()
        except Exception:
            _replace_link(current, previous_target)
            try:
                restart()
            except Exception:
                pass
            raise
    except Exception as exc:
        raise RuntimeError("Failed to update GeoDB pair") from exc
    finally:
        current = generations / "current"
        if not (current.is_symlink() and os.readlink(current) == stage.name):
            shutil.rmtree(stage)
