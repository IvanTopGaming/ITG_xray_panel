FROM node:20-alpine AS build
WORKDIR /app

ARG UI_PACKAGE

RUN test -n "$UI_PACKAGE" || { \
        echo "UI_PACKAGE build-arg is required (admin or node)" >&2; \
        exit 1; \
    }

COPY package.json package-lock.json ./
COPY packages/ui-core/package.json packages/ui-core/
COPY packages/admin/package.json packages/admin/
COPY packages/node/package.json packages/node/

RUN npm ci

COPY . .
COPY --from=project versions.json ./versions.json

RUN npm run build --workspace "@panel/$UI_PACKAGE" && \
    cp -r "packages/$UI_PACKAGE/dist" /app/dist

FROM nginx:alpine

RUN rm -rf /usr/share/nginx/html/*

COPY --from=build /app/dist /usr/share/nginx/html

COPY nginx.conf.template /etc/nginx/templates/default.conf.template
COPY entrypoint.sh /docker-entrypoint.d/99-custom-start.sh
RUN chmod +x /docker-entrypoint.d/99-custom-start.sh

EXPOSE 80
