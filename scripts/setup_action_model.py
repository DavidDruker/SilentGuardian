"""Fetch the pinned official checkpoint; verify before atomically installing it."""
import argparse
import hashlib
from pathlib import Path
import urllib.request

from backend.app.action_model import CHECKPOINT_SHA256, CHECKPOINT_URL


def fetch(path):
    path = Path(path)
    if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == CHECKPOINT_SHA256:
        print(f'Checkpoint already verified: {path}')
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(CHECKPOINT_URL, timeout=60) as response:
        data = response.read(20_000_001)
    if len(data) > 20_000_000 or hashlib.sha256(data).hexdigest() != CHECKPOINT_SHA256:
        raise ValueError('Download does not match the pinned official checkpoint')
    temporary = path.with_suffix('.download')
    temporary.write_bytes(data)
    temporary.replace(path)
    print(f'Installed verified ST-GCN++ checkpoint: {path}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--path', default='models/stgcnpp-ntu60-2d.pth')
    fetch(parser.parse_args().path)
