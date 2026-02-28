## HTTP server


## REST (OpenAPI) framework

Focus:
- LLM apps, APIs
    - streaming

Vision:
- batteries not included
    - no DB, just Python to/from HTTP orchestration
- go from specs
    - OpenAPI app
    - OpenRPC app
    - JSON-API app
        - application/vnd.api+json
        - https://github.com/apiad/jsonapi
    - ...
- plain old (sync) Python, to eliminate async complexity and pitfalls
- (type) inference as much as possible, to eliminate boilerplate
    - make life easier when possible, when we can infer things from Python code (like OpenAPI schema from types, etc.)
- 


### How is it different from?
- Flask
  - web first (templates, etc), no built-in OpenAPI support
      - https://flask-restless.readthedocs.io/en/latest/
- FastAPI
  - Pydantic only
  - OpenAPI only
  - async only
  - more complex (dependencies, etc.)


## JSON-RPC (OpenRPC) framework
