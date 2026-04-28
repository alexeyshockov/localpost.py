"""Experimental sub-packages.

Modules under ``localpost.experimental`` have **unstable public APIs** —
they're usable but their shape is still evolving. Expect breaking changes
in patch / minor releases. The ``experimental`` segment in every import
path is the marker; once a sub-package stabilises we'll move it back up
to ``localpost.<name>``.

See also:

* ``localpost.experimental.consumers`` — message-broker consumers.
* ``localpost.experimental.openapi`` — type-driven OpenAPI on top of
  ``localpost.http``.
"""
