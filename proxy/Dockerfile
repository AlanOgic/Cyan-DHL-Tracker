# Egress proxy of the tracker: lets HTTPS through only to the hosts in EGRESS_ALLOWED_HOSTS.
# Pinned Alpine release; bump it on purpose like the tracker's base image.
FROM alpine:3.24.2

RUN apk add --no-cache tinyproxy

COPY tinyproxy.conf /etc/tinyproxy/tinyproxy.conf
COPY --chmod=755 entrypoint.sh /usr/local/bin/egress-proxy

USER tinyproxy

HEALTHCHECK --interval=1m --timeout=5s --start-period=10s --start-interval=1s --retries=3 \
    CMD ["nc", "-z", "127.0.0.1", "8888"]

ENTRYPOINT ["/usr/local/bin/egress-proxy"]
