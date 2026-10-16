# Build from the repository root: docker build -f docker/Dockerfile .
# Only the files listed in Dockerfile.dockerignore reach the build.

# Debian 12 and Python 3.11, matching the runtime image below.
FROM python:3.11-slim-bookworm@sha256:0a310eeecf4e1f5a0743f9a6520c90c88d089c903ca5fd283f501e3a805f5f89 AS build
COPY docker/requirements.txt /tmp/requirements.txt
# Debian packages are signed by Debian but not version-pinned: security
# updates replace old versions, so pins would break rebuilds. binutils strips
# wasmtime; mtools and openssl only serve the tests and stay out of the final image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends binutils mtools openssl \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir --require-hashes --only-binary :all: \
        --target /deps -r /tmp/requirements.txt \
    && strip --strip-unneeded /deps/wasmtime/linux-*/_libwasmtime.so
COPY docker/sidepulse_hw /app/sidepulse_hw
COPY docker/sidepulse_truenas /app/sidepulse_truenas
# The firmware's own LEDS.LED parser, shared with the main package.
COPY src/sidepulse/resources/sdled.wasm /app/sidepulse_hw/sdled.wasm
RUN python -m compileall -q /deps /app
ENV PYTHONPATH=/deps:/app

# docker build --target test runs the suite; the published image skips it.
FROM build AS test
COPY docker/tests /app/tests
WORKDIR /app
RUN python -m unittest discover -s tests

# No shell, no package manager: Python, wasmtime and the tool.
FROM gcr.io/distroless/python3-debian12@sha256:2fdb05402a2cf21cf78fdb3ba4c5db167241e9e498140f5bf689d7efb773731f
COPY --from=build /deps /deps
COPY --from=build /app /app
ARG SIDEPULSE_HW_COMMIT="local build"
ENV PYTHONPATH=/deps:/app PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    SIDEPULSE_HW_COMMIT=${SIDEPULSE_HW_COMMIT}
WORKDIR /app
ENTRYPOINT ["python3", "-m", "sidepulse_hw"]
CMD ["watch"]
