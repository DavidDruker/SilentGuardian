"""Export an observed capture session as a labelled evaluation clip, without raw video."""
import argparse
import json
from datetime import timezone
from pathlib import Path

from sqlalchemy import select

from backend.app.config import settings
from backend.app.db import Database, Window


def main():
    p = argparse.ArgumentParser()
    p.add_argument('stream_id', help='person_id:browser-camera:session_id from the downloaded skeleton recording')
    p.add_argument('output_json')
    p.add_argument('--label', choices=['normal', 'abnormal'], required=True)
    p.add_argument('--person', required=True, help='Consenting teammate alias, not a real name')
    p.add_argument('--angle', required=True)
    p.add_argument('--reviewed', action='store_true')
    p.add_argument('--append', action='store_true')
    args = p.parse_args()
    if ':browser-camera:' not in args.stream_id or 'synthetic' in args.stream_id:
        raise SystemExit('Export an actual camera/local-video session; generated fixtures cannot be staged evidence')
    db = Database(settings.database_url)
    with db.sessions() as s:
        rows = list(s.scalars(select(Window).where(Window.stream_id == args.stream_id).order_by(Window.ts)))
        windows = [{'timestamp': w.ts.replace(tzinfo=timezone.utc).timestamp(), 'features': w.features}
                   for w in rows if w.quality >= .8 and w.features.get('posture')]
    if len(windows) < 3:
        raise SystemExit('No usable observed session found; check its full stream id and retained time range')
    clip = {'session': args.stream_id, 'person': args.person, 'angle': args.angle, 'label': args.label, 'windows': windows}
    path = Path(args.output_json)
    body = {'source':'staged', 'reviewed':args.reviewed, 'clips':[]}
    if path.exists():
        if not args.append:
            raise SystemExit('Output exists; use a new file or explicitly --append')
        body = json.loads(path.read_text(encoding='utf-8'))
        if body.get('source') != 'staged' or any(c['session'] == args.stream_id for c in body['clips']):
            raise SystemExit('Dataset source mismatch or duplicate session')
        body['reviewed'] = body.get('reviewed', False) and args.reviewed
    body['clips'].append(clip)
    path.write_text(json.dumps(body, indent=2), encoding='utf-8')
    print(f'Exported {len(windows)} quality-gated windows. Uncertain intervals are excluded from credited exposure.')


if __name__ == '__main__':
    main()
