"""Versioned model gate. Shadow remains the default until clip review validates it."""
from dataclasses import dataclass

POLICY_VERSION = 'rules-ml-gemini-v1'


@dataclass(frozen=True)
class Decision:
    action: str
    reason: str
    policy_version: str = POLICY_VERSION


def decide(rule_hits, floor, model_result, threshold):
    if 'prolonged_stillness' in rule_hits or floor >= 3:
        return Decision('fallback_open', 'urgent_rule_evidence')
    if model_result is None or model_result.get('status') != 'scored':
        return Decision('fallback_open' if floor >= 2 else 'hold',
                        'model_unavailable_or_uncertain')
    supported = (('fall' in rule_hits and model_result.get('fall_score', 0) >= threshold)
                 or (any(hit in rule_hits for hit in ('stumble', 'sway'))
                     and model_result.get('staggering_score', 0) >= threshold))
    if supported:
        return Decision('supported', 'model_supports_rule')
    if any(hit in rule_hits for hit in ('fall', 'stumble', 'sway')):
        return Decision('rejected', 'model_did_not_support_rule')
    return Decision('hold', 'model_has_no_matching_action_class')
