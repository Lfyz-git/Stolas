# Alpine's multi-architecture manifest and every APK payload are content-locked.
FROM alpine:3.23.0@sha256:51183f2cfa6320055da30872f211093f9ff1d3cf06f39a0bdb212314c5dc7375
COPY config/apk.lock.json /tmp/apk.lock.json
COPY config/apk-*.lock /tmp/
# BusyBox wget and sha256sum bootstrap without an unpinned package install.
COPY tools/install-apk.sh /tmp/install-apk.sh
RUN sh /tmp/install-apk.sh && rm /tmp/install-apk.sh /tmp/apk.lock.json \
    && python3 --version && iperf3 --version && ip -Version \
    && addgroup -g 10001 stolas && adduser -D -u 10001 -G stolas stolas \
    && mkdir /data && chown stolas:stolas /data
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 STOLAS_DATA_DIR=/data
COPY agent /app/agent
COPY config/example.json /app/config/example.json
USER 10001:10001
HEALTHCHECK --interval=60s --timeout=5s CMD python3 -c "import os,urllib.request; r=urllib.request.Request('http://127.0.0.1:'+os.getenv('STOLAS_PORT','8080')+'/healthz',headers={'Authorization':'Bearer '+os.environ['STOLAS_API_TOKEN']}); urllib.request.urlopen(r,timeout=3)" || exit 1
ENTRYPOINT ["python3", "-m", "agent"]
CMD ["serve"]
