# Build once, inspect for the immutable local image ID, then configure BugAgent
# with that sha256:... ID. The sandbox itself never pulls an image at run time.
FROM python:3.13-slim

RUN pip install --no-cache-dir pytest==8.4.1

WORKDIR /workspace

USER 10001:10001
