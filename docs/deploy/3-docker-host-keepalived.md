# Deployment guide 3: Docker, keepalived on the host

Each of the two hosts runs one Furnace container, but the **host's own keepalived** moves the shared IP. Use this
when the hosts already run keepalived for other services. Furnace gets **its own** `vrrp_instance` (its own shared
IP and router ID) next to your other blocks, so a Furnace problem moves only Furnace's shared IP.

[Back to the overview](README.md)

```
   host A 192.0.2.11                                   host B 192.0.2.12
   ┌──────────────────────────────┐                   ┌──────────────────────────────┐
   │ keepalived (yours)           │ ◄──── VRRP ─────► │ keepalived (yours)           │
   │   vrrp_instance YOUR_SERVICE │                   │   vrrp_instance YOUR_SERVICE │
   │   vrrp_instance FURNACE ─────┼─ shared IP .10    │   vrrp_instance FURNACE      │
   │     notify -> /run/furnace/role                  │     notify -> /run/furnace/role
   │ container "furnace" (no keepalived) ── HTTPS ──► │ container "furnace"          │
   └──────────────────────────────┘                   └──────────────────────────────┘
```

How it works:
- Your keepalived runs a small **health check** script: is Furnace alive on this host?
- Its **notify** script writes `leader` or `follower` into `/run/furnace/role` on the host.
- The container reads that file (the folder is mounted) and acts as the leader or the follower.
- **Furnace never changes your host.** It prints the block and the two scripts; you add them.

## Before you start

