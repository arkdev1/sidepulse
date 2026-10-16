# Build from the repository root: docker build -f docker/Dockerfile .
FROM python:3.12-slim-bookworm
RUN apt-get update \
    && apt-get install -y --no-install-recommends mtools \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir wasmtime==49.0.0
WORKDIR /app
COPY docker/sidepulse_hw /app/sidepulse_hw
# The firmware's own LEDS.LED parser, shared with the main package.
COPY src/sidepulse/resources/sdled.wasm /app/sidepulse_hw/sdled.wasm
COPY docker/tests /app/tests
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "-m", "sidepulse_hw"]
CMD ["watch"]
