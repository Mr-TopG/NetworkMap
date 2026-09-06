# Run NetworkMap on RouterOS containers

NetworkMap's published image supports `linux/arm64`, so it can run on an
RB5009-class RouterOS device with the optional Container package. The image
starts the web server on port `8765`, requires `NETWORKMAP_TOKEN`, and stores
the SQLite database under `/data`.

## Before you begin

- Update RouterOS and install the matching `container` package.
- Enable container device mode. RouterOS requires physical confirmation.
- Attach and format external USB storage. Do not keep container images or the
  NetworkMap database on the router's built-in NAND.
- Push this repository's `main` branch once so the included GitHub workflow can
  publish `ghcr.io/mr-topg/networkmap:latest` for ARM64. In GitHub's package
  settings, make that package public or configure RouterOS registry credentials.
- Choose an unused address on a trusted management LAN. The example below uses
  `192.168.88.250`; replace the address, gateway, bridge, and `usb1` disk name
  to match the router.

## RouterOS example

Generate a long token on a trusted computer and keep it private:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Then adapt these commands in a RouterOS terminal:

```routeros
/system/device-mode/update container=yes

/container/config/set registry-url=https://ghcr.io tmpdir=usb1/networkmap/tmp
/interface/veth/add name=veth-networkmap address=192.168.88.250/24 gateway=192.168.88.1
/interface/bridge/port/add bridge=bridge interface=veth-networkmap

/container/envs/add list=networkmap-env key=NETWORKMAP_TOKEN value="REPLACE_WITH_LONG_RANDOM_TOKEN"
/container/mounts/add list=networkmap-mounts src=usb1/networkmap/data dst=/data
/container/add remote-image=mr-topg/networkmap:latest interface=veth-networkmap root-dir=usb1/networkmap/root mountlists=networkmap-mounts envlist=networkmap-env name=networkmap logging=yes start-on-boot=yes
```

Wait until `/container/print` shows `status=stopped`, then start it:

```routeros
/container/start networkmap
```

Open `http://192.168.88.250:8765` and enter the configured token. Configure the
Linux app to synchronize its offline local copy with the router:

```bash
networkmap --server-url http://192.168.88.250:8765
```

The veth is attached directly to the example LAN so optional discovery can see
that LAN. Restrict access to trusted management devices with RouterOS bridge/IP
firewall rules appropriate to your configuration. Plain HTTP exposes the
session to anyone able to intercept that LAN; prefer an HTTPS reverse proxy,
management VPN, or isolated management VLAN.

To update after a new image has been published:

```routeros
/container/stop networkmap
/container/repull networkmap
/container/start networkmap
```

RouterOS container configuration is security-sensitive. Review every address,
mount, environment value, and firewall rule rather than pasting this example
unchanged.
