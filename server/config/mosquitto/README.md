# Mosquitto Configuration

Generated files:

- `home-sensor.conf`: broker listener, authentication, ACL, persistence, and logging defaults
- `home-sensor.acl`: least-privilege topic access for gateway, bridge, and the
  optional Home Assistant consumer/discovery publisher

Install locations on the Raspberry Pi:

```text
/etc/mosquitto/conf.d/home-sensor.conf
/etc/mosquitto/acl.d/home-sensor.acl
/etc/mosquitto/passwd
```

`/etc/mosquitto/passwd` is generated on the Pi with `mosquitto_passwd`; it is
not committed to this repository.
