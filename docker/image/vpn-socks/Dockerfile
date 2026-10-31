# Get tun2socks binary from official image (https://github.com/xjasonlyu/tun2socks)
FROM xjasonlyu/tun2socks:latest AS tun2socks

FROM alpine:3.19

RUN apk add --no-cache \
    openconnect \
    dante-server \
    iproute2 \
    bind-tools

COPY --from=tun2socks /usr/bin/tun2socks /usr/local/bin/tun2socks

COPY entrypoint.sh /entrypoint.sh
COPY vpnc-script.sh /vpnc-script.sh
COPY sockd.conf /etc/sockd.conf.template

RUN chmod +x /entrypoint.sh /vpnc-script.sh

EXPOSE 1080

ENTRYPOINT ["/entrypoint.sh"]
