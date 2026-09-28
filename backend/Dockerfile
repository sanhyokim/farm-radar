FROM python:3.12-slim
WORKDIR /app/backend
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY backend/pyproject.toml ./
COPY backend/farm_radar ./farm_radar
RUN pip install --no-cache-dir .
# 設定と会場定義は docker-compose でマウントする（/app/config.yaml, /app/venues）
