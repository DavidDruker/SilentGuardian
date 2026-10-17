"""Deterministic synthetic skeletons, NOT recordings or measured clinical data."""
import math
from datetime import datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

from .schemas import PoseFrame

SCENARIOS = ['fall', 'long_lie', 'stumble', 'normal', 'restricted_standing', 'assisted_standing', 'low_visibility', 'sway', 'bed_exit']


def skeleton(x=.5, y=.55, angle=0, wrist=0, ankle=0, visibility=.99):
    # Anatomically recognizable synthetic pose around its hips, 33 MediaPipe indices.
    xy = [(0,-.39)]*11+ [(-.07,-.27),(.07,-.27),(-.12,-.15),(.12,-.15),(-.15,-.03),(.15,-.03),
          (-.16,0),(.16,0),(-.16,-.02),(.16,-.02),(-.14,0),(.14,0),(-.055,0),(.055,0),
          (-.06,.18),(.06,.18),(-.065,.36),(.065,.36),(-.07,.37),(.07,.37),(-.09,.38),(.09,.38)]
    out=[]
    for i,(a,b) in enumerate(xy):
        if i in (15,16): b += wrist
        if i == 27: b += ankle
        if i == 28: b -= ankle
        ca,sa=math.cos(angle),math.sin(angle)
        out.append(dict(x=x+a*ca-b*sa, y=y+a*sa+b*ca, z=0, visibility=visibility))
    return out


def scenario_frames(name, person_id, start=None, duration=8, session_id=None):
    if name not in SCENARIOS:
        raise ValueError('Unknown scenario')
    start = start or datetime.now(ZoneInfo('America/Toronto')).replace(hour=2,minute=10,second=0,microsecond=0).timestamp()
    session_id = session_id or str(uuid4())
    for i in range(round(duration*15)):
        t=i/15
        x,y,angle,wrist,ankle=.5,.55,0,0,0
        if name in ('fall', 'long_lie') and t>=2:
            a=min(1,(t-2)/.65)
            y,angle=.55+.22*a,math.pi/2*a
        elif name == 'stumble' and 2<=t<3.4:
            x += .09*math.sin((t-2)*12)
            wrist=.13*math.sin((t-2)*16)
            ankle=.045*math.sin((t-2)*13)
        elif name == 'sway' and t>=1:
            x += .06*math.sin(t*6)
        elif name == 'normal':
            x += .015*math.sin(t)
            y += .015*math.sin(t*.8)
        elif name == 'bed_exit':
            x=.2 if t<2 else .2+min(.3,(t-2)*.18)
        pose=dict(track_id='subject',landmarks=skeleton(x,y,angle,wrist,ankle,.1 if name=='low_visibility' else .99))
        poses=[pose]
        if name=='assisted_standing':
            poses.append(dict(track_id='helper',landmarks=skeleton(x+.22,y)))
        yield PoseFrame(session_id=session_id,camera_id='synthetic-demo',person_id=person_id,timestamp=start+t,
                        poses=poses,monitored_track_id='subject',source='replay')
