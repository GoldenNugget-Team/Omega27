from tempfile import TemporaryDirectory
from pathlib import Path

import asyncio
import ssl

from pymobiledevice3.lockdown import create_using_usbmux, UsbmuxLockdownClient
from pymobiledevice3.services.mobilebackup2 import Mobilebackup2Service
from pymobiledevice3.services.diagnostics import DiagnosticsService
from pymobiledevice3.exceptions import PyMobileDevice3Exception, ConnectionTerminatedError

from . import backup


def _is_transient_restore_error(error) -> bool:
    """True for restore errors that mean 'device busy / service not ready /
    connection dropped' — all retryable on iOS 27, where mobilebackup2 and
    SpringBoard take minutes to come back after the security recovery.

    Covers:
      - connection drops (reboot, SSL tear-down)  -> OSError/ssl/ConnectionTerminated
      - stale lockdown client after reboot        -> MissingValueError
      - service still spinning up                 -> 'InvalidService' / 'start service'
      - rejected protected-domain placeholder      -> ErrorCode 205 (some restore paths; NOT observed on iOS 27 for this tool)
    """
    if isinstance(error, (ConnectionTerminatedError, OSError, ssl.SSLError, asyncio.TimeoutError)):
        return True
    text = str(error)
    name = type(error).__name__
    if "InvalidService" in name or "InvalidService" in text:
        return True
    low = text.lower()
    if "missingvalue" in low:            # stale lockdown client after reboot
        return True
    if "start" in low and "service" in low:
        return True
    if "205" in text:                    # ErrorCode 205: rejected domain/file
        return True
    return False


async def perform_restore(backup: backup.Backup, client: UsbmuxLockdownClient,
                          reboot: bool = False, max_retries: int = 6):
    try:
        with TemporaryDirectory() as backup_dir:
            backup.write_to_directory(Path(backup_dir))

            last_err = None
            for attempt in range(1, max_retries + 1):
                try:
                    async with Mobilebackup2Service(client) as mb:
                        # system=True rides the restore service; copy=False so we
                        # only inject/sparse the listed domains, not a full restore.
                        # iOS 27 may reject individual protected-domain placeholder
                        # entries (ErrorCode 205) or drop the connection when it
                        # reboots — both are transient here.
                        await mb.restore(backup_dir, system=True, reboot=reboot,
                                         copy=False, source=".")
                    last_err = None
                    break
                except (PyMobileDevice3Exception, ConnectionTerminatedError,
                        OSError, ssl.SSLError, asyncio.TimeoutError) as e:
                    last_err = e
                    # If we asked for a reboot and the only failure is the
                    # connection dropping (device rebooted) on the final attempt,
                    # treat the restore as accepted and move on.
                    if reboot and attempt >= max_retries and _is_transient_restore_error(e):
                        print(f"[restore] device rebooted during restore; "
                              f"assuming command accepted: {e}")
                        last_err = None
                        break
                    if attempt >= max_retries or not _is_transient_restore_error(e):
                        raise
                    print(f"[restore] transient error (attempt {attempt}/{max_retries}), "
                          f"retrying: {e}")
                    await asyncio.sleep(min(5 * attempt, 30))

            if reboot:
                # mb.restore(reboot=True) already reboots the device; a second,
                # explicit restart on the (now dead) client is best-effort only.
                try:
                    async with DiagnosticsService(client) as diagnostics_service:
                        await diagnostics_service.restart()
                except Exception as e:  # noqa: BLE001 - best effort
                    print(f"[restore] post-restore reboot request skipped ({e}); "
                          f"device should reboot on its own.")
    except PyMobileDevice3Exception as e:
        if "Find My" in str(e):
            print("Find My must be disabled in order to use this tool.")
            print("Disable Find My from Settings (Settings -> [Your Name] -> Find My) and then try again.")
            raise e
        elif "crash_on_purpose" not in str(e):
            raise e
