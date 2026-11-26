#!/usr/bin/env python3
"""janitor - find what is eating your disk, and say plainly what is safe to remove.

Read only until you ask it to clean, and even then it goes to the Recycle Bin.

    python janitor.py                    # report, nothing is touched
    python janitor.py --clean            # show the plan, ask, then recycle
    python janitor.py --clean --yes      # no question
    python janitor.py --roots "D:\\"     # look somewhere else
    python janitor.py --json             # for scripting
"""

import argparse
import ctypes
import json
import os
import re
import sys
import time
from ctypes import wintypes
from pathlib import Path

SAFE = "safe"
ASK = "ask"
KEEP = "keep"

# First match wins, so the specific rules sit above the general ones.
#
# Each rule is (kind, value, verdict, why), matched against the path below the
# folder being scanned:
#   seg  - one folder name is exactly this
#   end  - one folder name ends with this
#   path - the path contains this
# Whole-name matching is the point. "Tempest" and "Template" are not temp, and
# "cache2" is not a cache, however much they look like one.
RULES = [
    # never touch
    ("seg", "windows", KEEP, "part of Windows"),
    ("seg", "program files", KEEP, "installed software"),
    ("seg", "program files (x86)", KEEP, "installed software"),
    ("path", "windows defender", KEEP, "antivirus signatures"),
    ("seg", ".git", KEEP, "git history"),
    ("path", "recycle bin", KEEP, "your recycle bin, empty it yourself"),
    ("path", "everyagent", KEEP, "a project you are working on"),
    ("path", ".dsh", KEEP, "reference tree, marked do not touch"),
    # ask first, with the reason
    ("end", "-updater", ASK, "the updater needs this to install an update"),
    ("seg", "updater", ASK, "an app may be waiting to install an update"),
    ("seg", "update", ASK, "an app may be waiting to install an update"),
    ("seg", "ms-playwright", ASK, "browser tooling, in use while an agent runs"),
    ("seg", "node_modules", ASK, "regenerates, but re-downloading can take a while"),
    ("seg", ".venv", ASK, "python environment, regenerates slowly"),
    ("seg", "steam", ASK, "game installs and saves"),
    ("seg", "onedrive", ASK, "cloud files, deleting here deletes them online"),
    ("seg", "programs", ASK, "installed apps live here"),
    ("path", "roaming\\python", ASK, "python packages you installed"),
    # genuinely disposable
    ("seg", "temp", SAFE, "temporary files, rebuilt on demand"),
    ("seg", "tempstate", SAFE, "temporary state, rebuilt on demand"),
    ("seg", "tmp", SAFE, "temporary files, rebuilt on demand"),
    ("seg", "crashdumps", SAFE, "crash dumps, only useful right after a crash"),
    ("seg", "crashpad", SAFE, "crash reports"),
    ("seg", "code cache", SAFE, "compiled code cache, rebuilds itself"),
    ("end", "cache", SAFE, "a cache, the app rebuilds it on its own"),
    ("end", "cache_data", SAFE, "browser cache, rebuilds itself"),
    ("end", "cachestorage", SAFE, "browser cache, rebuilds itself"),
    ("seg", "cacheddata", SAFE, "compiled cache, rebuilds itself"),
    ("seg", "npm-cache", SAFE, "downloaded npm packages, re-downloads when needed"),
    ("seg", "_cacache", SAFE, "downloaded packages, re-downloads when needed"),
    ("seg", "pnpm-store", SAFE, "downloaded packages, re-downloads when needed"),
    ("path", "pip\\cache", SAFE, "downloaded python packages, re-downloads when needed"),
    ("seg", "caches", SAFE, "a cache, the app rebuilds it on its own"),
    ("end", "logs", SAFE, "log files"),
    ("seg", "log", SAFE, "log files"),
    ("seg", "dumps", SAFE, "diagnostic dumps"),
]

DEFAULT_ROOTS = [
    "%LOCALAPPDATA%",
    "%APPDATA%",
    "%TEMP%",
    "%USERPROFILE%\\Downloads",
]

# Even if a rule says safe, nothing here is ever touched.
PROTECTED = [
    "\\desktop\\",
    "\\documents\\",
    "\\pictures\\",
    "\\videos\\",
    "\\music\\",
    "\\onedrive\\",
    "\\dropbox\\",
    "\\windows\\",
    "\\program files",
    "\\program files (x86)\\",
    "\\.dsh\\",
    "\\.git\\",
    # Tools keep working files here while they run. Small, and deleting it
    # mid-session breaks whatever is using it.
    "\\temp\\opencode\\",
]


