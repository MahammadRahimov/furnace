# A clean Ubuntu with systemd, used to test install.sh like on a real server.
ARG UBUNTU=24.04
FROM ubuntu:${UBUNTU}
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -qq \
 && apt-get install -y -qq systemd systemd-sysv iproute2 sudo python3 curl ca-certificates >/dev/null \
 && apt-get clean && rm -rf /var/lib/apt/lists/* \
 && rm -f /lib/systemd/system/multi-user.target.wants/getty* \
 && echo "ubuntu-test" > /etc/hostname
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
