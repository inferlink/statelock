# Build targets:
#   runtime  the Statelock proxy (default)
#   dev      runtime + dev tools, tests and examples (editable install)
#   ojs      dev + Stagehand, for the OJS demo (examples/ojs: the mock journal and the agent)
# The Playwright base image pins Chromium; the pip pin below must match its version.
# Ubuntu 24.04 (noble), Python 3.12: browser-use needs Python 3.11+ (jammy has 3.10).
ARG PLAYWRIGHT_IMAGE=mcr.microsoft.com/playwright/python:v1.56.0-noble

FROM ${PLAYWRIGHT_IMAGE} AS runtime
# Ubuntu 24.04 marks its Python "externally managed"; this image is ours alone, so pip
# installs into it directly.
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    STATELOCK_POLICY_FILE=/app/policies/default.yaml \
    STATELOCK_ARTIFACT_DIR=/app/artifacts
WORKDIR /app
# Fail the build on an older Python; give compose's `python ...` commands a `python`.
RUN python3 -c 'import sys; assert sys.version_info >= (3, 11), sys.version' \
    && (command -v python >/dev/null || ln -s "$(command -v python3)" /usr/local/bin/python)
COPY pyproject.toml README.md LICENSE NOTICE /src/statelock/
COPY src /src/statelock/src
RUN python3 -m pip install "/src/statelock" "playwright==1.56.0"
COPY policies /app/policies
RUN mkdir -p /app/artifacts && chown -R pwuser:pwuser /app
USER pwuser
EXPOSE 8000
CMD ["uvicorn", "--factory", "statelock.app:create_app", "--host", "0.0.0.0", "--port", "8000"]

FROM runtime AS dev
USER root
# browser-use too, so its tests (tests/test_browser_browser_use.py) run in this image, and litellm
# (vlm) for visual_assert against a model (the perception-tests service).
RUN python3 -m pip install -e "/src/statelock[dev,browser-use,vlm]"
COPY tests /src/statelock/tests
COPY examples /app/examples
RUN chown -R pwuser:pwuser /app /src
USER pwuser

FROM dev AS ojs
USER root
RUN python3 -m pip install -e "/src/statelock[stagehand]" && mkdir -p /app/ojs-output && chown pwuser:pwuser /app/ojs-output
USER pwuser
