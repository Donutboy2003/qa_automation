# utils/text_helpers.py
# Walk a Cascade page JSON tree and replace or remove string values.
# These operate on the raw page JSON, not on parsed HTML — they treat
# every string field in the tree as a candidate for text replacement.

from __future__ import annotations

import copy
from typing import Any


def replace_in_page_json(
    page_json: dict[str, Any],
    search_term: str,
    replace_term: str,
) -> tuple[dict[str, Any], int]:
    """
    Deep-copy a Cascade page JSON dict and replace every occurrence of
    search_term with replace_term in all string fields.

    Works across all text nodes, metadata, identifiers — anywhere a string
    value appears in the tree.

    Args:
        page_json:    The full page JSON dict (from a Cascade read response)
        search_term:  The substring to find
        replace_term: The string to substitute in its place

    Returns:
        (updated_json, number_of_replacements_made)
    """
    updated = copy.deepcopy(page_json)
    count = [0]  # use list so nested function can mutate it

    def _walk(node: Any) -> Any:
        if isinstance(node, str):
            if search_term in node:
                count[0] += node.count(search_term)
                return node.replace(search_term, replace_term)
            return node
        if isinstance(node, dict):
            return {k: _walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [_walk(item) for item in node]
        return node

    updated = _walk(updated)
    return updated, count[0]


def remove_from_page_json(
    page_json: dict[str, Any],
    search_term: str,
) -> tuple[dict[str, Any], int]:
    """
    Deep-copy a Cascade page JSON dict and clear any string field that
    contains search_term (sets it to an empty string).

    Args:
        page_json:   The full page JSON dict
        search_term: The substring to search for — any field containing
                     this string will be set to ""

    Returns:
        (updated_json, number_of_fields_cleared)
    """
    updated = copy.deepcopy(page_json)
    count = [0]

    def _walk(node: Any) -> Any:
        if isinstance(node, str):
            if search_term in node:
                count[0] += 1
                return ""
            return node
        if isinstance(node, dict):
            return {k: _walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [_walk(item) for item in node]
        return node

    updated = _walk(updated)
    return updated, count[0]