def classify(rel):
    """Match against the path below the folder being scanned, never the whole
    absolute path. Otherwise everything inside C:\\...\\Temp looks like temp.

    Matching is per folder name, not per substring, so `Template` is not temp.
    """
    parts = [p for p in re.split(r"[\\/]+", str(rel).lower()) if p]
    if not parts:
        return None, ""
    joined = "\\".join(parts)
    for kind, value, verdict, why in RULES:
        if kind == "seg":
            hit = value in parts
        elif kind == "end":
            hit = any(p.endswith(value) for p in parts)
        else:
            hit = value in joined
        if hit:
            return verdict, why
    return None, ""


def protected(path):
    low = "\\" + str(path).lower().strip("\\") + "\\"
    return any(p in low for p in PROTECTED)


def expand(p):
    return Path(os.path.expandvars(p))


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0


def subtree_size(path, deadline):
    total = 0
    stack = [str(path)]
    while stack:
        if time.time() > deadline:
            return total
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            total += e.stat(follow_symlinks=False).st_size
                    except (OSError, ValueError):
                        continue
        except (OSError, ValueError):
            continue
    return total


def walk(root, deadline):
    """One pass over a folder. Returns total bytes, safe bytes, the safe
    subfolders found, and whether it ran out of time.

    A folder that is safe is measured once and not walked into again, so its
    bytes are never counted twice.
    """
    total = 0
    safe_total = 0
    safe_dirs = []
    base = str(root)
    stack = [base]
    while stack:
        if time.time() > deadline:
            return total, safe_total, safe_dirs, True
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            verdict, _ = classify(e.path[len(base) + 1 :])
                            if verdict == SAFE and not protected(e.path):
                                size = subtree_size(e.path, deadline)
                                total += size
                                safe_total += size
                                safe_dirs.append((e.path, size))
                            else:
                                stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            total += e.stat(follow_symlinks=False).st_size
                    except (OSError, ValueError):
                        continue
        except (OSError, ValueError):
            continue
    return total, safe_total, safe_dirs, False


def children(path):
    out = []
    try:
        with os.scandir(path) as it:
            for e in it:
                try:
                    if e.is_dir(follow_symlinks=False):
                        out.append(Path(e.path))
                except OSError:
                    continue
    except OSError:
        pass
    return out


def analyse(top, deadline):
    total, safe, safe_dirs, cut = walk(top, deadline)
    # The folder's own name decides whether everything inside inherits that
    # verdict: a child of %TEMP% is temp, a child of LocalAppData is not.
    own_verdict, own_why = classify(top.name)
    if own_verdict == SAFE and not protected(top):
        verdict, why = SAFE, own_why
    elif safe >= total * 0.85 and total > 0:
        verdict, why = SAFE, "almost all of it is cache"
    elif safe > 0:
        verdict, why = ASK, "of which %s is cache" % human(safe)
    elif own_verdict:
        verdict, why = own_verdict, own_why
    else:
        verdict, why = ASK, "not a known cache, check what it is first"
    safe_dirs.sort(key=lambda d: d[1], reverse=True)
    return {
        "path": str(top),
        "name": top.name,
        "size": total,
        "safe": safe,
        "verdict": verdict,
        "why": why,
        "partial": cut,
        "caches": [{"path": p, "size": s} for p, s in safe_dirs],
    }


# --- Recycle Bin, not the void -------------------------------------------

FO_DELETE = 3
FOF_ALLOWUNDO = 0x0040
FOF_NOCONFIRMATION = 0x0010
FOF_NOERRORUI = 0x0400
FOF_SILENT = 0x0004


class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", ctypes.c_uint16),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


def recycle(path):
    """Send one path to the Recycle Bin. Returns (ok, message)."""
    op = SHFILEOPSTRUCTW()
    op.hwnd = None
    op.wFunc = FO_DELETE
    op.pFrom = str(path) + "\0\0"
    op.pTo = None
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_NOERRORUI | FOF_SILENT
    op.fAnyOperationsAborted = False
    try:
        res = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    except Exception as exc:  # noqa: BLE001 - report, never crash mid-clean
        return False, str(exc)
    if op.fAnyOperationsAborted:
        return False, "cancelled"
    if res == 0:
        return True, "recycled"
    if res == 120:  # DE_ACCESSDENIEDSRC
        return False, "windows says no, the folder is locked or protected"
    if res == 124:  # DE_INVALIDFILES
        return False, "in use by a running app, or it changed while being removed"
    return False, "windows error %d" % res


def plan(rows):
    """The exact folders a clean run would recycle, biggest first."""
    items = []
    for row in rows:
        for cache in row["caches"]:
            if protected(cache["path"]):
                continue
            items.append(cache)
    items.sort(key=lambda c: c["size"], reverse=True)
    return items


