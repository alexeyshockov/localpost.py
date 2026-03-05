Hey, I'm working on a new Python web framework, very slim and compact.

The design idea is in DESIGN.md

What I have currently:
- http/router.py — WIP on the router part, which is an slim layer (like Werkzeug or Starlette), to communicate with the underlying HTTP server
- http/openapi/app.py — WIP on the actual framework logic
- spec/openapi/__init__.py — code taken from defspec Python library, but we need to rework it completely

Some random thoughts:
- if a op function returns something (like a dataclass), it means that it's Ok[this_dataclass] (because it should be OpResult in the end)
- 

Please take a look at it, take a look at the code, and plan the work need to make it working.
