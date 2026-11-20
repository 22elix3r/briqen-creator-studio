FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates ffmpeg fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 studio \
    && useradd --system --uid 10001 --gid 10001 --home-dir /app --no-create-home studio \
    && install -d -o studio -g studio /media /fixtures
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY studio ./studio
COPY tests ./tests
USER 10001:10001
