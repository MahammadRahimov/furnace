# Deployment guides

Furnace runs on **two servers**: a leader and a follower. A **shared IP** (VIP) moves to the server that is alive.
The list reader (for example a firewall) and the caller (for example a SOAR) always use the shared IP.

There are three ways to deploy it. Pick one, then follow its guide from top to bottom.

| | [1. Packages](1-packages.md) | [2. Docker, keepalived in the container](2-docker-builtin-keepalived.md) | [3. Docker, keepalived on the host](3-docker-host-keepalived.md) |
|---|---|---|---|
| What you install | `.deb` or `.rpm` | one container per host | one container per host + keepalived on the host |
| Settings | setup questions (or a settings file) | `.env` file | `.env` file + a block in `keepalived.conf` |
| Who moves the shared IP | keepalived on the host, set up by Furnace | keepalived inside the container | **your** keepalived on the host |
| Host firewall | Furnace opens the ports | you open the ports | you open the ports |
| Other services use keepalived on the same host? | **No**: setup replaces `keepalived.conf` | No: two keepalived on one host fight | **Yes**: Furnace gets its own block next to theirs |
| Best for | plain Linux servers that only run Furnace | Docker hosts that only run Furnace | Docker hosts that already run keepalived |

**Not sure?** Use guide 1 on servers that only run Furnace. Use guide 3 when the hosts already have keepalived for
other services.

These guides are for **version 0.10.0** (the join code, the host's own keepalived, and the same settings file on
both servers are new in 0.10.0). All addresses in them are examples: change `192.0.2.x` to your own.

## The three IPs, in every guide

```
leader     192.0.2.11   (server or host A)
follower   192.0.2.12   (server or host B)
shared IP  192.0.2.10   (a free address in the same network; nobody owns it, keepalived moves it)
```

The settings are **the same on both servers**. Each server finds its role from its own IP: the server that has
the leader's IP is the leader. Do not swap the IPs on the follower.

## What every deployment needs

- Two Linux servers (or VMs) **in the same network segment**. The shared IP can only move inside one segment.
- Three free IP addresses in that segment (see above).
- The network must let the shared IP move. On VMware, the port group must allow "MAC address changes" and
  "forged transmits". Most cloud networks (AWS, Azure, ...) do not allow a moving IP at all.
- These connections open on the network:

| From | To | Port | Why |
|---|---|---|---|
| the list reader and the caller | the shared IP | TCP 443 | the lists and the API |
| each server | the other server | TCP 443 | replication over HTTPS, the join code |
| each server | the other server | IP protocol 112 (VRRP) | keepalived |

After the install, every guide ends with the same steps: connect the list reader and the caller (README
[section 5](../../README.md#5-checklist-of-manual-work)) and test a failover before you go live.
