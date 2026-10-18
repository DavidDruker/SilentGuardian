"""Pinned, CPU ST-GCN++ inference; no MMAction/MMCV installation required."""
import hashlib
from pathlib import Path
import time

import numpy as np

MODEL_ID = 'stgcnpp-ntu60-2d-joint-20221228'
CHECKPOINT_URL = ('https://download.openmmlab.com/mmaction/v1.0/skeleton/stgcnpp/'
                  'stgcnpp_8xb16-joint-u100-80e_ntu60-xsub-keypoint-2d/'
                  'stgcnpp_8xb16-joint-u100-80e_ntu60-xsub-keypoint-2d_20221228-86e1e77a.pth')
CHECKPOINT_SHA256 = '86e1e77a7b27dd53c6ccc1600a4cc3c321ec8e04d17f25a41fbbb7896026945a'
PREPROCESSING = 'mediapipe33-coco17-xyv-linear100-2s-v1'
# COCO: nose, left/right eyes, ears, shoulders, elbows, wrists, hips, knees, ankles.
COCO_FROM_MEDIAPIPE = (0, 2, 5, 7, 8, 11, 12, 13, 14, 15, 16, 23, 24, 25, 26, 27, 28)
LABELS = (Path(__file__).parent/'vendor/stgcn/labels.txt').read_text().splitlines()
# Dataset labels contain inferred health conditions; present observable actions only.
DISPLAY_LABELS = {43: 'touch head', 44: 'touch chest', 45: 'touch back', 46: 'touch neck',
                  47: 'torso/head movement (dataset class 48)', 48: 'use a fan / wave hand'}


def prepare_sequence(samples):
    """Convert timestamped, quality-checked MediaPipe poses to N,M,T,V,C.

    Online adaptation: interpolate the most recent two seconds to 100 positions.
    This differs from the upstream full-clip benchmark's random uniform sampling.
    PreNormalize2D is exactly 2*x-1, 2*y-1 for browser-normalized coordinates.
    The third channel is visibility (not MediaPipe z); second person is zero-pad.
    """
    if len(samples) < 8:
        raise ValueError('Need at least 8 usable poses')
    ts = np.asarray([s['t'] for s in samples], dtype=np.float64)
    # Brief tracking dropouts (under .5 s) are bridged upstream and interpolated here.
    if not np.isfinite(ts).all() or np.any(np.diff(ts) <= 0) or np.max(np.diff(ts)) > .5:
        raise ValueError('Sequence is discontinuous')
    if ts[-1]-ts[0] < 1.8 or ts[-1]-ts[0] > 2.01:
        raise ValueError('Need a continuous two-second observation window')
    points = np.asarray([[[s['landmarks'][i][k] for k in ('x', 'y', 'visibility')]
                          for i in COCO_FROM_MEDIAPIPE] for s in samples], dtype=np.float32)
    if not np.isfinite(points).all() or np.any((points[..., 2] < 0) | (points[..., 2] > 1)):
        raise ValueError('Invalid landmarks')
    # Most of the body must be visible across the window. A fall briefly hides joints,
    # so judge the window as a whole; the model also receives each joint's visibility
    # as an input channel, so low-confidence joints are already down-weighted.
    visible = np.mean(points[:, 5:, 2] >= .6, axis=1)
    if visible.mean() < .7 or visible.min() < .5:
        raise ValueError('Insufficient visible body joints')
    points[..., :2] = points[..., :2]*2-1
    target = np.linspace(ts[0], ts[-1], 100)
    output = np.zeros((1, 2, 100, 17, 3), dtype=np.float32)
    for joint in range(17):
        for channel in range(3):
            output[0, 0, :, joint, channel] = np.interp(target, ts, points[:, joint, channel])
    return output


class ActionModel:
    def __init__(self, path, threads=2):
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError('Run python -m scripts.setup_action_model first')
        if hashlib.sha256(path.read_bytes()).hexdigest() != CHECKPOINT_SHA256:
            raise ValueError('ST-GCN++ checkpoint checksum mismatch')
        import torch
        from torch import nn
        from .vendor.stgcn.stgcn import STGCN
        torch.set_num_threads(threads)

        class Head(nn.Module):
            def __init__(self):
                super().__init__()
                self.pool = nn.AdaptiveAvgPool2d(1)
                self.fc = nn.Linear(256, 60)

            def forward(self, x):
                n, m, c, t, v = x.shape
                return self.fc(self.pool(x.reshape(n*m, c, t, v)).reshape(n, m, c).mean(1))

        class Network(nn.Module):
            def __init__(self):
                super().__init__()
                self.backbone = STGCN(graph_cfg=dict(layout='coco', mode='spatial'),
                                     gcn_adaptive='init', gcn_with_res=True, tcn_type='mstcn')
                self.cls_head = Head()

            def forward(self, x):
                return self.cls_head(self.backbone(x))

        self.torch, self.network = torch, Network()
        checkpoint = torch.load(path, map_location='cpu', weights_only=True)
        self.network.load_state_dict(checkpoint['state_dict'], strict=True)
        self.network.eval()
        self.parameter_count = sum(p.numel() for p in self.network.parameters())

    def predict(self, samples):
        tensor = prepare_sequence(samples)
        started = time.perf_counter()
        with self.torch.inference_mode():
            scores = self.network(self.torch.from_numpy(tensor)).softmax(-1)[0].numpy()
        if scores.shape != (60,) or not np.isfinite(scores).all():
            raise ValueError('Invalid model output')
        top = np.argsort(scores)[-5:][::-1]
        return dict(model=MODEL_ID, checkpoint_sha256=CHECKPOINT_SHA256, preprocessing=PREPROCESSING,
                    status='scored', mode='shadow', observed_at=samples[-1]['t'],
                    window_start=samples[0]['t'], source_frames=len(samples),
                    latency_ms=round((time.perf_counter()-started)*1000, 2),
                    fall_score=float(scores[42]), staggering_score=float(scores[41]),
                    top_actions=[dict(class_id=int(i), label=DISPLAY_LABELS.get(int(i), LABELS[i]),
                                      score=float(scores[i])) for i in top])
