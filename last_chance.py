# This is for things when omega interupted in process, This is the last chance that you can recover your personal data.

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

async def main():                                                                                                                                             
    lockdown = await create_using_usbmux()
    await _restore_protective_backup(lockdown, "./omega_backup", lockdown.udid, reboot=True, progress_callback=lambda str: click.secho(f"{str}\r", fg="green"))
    click.secho("Restore completed! Your personal data has recovered!", fg="green")
    click.secho("If your device is launching for a long time, that's normal, just wait for it.", fg="blue")

asyncio.run(main())