def clean(rows, assume_yes):
    items = plan(rows)
    if not items:
        print("Nothing it can safely remove. Nothing was touched.")
        return

    total = sum(c["size"] for c in items)
    print()
    print("About to send %d folders (%s) to the Recycle Bin:" % (len(items), human(total)))
    print()
    for c in items[:40]:
        name = c["path"]
        if len(name) > 76:
            name = "..." + name[-73:]
        print("  %-9s %s" % (human(c["size"]), name))
    if len(items) > 40:
        print("  ... and %d more" % (len(items) - 40))
    print()

    if not assume_yes:
        try:
            answer = input("Type yes to continue: ").strip().lower()
        except EOFError:
            print("No terminal to ask in. Nothing was touched. Re-run with --yes when you mean it.")
            return
        if answer != "yes":
            print("Stopped. Nothing was touched.")
            return

    freed = 0
    failed = 0
    for c in items:
        ok, message = recycle(c["path"])
        if ok:
            freed += c["size"]
        else:
            failed += 1
            print("  skipped %s (%s)" % (c["path"], message))

    print()
    print("Done. %s moved to the Recycle Bin." % human(freed))
    if failed:
        print("%d could not be removed, usually because an app is using them." % failed)
        print("Close that app and run it again.")
    print("Empty the Recycle Bin when you are sure, that is when the space comes back.")


def parse_size(text):
    text = text.strip().upper()
    for suffix, mult in (("TB", 1024 ** 4), ("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if text.endswith(suffix):
            return int(float(text[: -len(suffix)]) * mult)
    return int(float(text))


def main():
    ap = argparse.ArgumentParser(description="Find what is eating your disk.")
    ap.add_argument("--roots", nargs="*", default=DEFAULT_ROOTS, help="folders to look in")
    ap.add_argument("--min", default="50MB", help="ignore anything smaller, e.g. 200MB")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--clean", action="store_true", help="offer to recycle the cache folders")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--budget", type=int, default=180, help="seconds before it gives up")
    args = ap.parse_args()

    min_bytes = parse_size(args.min)
    roots = [expand(r) for r in args.roots]
    deadline = time.time() + args.budget

    t0 = time.time()
    rows = []
    roots = [Path(os.path.realpath(r)) for r in roots if r.exists()]
    root_ids = set()
    for r in roots:
        try:
            st = r.stat()
            root_ids.add((st.st_dev, st.st_ino))
        except OSError:
            pass

    def same_folder(p):
        try:
            st = p.stat()
            return (st.st_dev, st.st_ino) in root_ids
        except OSError:
            return False

    for root in roots:
        # A root that is itself disposable (like %TEMP%) is shown as one line,
        # but cleaned child by child so a single locked file cannot block it.
        own, own_why = classify(root.name)
        if own == SAFE and not protected(root):
            # Protected children are dropped here rather than at clean time, so
            # the report never promises space it will not actually free.
            kids = [
                {"path": str(c), "size": subtree_size(c, deadline)}
                for c in children(root)
                if not protected(c)
            ]
            size = sum(k["size"] for k in kids)
            if size >= min_bytes:
                rows.append(
                    {
                        "path": str(root),
                        "name": root.name,
                        "size": size,
                        "safe": size,
                        "verdict": SAFE,
                        "why": own_why,
                        "partial": False,
                        "caches": kids,
                    }
                )
            continue
        for child in children(root):
            if same_folder(child):
                continue
            row = analyse(child, deadline)
            if row["size"] >= min_bytes:
                rows.append(row)
    rows.sort(key=lambda r: r["size"], reverse=True)
    elapsed = time.time() - t0

    if args.json:
        print(json.dumps({"rows": rows, "seconds": round(elapsed, 1)}, indent=2))
        if args.clean:
            sys.stderr.write("--json only reports. Run without it to clean.\n")
        return

    total = sum(r["size"] for r in rows)
    safe = sum(r["safe"] for r in rows)

    print()
    print("Scanned %d folders in %.1fs.  %s total, %s of it cache." % (len(rows), elapsed, human(total), human(safe)))
    print()
    print("%-9s %-52s %-5s %s" % ("SIZE", "FOLDER", "", "VERDICT"))
    print("-" * 104)
    for r in rows[: args.top]:
        name = r["path"]
        if len(name) > 52:
            name = "..." + name[-49:]
        mark = {SAFE: "-", ASK: "?", KEEP: "!"}[r["verdict"]]
        print("%-9s %-52s %-5s %s" % (human(r["size"]), name, mark, r["verdict"].upper()))
        if r["why"]:
            print("%-9s %-52s %-5s   %s" % ("", "", "", r["why"]))
    print()
    if any(r["partial"] for r in rows):
        print("Some folders were too slow to finish and are undercounted.")
        print()

    if args.clean:
        clean(rows, args.yes)


if __name__ == "__main__":
    main()
