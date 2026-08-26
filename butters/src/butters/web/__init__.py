"""Separate bounded browser service for Butters Beta 1."""

__all__ = ["create_app"]


def create_app(*args: object, **kwargs: object):
    from butters.web.app import create_app as factory

    return factory(*args, **kwargs)
