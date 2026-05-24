FROM node:20-alpine AS build
WORKDIR /app

COPY package.json package-lock.json ./

RUN npm ci

COPY . .
# `project` is a buildx named context (passed via --build-context), not a FROM alias.
COPY --from=project versions.json ./versions.json

RUN npm run build

FROM nginx:alpine

RUN rm -rf /usr/share/nginx/html/*

COPY --from=build /app/dist /usr/share/nginx/html

COPY nginx.conf.template /etc/nginx/templates/default.conf.template
COPY entrypoint.sh /docker-entrypoint.d/99-custom-start.sh
RUN chmod +x /docker-entrypoint.d/99-custom-start.sh

EXPOSE 80
