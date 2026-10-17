# MMAction2 attribution

`stgcn.py`, `gcn.py`, `graph.py`, and `labels.txt` derive from
[OpenMMLab MMAction2 v1.2.0](https://github.com/open-mmlab/mmaction2/tree/v1.2.0).
Copyright (c) OpenMMLab. All rights reserved. Apache License 2.0 is included as LICENSE.

Sources: `mmaction/models/backbones/stgcn.py`, `mmaction/models/utils/gcn_utils.py`,
`mmaction/models/utils/graph.py`, and `tools/data/skeleton/label_map_ntu60.txt`.

Modifications: removed the registry and unused AAGCN implementation; replaced
MMEngine/MMCV inference wrappers with equivalent torch.nn modules. Training and
initialization infrastructure is not included. The classifier head is implemented
in `../../action_model.py` with the upstream average pooling and linear projection.

Reference: Duan, Wang, Chen, Lin. PYSKL: Towards Good Practices for Skeleton Action
Recognition (2022), https://arxiv.org/abs/2205.09443.

The checkpoint is distributed separately by OpenMMLab. Apache-2.0 covers this
source; it does not grant rights to redistribute NTU RGB+D recordings. Consult
the dataset's conditions before acquiring training data or fine-tuning.
