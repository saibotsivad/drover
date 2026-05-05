# syntax=docker/dockerfile:1

FROM node:22-slim AS build
WORKDIR /build

COPY package.json package-lock.json ./
RUN npm ci

COPY scripts/ ./scripts/
COPY public/ ./public/
RUN npm run vendor

# -------------------------------------------------------------------

FROM node:22-slim
WORKDIR /app

COPY --chown=node:node package.json package-lock.json ./
RUN npm ci --omit=dev && npm cache clean --force

COPY --chown=node:node src/ ./src/
COPY --from=build --chown=node:node /build/public/ ./public/

USER node

ENV NODE_ENV=production \
    PORT=8080

EXPOSE 8080

CMD ["node", "src/server.js"]
