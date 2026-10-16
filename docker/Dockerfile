FROM alpine:3.20
RUN apk add --no-cache mtools
COPY sidepulse-sync.sh /usr/local/bin/sidepulse-sync
ENTRYPOINT ["/bin/sh", "/usr/local/bin/sidepulse-sync"]
