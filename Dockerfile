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
COPY forecaster ./forecaster
COPY hydro ./hydro
COPY forecast.py ./forecast.py
COPY README.md ./README.md

RUN useradd --create-home --shell /usr/sbin/nologin terra \
    && chown -R terra:terra /app

USER terra

ENTRYPOINT ["python", "forecast.py"]
