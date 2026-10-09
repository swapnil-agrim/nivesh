# Deploying Nivesh on a small VM

Nivesh is single-user and makes outbound calls only. Two commands cover day-to-day use:
`docker compose run --rm nivesh run <command>` and `docker compose run --rm nivesh backup`.

## VM
A small VM (1 vCPU, 2 GB) in an Indian region (for example Mumbai), so broker calls originate from
India. Install Docker and the compose plugin.

## Static IP
Reserve a static public IP and attach it to the VM. Broker API access may be restricted to
registered IPs, and the egress IP must not change.

## Firewall
No inbound traffic except SSH (or a VPN). The compose file publishes no ports.

## Secrets
Secrets are injected as environment variables (`ANTHROPIC_API_KEY` is passed through from the
host environment by `docker-compose.yml`); the OS keychain is not available in the container.
Never bake secrets into the image or commit them. `nivesh secrets set` is for desktop use only.

## Data volume ownership
The container runs as uid 10001 and requires the data directory to be 0700 and owned by it:

    mkdir -p data && sudo chown 10001:10001 data && chmod 700 data

A root-owned mount fails with a permission error; this is the fix.

## Register the IP with HDFC
Manual, not verified by tests: register the VM's static IP in the HDFC Securities API portal
(IP whitelisting) before first use. Steps are the broker's and may change. The full registration
steps and the mismatch symptoms are in `docs/runbooks/investright.md`.

## Daily login via SSH tunnel
Manual, not verified by tests: the broker login callback lands on localhost port 8765. Forward it with
`ssh -L 8765:localhost:8765 <vm>` and complete the login in your local browser each day (or use
`nivesh login --paste`). See `docs/runbooks/investright.md`.

## Egress check
Set `registered_ip` in `config/nivesh.yaml`, then run `docker compose run --rm nivesh egress-check`.
It exits 1 when the VM's public IP differs from the registered one. `nivesh run` prints the same
warning at startup but proceeds.

## Backup cron
Set `backup.target` and `backup.recipient` (an age public key) in config. Keep the age identity
(private key) off the VM. Host cron, nightly until the E11 scheduler exists:

    0 2 * * * cd /opt/nivesh && docker compose run --rm nivesh backup

Restore: `nivesh restore <archive> --identity <identity-file> --data-dir <new-dir>`.
