"""
Omega - "clearing" method diagnostic (research tool)
====================================================

PURPOSE
-------
The shipped Omega (v2) uses a *Directory placeholder* trick to "mess up writes"
to the trustd validity DB and the MobileIdentityData ban plists:

    backup.Directory("trustd/valid.sqlite3", "ProtectedDomain")   # v2
    backup.Directory("MobileIdentityData/Rejections.plist", "DatabaseDomain")

On iOS 27 the restore post-process was reworked and silently *ignores* those
placeholders, so the unrevoke has no effect. (Confirmed: iOS 27 does NOT reject
these with ErrorCode 205 - 205 is a Nugget PosterBoard/AppDomain issue, not this.)

The ORIGINAL BlacklistBeGone v1.0 (git b9a8f62) used a different, simpler method:
it wrote the SAME files with *cleared content* via ConcreteFile:

    backup.ConcreteFile(".../Rejections.plist",      contents=plistlib.dumps({}))
    backup.ConcreteFile(".../ocspcache.sqlite3",     contents=b'')   # truncation

i.e. it *cleared* (truncated) the files rather than turning them into directories.
Because iOS 27 has no 205, those ConcreteFile entries should at least be ACCEPTED
at the protocol layer. The open question this diagnostic answers on a real device:

    Does writing cleared content to the iOS 27 live validity DB
    (ProtectedDomain/trustd/valid.sqlite3) actually clear app revokes,
    and if so, for how long before trustd self-repairs / re-validates?

WHAT THIS TOOL DOES
-------------------
1. Builds the v1.0 CLEARING-method payload (6 FileToRestore objects with
   cleared/empty content) against the iOS 27 live targets:
       DatabaseDomain/MobileIdentityData/{Rejections,AuthListBannedUpps,
                                          AuthListBannedCdHashes}.plist
       ProtectedDomain/trustd/valid.sqlite3{-shm,-wal}
   The modern path delivers these through GoldenNugget's ``restore_files``
   six-segment restore (src.restore.restore). The Backup / ConcreteFile /
   Manifest.db primitives come from GoldenNugget's src.restore package, which
   auto-selects the modern iOS 27 sqlite Manifest.db format from the device
   version.
2. Prints every restore manifest record (domain, path, flags, uid/gid, mode,
   size, hash) so you can see EXACTLY what is about to be sent.
3. With a device connected and after you type CLEAR, runs restore_files (the
   six-segment _files restore) with NO reboot (so you can observe the window
   immediately). The legacy path uses the raw perform_restore instead.
4. Reports whether the restore was accepted (no protocol-level exception) - this
   is the "no 205" confirmation.
5. Prints a measurement plan for the clear window.

MODES
-----
  (default / --modern)  Use v2/omega's iOS 27-correct domains+paths
                         (DatabaseDomain / ProtectedDomain, valid.sqlite3).
  --legacy              Reproduce v1.0 *exactly*: SysContainerDomain path
                         traversal reaching /var/db/MobileIdentityData and
                         /var/protected/trustd/private/ocspcache.sqlite3.
                         *** CONFIRMED DEAD ON iOS 27 (user-verified 2026-10-25):
                         ocspcache.sqlite3 no longer exists - it was renamed to
                         valid.sqlite3. This mode writes to a non-existent path
                         and has no effect. Kept only to demonstrate the dead
                         path / let you re-verify; do NOT expect a result. ***

EXPERIMENT KNOBS
----------------
  --sqlite-payload {zero,space,nullbyte}
        Content written to valid.sqlite3 / ocspcache.sqlite3.
        zero    -> b''            (faithful to v1.0 truncation; default)
        space   -> b' '           (1-byte non-empty, in case 0-byte is skipped)
        nullbyte-> b'\\x00'       (1-byte non-empty alternative)
  --reboot   Request a reboot after restore (forces trustd to reopen the DB).
             Default: no reboot, so you can measure the window live.

SAFETY
------
This is EXPERIMENTAL. It writes into ProtectedDomain/DatabaseDomain. It can
damage the device or cause data loss. It does NOT run the protective 3-phase
backup/restore - if something goes wrong you may need to restore the device
normally. Use at your own risk. Nothing is sent until you type CLEAR.

This file is independent of omega.py and does not modify the shipped flow.
"""

