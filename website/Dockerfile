# Serve the pre-built MkDocs site. Build context should be website/ after
# scripts/build_docs_site.sh has produced website/site/.
#
# Example:
#   ./scripts/build_docs_site.sh
#   docker build -t de-starter-docs ./website
#   docker run --rm -p 8080:8080 de-starter-docs
FROM nginx:1.27-alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY site/ /usr/share/nginx/html/

EXPOSE 8080
