FROM node:22-alpine AS widget-build
WORKDIR /app/packages/widget
COPY packages/widget/package.json ./
RUN npm install --no-audit --no-fund
COPY packages/widget .
RUN npm run build

FROM node:22-alpine AS build
WORKDIR /app/packages/web
COPY packages/web/package.json packages/web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY packages/web .
COPY --from=widget-build /app/packages/widget/dist/juicebox-intake-widget.js ./public/juicebox-intake-widget.js
RUN npm run build

FROM nginx:1.27-alpine
COPY --from=build /app/packages/web/dist /usr/share/nginx/html
COPY packages/web/nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
