FROM nginx:1.27-alpine

# nginx template lets us substitute env variables at container start
COPY nginx.conf.template /etc/nginx/templates/default.conf.template

COPY html /usr/share/nginx/html

# Default upstream — overridable via env
ENV BACKEND_HOST=entrypoint
ENV BACKEND_PORT=8000

EXPOSE 80
