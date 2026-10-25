import platform
import sys
import click
import plistlib
import asyncio
import os
import shutil
import time

from protective import ProtectiveBackupService, perform_protective_backup, clean_backup_for_restore, _wait_for_device, _restore_protective_backup
from pathlib import Path
from sparserestore import backup, perform_restore
from pymobiledevice3.exceptions import NoDeviceConnectedError, PyMobileDevice3Exception
from pymobiledevice3.lockdown import create_using_usbmux

def exit(code=0):
    if platform.system() == "Windows" and getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        input("Press Enter to exit...")

    sys.exit(code)

async def get_device_info(client):
    os_names = {
        "iPhone": "iOS",
        "iPad": "iPadOS",
        "iPod": "iOS",
        "RealityDevice": "visionOS",
    }
    device_class = await client.get_value(key="DeviceClass")
    product_version = await client.get_value(key="ProductVersion")
    info_text = f"{await client.get_value(key="DeviceName")} ({(os_names[device_class] + " " + product_version) if device_class in os_names else ""})"
    return info_text

async def _await_service_ready(client, timeout: float = 180.0):
    """Probe the device with a cheap lockdown call until it answers.

    Replaces the blind `asyncio.sleep(5)` after the security-recovery reboot.
    On iOS 27 mobilebackup2/SpringBoard can take minutes to come back, and a
    too-early `_restore_protective_backup` hits a not-ready service and raises
    MissingValueError (the classic stale-client symptom).
    """
    deadline = time.monotonic() + timeout
    delay = 3.0
    while True:
        try:
            await client.get_value(key="ProductVersion")
            return
        except Exception:
            if time.monotonic() >= deadline:
                return  # let _restore_protective_backup's own retries take over
            await asyncio.sleep(delay)
            delay = min(delay * 1.5, 15.0)

