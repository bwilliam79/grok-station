"""Stable default portrait for a bot that has no usable photo.

default_avatar_index(slug) is 1..18. The same formula is in admin/web/app.js (defaultAvatarIndex).
"""
import hashlib


def default_avatar_index(slug):
    return int(hashlib.sha256(slug.encode()).hexdigest(), 16) % 18 + 1
