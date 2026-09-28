FROM node:22-alpine AS ui
WORKDIR /build/frontend
COPY frontend/package*.json ./
RUN npm install
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /opt/swarmguard
COPY pyproject.toml README.md ./
COPY src/ src/
RUN pip install --no-cache-dir .
COPY config/ config/
COPY --from=ui /build/frontend/dist /opt/swarmguard/frontend/dist
ENV SWARMGUARD_UI_DIR=/opt/swarmguard/frontend/dist
EXPOSE 8000
CMD ["swarmguard-api"]
