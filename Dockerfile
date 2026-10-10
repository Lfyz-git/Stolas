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
ARG STOLAS_INSTANCE_ID=development
LABEL org.stolas.instance=$STOLAS_INSTANCE_ID
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 STOLAS_DATA_DIR=/data
COPY agent /app/agent
COPY config/example.json /app/config/example.json
USER 10001:10001
HEALTHCHECK --interval=15s --timeout=5s CMD python3 -c "import os,urllib.request; h=os.getenv('STOLAS_LISTEN','127.0.0.1'); h='127.0.0.1' if h=='0.0.0.0' else h; r=urllib.request.Request('http://'+h+':'+os.getenv('STOLAS_PORT','8080')+'/healthz',headers={'Authorization':'Bearer '+os.environ['STOLAS_API_TOKEN']}); urllib.request.build_opener(urllib.request.ProxyHandler({})).open(r,timeout=3)" || exit 1
ENTRYPOINT ["python3", "-m", "agent"]
CMD ["serve"]
