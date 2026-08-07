"""Sample loading and deterministic context-manipulation helpers."""

from utils_context import build_context_from_folder
import json
import os
import random


def shuffle_context(context):
    """Shuffle the key order of a context dict."""
    keys = list(context.keys())
    random.shuffle(keys)
    return {k: context[k] for k in keys}


def load_sample(sample_id, samples_folder):
    """Load sample.json and return (sample, sample_folder, id2state)."""
    sample_folder = os.path.join(samples_folder, sample_id, "")
    sample_fn = f"{sample_folder}sample.json"
    with open(sample_fn, "r", encoding="utf-8") as f:
        sample = json.load(f)
    id2state = {state["state_id"]: state for state in sample["states"]}
    return sample, sample_folder, id2state


def load_distractor_context(sample_folder):
    """Load distractor context files from a sample's distractor_context/ folder.
    Returns a dict of {filename: content} or empty dict if folder doesn't exist."""
    distractor_folder = os.path.join(sample_folder, "distractor_context")
    if os.path.isdir(distractor_folder):
        return build_context_from_folder(distractor_folder)
    return {}


def merge_distractor(context, distractor_context):
    """Merge distractor files into a context dict (non-destructive copy)."""
    if distractor_context:
        merged = dict(context)
        merged.update(distractor_context)
        return merged
    return context
