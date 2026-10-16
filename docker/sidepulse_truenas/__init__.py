"""TrueNAS status collector for SidePulse.

Polls the TrueNAS JSON-RPC API (read-only calls) and turns pool health,
alerts, running backup jobs and available updates into an LED program, which
it writes to a shared file. It never touches the device: the sidepulse_hw
container, which has no network, validates that file and writes it.
"""
