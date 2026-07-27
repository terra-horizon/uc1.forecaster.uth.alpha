FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MPLBACKEND=Agg

WORKDIR /app

COPY requirements.txt .
RUN pip install --upgrade pip \
    && pip install -r requirements.txt

COPY config ./config
COPY collector ./collector
COPY collector_bootstrap.py ./collector_bootstrap.py
COPY forecaster ./forecaster
COPY hydro ./hydro
COPY scripts ./scripts
COPY README.md ./README.md

RUN pip install ./collector

RUN useradd --create-home --shell /usr/sbin/nologin terra \
    && chown -R terra:terra /app

USER terra

ENTRYPOINT ["python", "-m", "forecaster.scheduled_pipeline"]
