FROM node:20-alpine AS build
WORKDIR /web
COPY web/staff/package.json web/staff/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/staff ./
RUN npm run build

FROM nginx:1.27-alpine
COPY infra/web.nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /web/dist /usr/share/nginx/html
EXPOSE 80