import asyncio
import hashlib
import os
import plistlib
import sys

# --- Use GoldenNugget's iOS-27-aware restore implementation -----------------
# GoldenNugget's restore code lives in the package `src.restore` under the
# project root (src/restore/__init__.py defines perform_restore;
# src/restore/backup.py defines Backup/ConcreteFile/Directory). Its internal
# modules import each other as `from src.utils...`, so the PROJECT ROOT must be
# on sys.path (not src/). We also honor a `goldennugget` package if it is ever
# installed, for portability.
def _resolve_goldennugget_root():
    candidates = [
        os.environ.get("GOLDENNUGGET_SRC"),
        "/Users/jason/GoldenNugget",
        os.path.expanduser("~/GoldenNugget"),
    ]
    for c in candidates:
        if c and os.path.isfile(os.path.join(c, "src", "restore", "__init__.py")):
            return c
    raise SystemExit(
        "Could not locate GoldenNugget/src. Set GOLDENNUGGET_SRC to the "
        "GoldenNugget project root (e.g. /Users/jason/GoldenNugget).")

try:
    # If GoldenNugget is installed as a `goldennugget` package, prefer it.
    from goldennugget import backup, perform_restore
    from goldennugget import restore_files
    from goldennugget import FileToRestore
except Exception:
    _gn_root = _resolve_goldennugget_root()
    if _gn_root not in sys.path:
        sys.path.insert(0, _gn_root)
    from src.restore import backup, perform_restore
    from src.restore.restore import restore_files
    from src.utils.file_to_restore import FileToRestore

from pymobiledevice3.exceptions import NoDeviceConnectedError
from pymobiledevice3.lockdown import create_using_usbmux


def _sqlite_payload(kind: str) -> bytes:
    return {
        "zero": b"",
        "space": b" ",
        "nullbyte": b"\x00",
    }[kind]


def _progress(p):
    """Minimal mobilebackup2 progress_callback (receives an int or dict)."""
    try:
        print(f"  restore progress: {p}", flush=True)
    except Exception:
        pass


def _modern_file_specs(sqlite_payload: bytes):
    """The 6 iOS 27 live targets as (relativePath, domain, contents)."""
    empty_plist = plistlib.dumps({})  # empty dict -> clears the ban list
    return [
        ("MobileIdentityData/Rejections.plist",          "DatabaseDomain", empty_plist),
        ("MobileIdentityData/AuthListBannedUpps.plist",  "DatabaseDomain", empty_plist),
        ("MobileIdentityData/AuthListBannedCdHashes.plist", "DatabaseDomain", empty_plist),
        ("trustd/valid.sqlite3",   "ProtectedDomain", sqlite_payload),
        ("trustd/valid.sqlite3-shm", "ProtectedDomain", sqlite_payload),
        ("trustd/valid.sqlite3-wal", "ProtectedDomain", sqlite_payload),
    ]


def build_modern_files(sqlite_payload: bytes):
    """The 6 iOS 27 live targets as FileToRestore objects.

    Handed to GoldenNugget's ``restore_files`` six-segment restore. Setting
    owner=0/group=0 and leaving mode=None (=DEFAULT 0o755, flags=4) mirrors
    ConcreteFile's defaults exactly, so the manifest restore_files builds is
    byte-for-byte what the old one-shot Backup produced - just delivered via
    the _files path instead of a pre-built Backup.
    """
    specs = _modern_file_specs(sqlite_payload)
    return [
        FileToRestore(contents=c, restore_path=p, domain=d, owner=0, group=0)
        for (p, d, c) in specs
    ]


