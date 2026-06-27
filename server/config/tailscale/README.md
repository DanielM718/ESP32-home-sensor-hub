# Tailscale Configuration Notes

The Raspberry Pi joins your Tailnet using:

```bash
/opt/home-sensor/server/scripts/install_tailscale.sh
```

No real auth keys or Tailnet secrets are stored in this repository.

Use `tailnet-policy-example.hujson` as a starting point for a Tailnet ACL policy
that restricts access to the Pi services.
