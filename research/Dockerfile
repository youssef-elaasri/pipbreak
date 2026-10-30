# Sandbox for mining PRs: untrusted checkouts and their dependencies live only in here.
ARG PYTHON=3.13
FROM python:${PYTHON}-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Unprivileged user; /work and /results are owned by it so a fresh named volume inherits that.
RUN useradd --uid 10001 --create-home miner \
 && mkdir -p /work /results \
 && chown miner:miner /work /results

COPY --chown=root:root apicheck /app/apicheck
COPY --chown=root:root research/mine.py research/upgrades.py research/run_check.py research/known_breaks.py /app/research/

ENV APICHECK_SAMPLE=/data/sample.json \
    APICHECK_WORK=/work \
    APICHECK_RESULTS=/results \
    HOME=/tmp \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

USER miner
WORKDIR /app
ENTRYPOINT ["python"]