- Everything from [guide 2](2-docker-builtin-keepalived.md#before-you-start) "Before you start", except
  "no other keepalived". (VRRP between the hosts is probably open already for your other services.)
- keepalived installed on both hosts: `sudo apt install keepalived` or `sudo dnf install keepalived`.
- A **free VRRP router ID** for Furnace: not used by your other `vrrp_instance` blocks, and not by anything else
  on the network. The default is `51`. If 51 is taken, set another one in `.env`: `FURNACE_VRRP_ROUTER_ID=52`.
- Your `keepalived.conf` `global_defs` should have:
  ```
  global_defs {
      script_user root
      enable_script_security
  }
  ```

## The order

The container can add tokens and make the join code only when it is the **leader**, and in this mode the host's
keepalived decides that. So the order is:

1. Host A: start the container.
2. Host A: add the scripts and the Furnace block to keepalived. Host A becomes the leader.
3. Host A: the first API token, the list login and the join code.
4. Host B: start the container with the join code, then add the scripts and the block to its keepalived.
5. Failover test.

## Step 1. Host A: start the container

Do [guide 2](2-docker-builtin-keepalived.md) steps 1 and 2 on host A (download, `.env`), and add one more line to
`.env`:

```
FURNACE_KEEPALIVED=external
```

Now the container runs no keepalived. It also needs no `NET_ADMIN`/`NET_RAW`: you may remove the `cap_add` lines
from `docker-compose.yml` (optional).

```bash
cd ~/furnace
docker compose run --rm furnace check
docker compose up -d
docker logs furnace                  # "Ready."
docker exec furnace furnace status   # warns "no role from the host's keepalived yet": normal until step 2
```

Until the host's keepalived writes the role, the container works as the follower. That is safe.

## Step 2. Host A: the host's keepalived

Do 2a to 2c on host A now. Host B does the same in step 4.
`docker exec furnace furnace host-keepalived` (without a word after it) shows all of 2a to 2c for this host.

### 2a. The two scripts

```bash
docker exec furnace furnace host-keepalived notify       | sudo tee /usr/local/sbin/furnace-notify >/dev/null
docker exec furnace furnace host-keepalived health-check | sudo tee /usr/local/sbin/furnace-health-check >/dev/null
sudo chmod 755 /usr/local/sbin/furnace-notify /usr/local/sbin/furnace-health-check
```

With SELinux on (Red Hat family), let keepalived run them:

```bash
sudo semanage fcontext -a -t keepalived_unconfined_script_exec_t '/usr/local/sbin/furnace-(notify|health-check)'
sudo restorecon -v /usr/local/sbin/furnace-notify /usr/local/sbin/furnace-health-check
```

### 2b. The Furnace block in keepalived.conf

Print the block. It is already filled in **for this host** (card, both IPs, router ID, priority, password), so
print it on each host separately:

```bash
docker exec furnace furnace host-keepalived config
```

It looks like this (on host A):

```
vrrp_script chk_furnace {
    script "/usr/local/sbin/furnace-health-check"
    interval 5
    timeout 4
    fall 3
    rise 2
}

vrrp_instance FURNACE {
    state BACKUP
    nopreempt
    interface eth0
    virtual_router_id 51
    priority 150                  # 100 on host B
    advert_int 1
    unicast_src_ip 192.0.2.11     # 192.0.2.12 on host B
    unicast_peer {
        192.0.2.12                # 192.0.2.11 on host B
    }
    authentication {
        auth_type PASS
        auth_pass ********        # the same on both hosts (Furnace's keepalived password)
    }
    virtual_ipaddress {
        192.0.2.10/24 dev eth0
    }
    track_script {
        chk_furnace
    }
    notify /usr/local/sbin/furnace-notify
}
```

Add it to `/etc/keepalived/keepalived.conf`, **below** your other blocks. Do not change your own blocks. Back up
the file first:

```bash
sudo cp /etc/keepalived/keepalived.conf /etc/keepalived/keepalived.conf.before-furnace
docker exec furnace furnace host-keepalived config | sudo tee -a /etc/keepalived/keepalived.conf >/dev/null
```

### 2c. Restart keepalived and check

```bash
sudo keepalived -t -f /etc/keepalived/keepalived.conf   # checks the file (keepalived 2.x)
sudo systemctl restart keepalived
cat /run/furnace/role                                   # "leader" on host A
docker exec furnace furnace status                      # Role: leader
ip -br addr | grep 192.0.2.10                           # the shared IP is on host A
```

Restarting keepalived can move your other services' shared IPs for a moment. Do it in a quiet time.

## Step 3. Host A: tokens, list login, join code

```bash
docker exec -it furnace furnace token add soar         # asks the permission grid and the days
docker exec -it furnace furnace edl-user add paloalto  # the list login for the firewall
docker exec furnace furnace join-code                  # FJ1-....  (once, 1 hour, only from host B)
```

Each secret is shown **once**. Store it now.

## Step 4. Host B: the container, then its keepalived

Do [guide 2](2-docker-builtin-keepalived.md) step 1 on host B, and copy the **same `.env`** from host A (with
`FURNACE_KEEPALIVED=external`). Add the join code and start:

```bash
cd ~/furnace
echo 'FURNACE_JOIN_CODE=FJ1-ABCD-EFGH-IJKL-MNOP-QRST-UVWX-YZ23-4567' >> .env
docker compose run --rm furnace check     # "this host is the follower (192.0.2.12 ...)"
docker compose up -d
docker logs furnace                       # "fetched from the leader 192.0.2.11 with the join code" and "Ready."
```

No join code possible? Use the bundle file, as in
[guide 2, step 6](2-docker-builtin-keepalived.md#step-6-start-the-follower-host-b).

Then do **2a to 2c on host B**. Its block says `priority 100` and has the two IPs the other way round. That is
right: always print the block on the host it is for. `cat /run/furnace/role` on host B says `follower`, and the
shared IP stays on host A.

## Step 5. Failover test

```bash
# host A
docker stop furnace
```

The health check fails, and after about 15 seconds the Furnace shared IP moves to host B (host B becomes the
leader). **Your other services' shared IPs do not move.** Then:

```bash
docker start furnace       # host A comes back as the follower; it does not take the shared IP back
```

## Step 6. Connect the list reader and the caller

The same as [guide 2, step 8](2-docker-builtin-keepalived.md#step-8-connect-the-list-reader-and-the-caller).

## Good to know

- If keepalived on a host stops, the shared IP moves to the other host. Furnace in the container keeps running.
- If `/run/furnace/role` is missing, the container works as the follower and `furnace status` warns. `/run` is
  emptied at a reboot; keepalived writes the file again when it starts.
- The VRRP router ID or the shared IP changed in `.env`? Run `docker compose up -d`, print the block again
  (step 2b), replace the old Furnace block, and restart keepalived.
- To go back to the built-in keepalived ([guide 2](2-docker-builtin-keepalived.md)): remove the Furnace block from
  `keepalived.conf`, restart keepalived, remove `FURNACE_KEEPALIVED=external` from `.env`, and run
  `docker compose up -d`. If you removed `cap_add`, put it back first.
- Daily work, backup and upgrade: the same as [guide 2](2-docker-builtin-keepalived.md#daily-work-backup-upgrade).
