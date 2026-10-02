# 1) 画面（frontend/）を作る
FROM node:22-slim AS ui
WORKDIR /ui
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# 2) 本体（Python）
FROM python:3.12-slim
WORKDIR /app/backend
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 FARM_RADAR_UI_DIR=/app/frontend/dist
COPY backend/pyproject.toml ./
COPY backend/farm_radar ./farm_radar
RUN pip install --no-cache-dir .
COPY --from=ui /ui/dist /app/frontend/dist
# 設定と会場定義・チェーンの登録は docker-compose でマウントする（/app/config.yaml, /app/venues, /app/chains）
