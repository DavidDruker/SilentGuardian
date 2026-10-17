"""Only the inference wrappers needed by the pinned ST-GCN++ architecture.

Training initializers/registries are intentionally absent. All model parameters
and buffers must be supplied by a strictly loaded, verified checkpoint.
"""
from torch import nn

ModuleList = nn.ModuleList
Sequential = nn.Sequential


class BaseModule(nn.Module):
    def __init__(self, init_cfg=None):
        super().__init__()


def build_norm_layer(config, channels):
    if config != {'type': 'BN'}:
        raise ValueError('Only the pinned checkpoint BN configuration is supported')
    return 'bn', nn.BatchNorm2d(channels)


def build_activation_layer(config):
    if config != {'type': 'ReLU'}:
        raise ValueError('Only ReLU is supported')
    return nn.ReLU()