async def main():
    try:
        lockdown = await create_using_usbmux()
    except NoDeviceConnectedError:
            click.secho("No device detected! Please connect your device via USB and try again.", fg="red")
            exit(1)

    click.secho("Omega v1.0 - by jailbreak.party\nMade possible by JJTech (@JJTech0130), Duy Tran (@khanhduytran0) and Mineek (@mineekdev)", fg="blue")
    click.secho(f"Connected to {await get_device_info(lockdown)}", fg="green")
    try:
        product_version = await lockdown.get_value(key="ProductVersion") or ""
    except Exception:
        product_version = ""

    click.secho("This will clear the app revokes and certificate validity databases.", fg="yellow")
    click.secho("NOTE: This tool is experimental and has a chance of damaging your device, or causing data loss.\nWe take zero responsibility in the event this happens. Use this tool at your own risk.", fg="red")

    while True:
            value = input("To continue, type \"CONTINUE\" and hit Enter. Otherwise, the script will exit.\n").strip()
            if value == "CONTINUE":
                click.secho("Creating backup...", fg="yellow")

                plist_contents = plistlib.dumps({})

                cloudconfig_path = Path.joinpath(Path.cwd(), "files/CloudConfigurationDetails.plist")
                cloudconfig_contents = open(cloudconfig_path, "rb").read()

                purplebuddy_path = Path.joinpath(Path.cwd(), "files/com.apple.purplebuddy.plist")
                purplebuddy_contents = open(purplebuddy_path, "rb").read()
                udid: str = lockdown.udid if lockdown.udid else ""
                if not udid: raise Exception("UDID is null.")

                back = backup.Backup(files=[
                    # using Directory now for persistence (thanks Duy!!).
                    # idk why the backup system lets us do this, but it's very nice cause it messes up writes to these files entirely
                    # now we pray misagent/installd/trustd don't get updated...
                    backup.Directory("", "DatabaseDomain"),
                    backup.Directory("MobileIdentityData", "DatabaseDomain"),
                    backup.Directory("MobileIdentityData/Rejections.plist", "DatabaseDomain"),
                    backup.Directory("MobileIdentityData/AuthListBannedUpps.plist", "DatabaseDomain"),
                    backup.Directory("MobileIdentityData/AuthListBannedCdHashes.plist", "DatabaseDomain"),
                    backup.Directory("", "ProtectedDomain"),
                    backup.Directory("trustd", "ProtectedDomain"),
                    backup.Directory("trustd/valid.sqlite3", "ProtectedDomain"),
                    backup.Directory("trustd/valid.sqlite3-shm", "ProtectedDomain"),
                    backup.Directory("trustd/valid.sqlite3-wal", "ProtectedDomain"),
                    # skip setup
                    backup.Directory("", "SysSharedContainerDomain-systemgroup.com.apple.configurationprofiles"),
                    backup.Directory("Library", "SysSharedContainerDomain-systemgroup.com.apple.configurationprofiles"),
                    backup.Directory("Library/ConfigurationProfiles", "SysSharedContainerDomain-systemgroup.com.apple.configurationprofiles"),
                    backup.ConcreteFile("Library/ConfigurationProfiles/CloudConfigurationDetails.plist", "SysSharedContainerDomain-systemgroup.com.apple.configurationprofiles", contents=cloudconfig_contents),
                    backup.Directory("", "ManagedPreferencesDomain"),
                    backup.Directory("mobile", "ManagedPreferencesDomain"),
                    backup.ConcreteFile("mobile/com.apple.purplebuddy.plist", "ManagedPreferencesDomain", contents=purplebuddy_contents),
                ])

                click.secho("Creating device backup for iOS 27...", fg="yellow")
                backup_root = "./omega_backup"
                os.makedirs(backup_root, exist_ok=True)
                await perform_protective_backup(lockdown, backup_root, lambda s: click.secho(f"Progress: {s}\r"), include_photos=True)
                removed_rows, removed_files = await asyncio.to_thread(
                    clean_backup_for_restore, backup_root, udid
                )
                print(f"[iOS27] Protective backup pruned: "
                      f"-{removed_rows} manifest rows, -{removed_files} payload files")

                # iOS 27 hardening notice (grounded in the 26.6.2 -> 27.0 diff):
                # trustd gained _SecFixTrustdFilePermissions + new revocation
                # policy checks, and BackupAgent2 now hardens file ownership
                # (_fchownat/_fcopyfile). The ProtectedDomain/trustd and
                # DatabaseDomain/MobileIdentityData "unrevoke" placeholders are
                # very likely REJECTED (ErrorCode 205) or self-healed, so
                # clearing app revokes may not take effect. Skip-setup still works.
                try:
                    major = int((product_version.split(".")[0]) if product_version else "0")
                except Exception:
                    major = 0
                if major >= 27:
                    click.secho(
                        "iOS 27 detected: trustd now self-repairs its validity DB "
                        "(_SecFixTrustdFilePermissions) and re-checks revocation with "
                        "a new policy, while BackupAgent2 hardens restore-time file "
                        "ownership. The trustd/MobileIdentityData 'unrevoke' trick is "
                        "very likely REJECTED or self-healed - app-revoke clearing may "
                        "NOT persist. Skip-setup (configurationprofiles / purplebuddy) "
                        "still applies.", fg="yellow")

                click.secho("Applying tweaks...", fg="yellow")
                try:
                    await perform_restore(back, client=lockdown, reboot=True)
                except PyMobileDevice3Exception as e:
                    if "Find My" in str(e):
                        raise
                    click.secho(f"Restore reported an error (continuing): {e}", fg="red")

                click.secho("Waiting for device reboot...", fg="yellow")
                click.secho("Tips: Unlock your device after the reboot complete.", fg="blue")
                lockdown = await _wait_for_device(udid, lambda s: click.secho(f"{s}\r", fg="green"))

                # Replace blind asyncio.sleep(5) with a readiness probe so the
                # first post-recovery service call doesn't hit a not-yet-ready
                # service and raise MissingValueError (stale-client symptom).
                await _await_service_ready(lockdown)

                click.secho("Restoring device backup...", fg="yellow")
                await _restore_protective_backup(lockdown, backup_root, udid, reboot=True, progress_callback=lambda s: click.secho(f"{s}\r", fg="green"))
                click.secho("Finished! Your device should reboot shortly. If it does not, you may reboot it manually.")
                click.secho("Do you want to remove the device backup? [Y] Yes [N] No", fg="green")
                option = input("? ").strip()
                if option in ['Y', 'y']: shutil.rmtree(backup_root, ignore_errors=True)
                else: pass
            click.secho("Exiting script...", fg="red")
            exit()

if __name__ == "__main__":
    asyncio.run(main())