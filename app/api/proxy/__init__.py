"""Data-plane routes: the OpenAI-compatible proxy under ``/g/{slug}/v1``.

Kept import-free so that ``app.services`` can import ``app.api.proxy.errors`` — which
defines the data-plane error contract — without a cycle through the routes.
"""
