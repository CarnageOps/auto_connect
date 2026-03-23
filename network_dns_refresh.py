"""
Network DNS Refresh — Flush / Renew / Set DNS Provider

Automates the manual "ipconfig /flushdns && ipconfig /renew" dance and
optionally switches the IPv4 DNS servers on a given network adapter to a
public resolver (Google, Cloudflare, Quad9, OpenDNS, AdGuard, CleanBrowsing,
or Comodo).

Requires: Windows, Python 3.8+, no third-party packages.
Must run elevated (Run as Administrator) for renew and DNS changes.

Usage examples (from an elevated terminal):

    # Flush + renew + set Cloudflare DNS on Wi-Fi
    python network_dns_refresh.py --provider cloudflare

    # Same, but target a specific adapter
    python network_dns_refresh.py --provider google --interface "Ethernet 2"

    # Preview commands without executing
    python network_dns_refresh.py --provider cloudflare --dry-run

    # Only flush + renew, don't touch DNS servers
    python network_dns_refresh.py

    # Only set DNS (skip flush and renew)
    python network_dns_refresh.py --provider google --skip-flush --skip-renew
"""

from __future__ import annotations

import argparse
import ctypes
import logging
import subprocess
import sys

log = logging.getLogger("network_dns_refresh")

DNS_PROVIDERS: dict[str, tuple[str, str]] = {
    "google":        ("8.8.8.8",       "8.8.4.4"),
    "cloudflare":    ("1.1.1.1",       "1.0.0.1"),
    "quad9":         ("9.9.9.9",       "149.112.112.112"),
    "opendns":       ("208.67.222.222", "208.67.220.220"),
    "adguard":       ("94.140.14.14",  "94.140.15.15"),
    "cleanbrowsing": ("185.228.168.9", "185.228.169.9"),
    "comodo":        ("8.26.56.26",    "8.20.247.20"),
}


def _is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
    except AttributeError:
        return False


def _run(cmd: list[str], *, dry_run: bool = False) -> subprocess.CompletedProcess[str] | None:
    pretty = " ".join(cmd)
    if dry_run:
        log.info("[dry-run] %s", pretty)
        return None
    log.info(">>> %s", pretty)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.stdout.strip():
        log.info(result.stdout.strip())
    if result.stderr.strip():
        log.warning(result.stderr.strip())
    if result.returncode != 0:
        log.error("Command exited %d", result.returncode)
    return result


def _detect_default_interface() -> str | None:
    """Return the InterfaceAlias of the adapter carrying the default route."""
    result = subprocess.run(
        [
            "powershell", "-NoProfile", "-Command",
            "(Get-NetRoute -DestinationPrefix '0.0.0.0/0' "
            "| Sort-Object RouteMetric | Select-Object -First 1).InterfaceAlias",
        ],
        capture_output=True,
        text=True,
    )
    alias = result.stdout.strip()
    return alias if alias and result.returncode == 0 else None


def flush_dns(*, dry_run: bool = False) -> None:
    _run(["ipconfig", "/flushdns"], dry_run=dry_run)


def renew_dhcp(*, dry_run: bool = False) -> None:
    _run(["ipconfig", "/renew"], dry_run=dry_run)


def set_dns(interface: str, provider: str, *, dry_run: bool = False) -> None:
    primary, secondary = DNS_PROVIDERS[provider]
    _run(
        [
            "netsh", "interface", "ip", "set", "dns",
            f"name={interface}", "static", primary,
        ],
        dry_run=dry_run,
    )
    _run(
        [
            "netsh", "interface", "ip", "add", "dns",
            f"name={interface}", secondary, "index=2",
        ],
        dry_run=dry_run,
    )
    log.info(
        "DNS for [%s] -> %s (%s, %s)",
        interface, provider, primary, secondary,
    )


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Flush DNS, renew DHCP, and optionally set a public DNS provider.",
    )
    p.add_argument(
        "--provider",
        choices=list(DNS_PROVIDERS),
        default=None,
        help="DNS provider to configure (omit to skip DNS change).",
    )
    p.add_argument(
        "--interface",
        default=None,
        help='Network adapter alias, e.g. "Wi-Fi" or "Ethernet". '
             "Auto-detected from the default route if omitted.",
    )
    p.add_argument("--skip-flush", action="store_true", help="Skip ipconfig /flushdns.")
    p.add_argument("--skip-renew", action="store_true", help="Skip ipconfig /renew.")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print commands without executing them.",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument(
        "--log-file",
        default=None,
        help=argparse.SUPPRESS,  # used internally by the GUI for elevated subprocesses
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    level = logging.DEBUG if args.verbose else logging.INFO
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if args.log_file:
        handlers.append(logging.FileHandler(args.log_file, encoding="utf-8"))
    logging.basicConfig(
        level=level,
        format="%(levelname)-7s %(message)s",
        handlers=handlers,
    )

    if sys.platform != "win32":
        log.error("This script only works on Windows.")
        return 1

    if not args.dry_run and not _is_admin():
        log.error(
            "Not running as Administrator. "
            "Right-click your terminal and choose 'Run as administrator', "
            "or re-run with --dry-run to preview commands."
        )
        return 1

    if not args.skip_flush:
        flush_dns(dry_run=args.dry_run)

    if not args.skip_renew:
        renew_dhcp(dry_run=args.dry_run)

    if args.provider:
        iface = args.interface
        if not iface:
            iface = _detect_default_interface()
            if not iface:
                log.error(
                    "Could not auto-detect network interface. "
                    "Pass --interface explicitly."
                )
                return 1
            log.info("Auto-detected interface: %s", iface)
        set_dns(iface, args.provider, dry_run=args.dry_run)

    log.info("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