def print_files(files):
    """Show exactly what restore_files will receive (before device delivery)."""
    print(f"  files: {len(files)}")
    for f in files:
        c = f.contents
        if isinstance(c, (bytes, bytearray)):
            clen, h = len(c), hashlib.sha1(c).hexdigest()
        elif isinstance(c, str):
            clen, h = len(c), hashlib.sha1(c.encode()).hexdigest()
        else:
            clen, h = -1, ""
        print(f"  dom={f.domain!r} path={f.restore_path!r}")
        print(f"      owner={f.owner} group={f.group} mode={f.mode} "
              f"len={clen} sha1={h[:12]}")


def build_legacy_backup(sqlite_payload: bytes):
    """EXACT v1.0 (b9a8f62) method: SysContainerDomain traversal + ocspcache."""
    empty_plist = plistlib.dumps({})
    base = "SysContainerDomain-../../../../../../../../"
    return backup.Backup(files=[
        backup.Directory("", base + "var/db/MobileIdentityData/"),
        backup.ConcreteFile("", base + "var/db/MobileIdentityData/Rejections.plist",
                            contents=empty_plist),
        backup.ConcreteFile("", base + "var/db/MobileIdentityData/AuthListBannedUpps.plist",
                            contents=empty_plist),
        backup.ConcreteFile("", base + "var/db/MobileIdentityData/AuthListBannedCdHashes.plist",
                            contents=empty_plist),
        backup.Directory("", base + "var/protected/trustd/private/"),
        # v1.0 targeted ocspcache.sqlite3 (the iOS <=26 name). On iOS 27 this
        # file is likely renamed to valid.sqlite3, so this may write a dead path.
        backup.ConcreteFile("", base + "var/protected/trustd/private/ocspcache.sqlite3",
                            contents=sqlite_payload),
        backup.ConcreteFile("", base + "var/protected/trustd/private/ocspcache.sqlite3-shm",
                            contents=sqlite_payload),
        backup.ConcreteFile("", base + "var/protected/trustd/private/ocspcache.sqlite3-wal",
                            contents=sqlite_payload),
    ], apps=[])


def print_manifest(back: backup.Backup):
    mbdb = back.generate_manifest_db()
    print(f"  records: {len(mbdb.records)}")
    for r in mbdb.records:
        mode = int(r.mode)
        ftype = mode & 0o0170000          # S_IFMT mask
        if ftype == 0o0040000:            # S_IFDIR
            kind = "DIR "
        elif ftype == 0o0120000:          # S_IFLNK
            kind = "LNK "
        else:                             # S_IFREG (and anything else)
            kind = "FILE"
        print(f"  [{kind}] dom={r.domain!r}")
        print(f"          path={r.filename!r}")
        print(f"          flags={r.flags} uid={r.user_id} gid={r.group_id} "
              f"mode={oct(int(mode))} size={r.size} hash={r.hash.hex()[:12]}")


