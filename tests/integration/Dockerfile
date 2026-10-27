# A clean Debian or Ubuntu with systemd. BASE is the image, for example debian:12.
ARG BASE=ubuntu:24.04
FROM ${BASE}
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -qq \
 && apt-get install -y -qq systemd systemd-sysv iproute2 sudo python3 curl ca-certificates >/dev/null \
 && apt-get clean && rm -rf /var/lib/apt/lists/* \
 && rm -f /lib/systemd/system/multi-user.target.wants/getty* \
 && echo "furnace-test" > /etc/hostname
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
