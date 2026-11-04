# Next release: v0.9.0 (not released)

What changes in the next release, compared with v0.8.0.

How a release happens (user decision 2026-11-01):

1. Every finished change is added to this list. The lab can be updated and tried at any time.
2. Nothing is released until the user says "update" or "new version".
3. Then: the full tests (unit tests, the 11-system integration round and the package tests), then the version
   on GitHub and the release with the packages.
4. After the release, this list is emptied for the version after it.

## What changes

1. **Docker image.** Furnace also runs as a Docker image (Debian 12, the same bundled Flask and gunicorn): two
   hosts, one container each, leader and follower from the same image, keepalived moves the shared IP
   (`network_mode: host`, only `NET_ADMIN` and `NET_RAW`, not privileged). A small process manager replaces
   systemd (API, nginx, keepalived, replication every 20 seconds over HTTPS, daily backup, nightly cleanup).
   Settings in `.env` (`docker/.env.example`); `docker/docker-compose.yml` is the same on both hosts and never
   edited. The leader makes the certificate and secrets and a follower bundle; the first token and list login
   with `docker exec`, never in `docker logs`. The image is a file on the release (`docker load`), no registry.
   README sections 3.6 and 3.7.
2. **Docker test lab on one host** (`docker/docker-compose.lab.yml`): leader, follower and a client box on a
   private Docker network, nothing to change. For testing and learning only.

Already decided for the version after v0.8.0 (user decision 2026-11-01): SSH replication is removed (HTTPS only).
Not started.
