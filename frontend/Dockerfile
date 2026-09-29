ARG BASE_REGISTRY=docker.io
FROM ${BASE_REGISTRY}/library/node:24.12.0-alpine AS build

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM ${BASE_REGISTRY}/library/nginx:1.28.0-alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80
