import json
import time
from pathlib import Path

from backend.app.config import Settings
from backend.app.db import Database
from backend.app.notifications import NotificationWorker
from backend.app.replay import scenario_frames
from backend.app.service import Guardian


def offline_guardian():
    db = Database('sqlite:///:memory:')
    db.create()
    # Trusted built-in defaults only: never inherit future live .env credentials
    # or delivery switches when measuring synthetic fixtures.
    g = Guardian(db, Settings.model_construct(worker_enabled=False))
    g.seed()
    return g


def main():
    cases=[('fall','daniel'),('stumble','daniel'),('stumble','margaret'),('normal','daniel'),
           ('restricted_standing','margaret'),('restricted_standing','daniel'),('assisted_standing','margaret'),
           ('sway','daniel'),('bed_exit','margaret'),('low_visibility','daniel')]
    results=[]
    for scenario,person in cases:
        g=offline_guardian();durations=[];first=None
        frames=list(scenario_frames(scenario,person,duration=9))
        for f in frames:
            begin=time.perf_counter();r=g.ingest(f);durations.append((time.perf_counter()-begin)*1000)
            if r.event_id and first is None:first=f.timestamp-frames[0].timestamp
        NotificationWorker(g).tick()
        evs=g.events()
        results.append({'scenario':scenario,'profile':person,'source':'synthetic landmarks','observed_seconds':9,
                        'event_count':len(evs),'rule_hits':sorted({h for e in evs for h in e.rule_hits}),
                        'first_event_at_s':round(first,3) if first is not None else None,
                        'fall_onset_to_queued_alert_s':round(first-2,3) if scenario=='fall' and first is not None else None,
                        'ingest_p95_ms':round(sorted(durations)[int(.95*len(durations))],2),
                        'notification_states':[n.state for e in evs for n in e.notifications]})
    report={'scope':'Synthetic landmarks through the actual detector, persistence and dry-run notification worker. Not recorded video, physical capture latency, real provider delivery, or clinical validation.',
            'results':results,'normal_observation_seconds':9,'normal_false_alerts':next(r['event_count'] for r in results if r['scenario']=='normal'),
            'limitations':['No credentials supplied','No staged team recordings supplied','No reviewed personal baseline fitted','No live camera accuracy measurement']}
    Path('docs').mkdir(exist_ok=True)
    Path('docs/replay-results.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))


if __name__=='__main__':main()
