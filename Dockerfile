# k6's own "-with-browser" image: k6 + a matching Chromium, already
# configured headless/no-sandbox. Alpine-based; add plain python3, stdlib only.
FROM grafana/k6:latest-with-browser@sha256:863201153fc967785909ebc2c95e29307b94c38e2fbb4f36852aac98577c5801

USER root
RUN apk add --no-cache python3

WORKDIR /app
COPY dashboard_checker/ ./dashboard_checker/

USER 12345
EXPOSE 8080
# Overrides the base image's own k6 entrypoint; dashboard_checker/browser/scan.py
# still shells out to k6.
ENTRYPOINT ["python3", "-m", "dashboard_checker"]
