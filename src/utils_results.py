"""Result identity helpers."""

import uuid


def generate_response_id():
    """Return a compact unique response identifier."""
    return uuid.uuid4().hex[:24]
