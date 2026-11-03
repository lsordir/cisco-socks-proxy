"""Small atomic file protocol shared by the controller and two VPN workers."""
import json
import os
from pathlib import Path


def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}


def write_json(path, value):
    path = Path(path)
    tmp = path.with_name(path.name + f'.{os.getpid()}.tmp')
    tmp.write_text(json.dumps(value))
    os.replace(tmp, path)