async def main():
    import argparse
    ap = argparse.ArgumentParser(description="Omega v1.0 clearing-method diagnostic")
    ap.add_argument("--legacy", action="store_true",
                    help="Reproduce v1.0 exactly (SysContainerDomain traversal + ocspcache.sqlite3)")
    ap.add_argument("--sqlite-payload", choices=["zero", "space", "nullbyte"],
                    default="zero", help="Content for the sqlite file (default: zero/truncate)")
    ap.add_argument("--reboot", action="store_true",
                    help="Request reboot after restore (default: no reboot)")
    ap.add_argument("--skip-confirm", action="store_true",
                    help="DANGER: skip the CLEAR confirmation (for scripted runs)")
    args = ap.parse_args()

    sqlite_payload = _sqlite_payload(args.sqlite_payload)
    mode = "LEGACY v1.0 (SysContainerDomain traversal + ocspcache.sqlite3)" \
        if args.legacy else "MODERN (DatabaseDomain/ProtectedDomain + valid.sqlite3)"
    delivery = ("perform_restore (raw, legacy path-traversal - restore_files "
                "drops empty-domain files)") if args.legacy else \
               "restore_files (six-segment _files restore)"
    print("=" * 70)
    if args.legacy:
        print("!!! DEAD PATH (iOS 27): ocspcache.sqlite3 does not exist - it was")
        print("    renamed to valid.sqlite3. This mode writes to a non-existent")
        print("    file and WILL NOT clear anything. Use the default MODERN mode")
        print("    (targets valid.sqlite3) for the only live iOS 27 hypothesis.")
        print("=" * 70)
    print("Omega - clearing-method diagnostic (RESEARCH / EXPERIMENTAL)")
    print("=" * 70)
    print(f"mode            : {mode}")
    print(f"sqlite payload  : {args.sqlite_payload!r} ({len(sqlite_payload)} bytes)")
    print(f"reboot after    : {args.reboot}")
    print(f"delivery       : {delivery}")
    print()
    print("WARNING: writes into ProtectedDomain/DatabaseDomain. Can damage the")
    print("device or cause data loss. Does NOT run the protective backup. Use at")
    print("your own risk.")
    print()

    if args.legacy:
        # Legacy v1.0 reproduction: empty-domain SysContainerDomain traversal.
        # restore_files DROPS files with no real domain (iOS 26.2+ can't
        # deliver the path-traversal exploit), so the legacy demo stays on the
        # raw perform_restore path to faithfully reproduce v1.0.
        back = build_legacy_backup(sqlite_payload)
        print("=== Planned restore manifest (legacy v1.0, exactly what will be sent) ===")
        print_manifest(back)
        print()
    else:
        # Modern clear hypothesis: 6 FileToRestore -> restore_files six-segment.
        files = build_modern_files(sqlite_payload)
        print("=== Planned FileToRestore list (restore_files input) ===")
        print_files(files)
        print()
        print("NOTE: the Backup + Manifest.db is built INSIDE restore_files, which")
        print("      auto-selects the iOS 26 (MBDB) vs iOS 27 (sqlite) manifest")
        print("      format from the connected device's version.")
        print()

    # Connect (async, matches the current sparserestore API).
    try:
        lockdown = await create_using_usbmux()
    except NoDeviceConnectedError:
        print("No device connected -> preview only. Connect a device and re-run to execute.")
        return

    if not args.skip_confirm:
        ans = input('Type CLEAR to send the restore, or anything else to abort: ').strip()
        if ans != "CLEAR":
            print("Aborted.")
            return

    if 1:
        print("Sending six-segment restore via GoldenNugget restore_files ...")
        try:
            await restore_files(
                files,
                reboot=args.reboot,
                lockdown_client=lockdown,
                progress_callback=_progress,
                skip_protective_backup=False,   # single raw sparse pass (matches prior behavior)
                skip_setup=False,              # do not skip setup on reboot (matches prior behavior)
        
            )
            print()
            print("RESULT: restore_files ACCEPTED with no protocol-level exception.")
            print("        (On iOS 27 this means: NOT rejected with 205. The entries")
            print("         landed. Whether the CLEAR EFFECT persists depends on")
            print("         trustd self-repair / re-validation - measure it below.)")
        except Exception as e:
            print()
            print(f"RESULT: restore_files raised -> {type(e).__name__}: {e}")
            print("        A raise here is the protocol-layer signal. If it is NOT a")
            print("        205/domain-rejection, the failure is elsewhere (service")
            print("        not ready, connection drop, etc.).")

    print()
    print("=== Window measurement plan ===")
    print("The validity DB is a CACHE. If cleared, trustd will eventually")
    print("re-fetch revocation info from Apple and re-populate the bans.")
    print("Measure how long the unrevoke lasts:")
    print("  1. Immediately after this step, open a previously-revoked sideloaded app.")
    print("  2. Re-test at T+5m, T+30m, T+1h, T+24h.")
    print("  3. If it works at T0 but fails later -> window confirmed; clearing")
    print("     is viable as a recurring action, not a permanent fix.")
    print("  4. If it NEVER works -> trustd self-repairs instantly (rebuilds the")
    print("     DB from a protected source). Then clearing is dead on iOS 27.")


if __name__ == "__main__":
    asyncio.run(main())
