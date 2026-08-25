import platform
import sys
import click
import plistlib
import asyncio
import os
import shutil

from protective import ProtectiveBackupService, perform_protective_backup, clean_backup_for_restore, _wait_for_device, _restore_protective_backup
from pathlib import Path
from sparserestore import backup, perform_restore
from pymobiledevice3.exceptions import NoDeviceConnectedError
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

async def main():
    try:
        lockdown = await create_using_usbmux()
    except NoDeviceConnectedError:
            click.secho("No device detected! Please connect your device via USB and try again.", fg="red")
            exit(1)

    click.secho("Omega v1.0 - by jailbreak.party\nMade possible by JJTech (@JJTech0130), Duy Tran (@khanhduytran0) and Mineek (@mineekdev)", fg="blue")
    click.secho(f"Connected to {await get_device_info(lockdown)}", fg="green")

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
                click.secho("Applying tweaks...", fg="yellow")
                await perform_restore(back, client=lockdown, reboot=True)

                click.secho("Waiting for device reboot...", fg="yellow")
                click.secho("Tips: Unlock your device after the reboot complete.", fg="blue")
                await _wait_for_device(udid, lambda str: click.secho(f"{str}\r", fg="green"))
                
                click.secho("Restoring device backup...", fg="yellow")
                lockdown = await create_using_usbmux() # new lockdown 
                await asyncio.sleep(5)
                await _restore_protective_backup(lockdown, backup_root, udid, reboot=True, progress_callback=lambda str: click.secho(f"{str}\r", fg="green"))
                click.secho("Finished! Your device should reboot shortly. If it does not, you may reboot it manually.")
                click.secho("Do you want to remove the device backup? [Y] Yes [N] No", fg="green")
                option = input("? ").strip()
                if option in ['Y', 'y']: shutil.rmtree(backup_root, ignore_errors=True)
                else: pass
            click.secho("Exiting script...", fg="red")
            exit()

if __name__ == "__main__":
    asyncio.run(main())