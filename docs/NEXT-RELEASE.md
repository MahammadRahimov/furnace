# Next release: v0.10.0 (not released)

What changes in the next release, compared with v0.9.0.

How a release happens (user decision 2026-11-01):

1. Every finished change is added to this list. The lab can be updated and tried at any time.
2. Nothing is released until the user says "update" or "new version".
3. Then: the full tests (unit tests, the 11-system integration round, the package tests and the Docker tests),
   then the version on GitHub and the release with the packages, the Docker image and the compose files.
4. After the release, this list is emptied for the version after it.

## What changes

Nothing yet.

Already decided (user decision 2026-11-01): SSH replication is removed (HTTPS only). Not started.

To talk about first (user, 2026-11-05): an "external keepalived" mode for Docker. The host runs keepalived, not
the container. Needs: a setting that turns off the built-in keepalived, a notify script that writes the role
file for the container, and a health check on the host. Nothing decided yet.
