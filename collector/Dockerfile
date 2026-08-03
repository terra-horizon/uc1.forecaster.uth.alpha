FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MPLBACKEND=Agg

WORKDIR /app

COPY pyproject.toml ./pyproject.toml
COPY data_collection ./data_collection
COPY collect.py ./collect.py
COPY README.md ./README.md
COPY DATA_CONTRACT.md ./DATA_CONTRACT.md

RUN pip install --upgrade pip \
    && pip install . \
    && useradd --create-home --shell /usr/sbin/nologin terra \
    && chown -R terra:terra /app

USER terra

ENTRYPOINT ["python", "collect.py"]
