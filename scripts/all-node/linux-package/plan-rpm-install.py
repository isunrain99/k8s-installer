#!/usr/bin/env python3
"""Decides which rpms of the bundle go onto a node and which the node keeps.

The bundle carries two kinds of package. The ones release.yml declares, and the
packages of kubernetes, are the ones the installer chose, and a node runs
exactly that version: they go in whatever the node holds, downgrade and all.
Everything else is the dependency closure of those, resolved against the oldest
minor the release supports, and nobody picked its versions. A node of a newer
minor already holds most of it at a newer version, and handing rpm the older
one is a downgrade nothing asked for. It also fails: a package of the node that
pins the exact version of what it is built with - openssl-devel on
openssl-libs, device-mapper-event on device-mapper - refuses the transaction, or
worse, one that does not pin it is left running on an older library than it was
built against.

So a dependency the node holds at a newer version stays as it is. Not package by
package but by the source rpm it was built from: the packages of one source rpm
require each other at exactly one version, and keeping openssl-libs of the node
while handing rpm the openssl of the bundle is the same refusal over again. A
package of such a source rpm that the node does not hold at all is left out with
the rest, and if something needs it, the rpm --test that follows says so before
the node is touched.

A newer library satisfies what was built against an older one of the same major
version: the soname is the same and every older symbol version is still there,
and rpm checks both, since what a binary links against is written into its
requirements. What rpm can not see is a change of behaviour, which is a risk the
node already runs with for everything else it holds.

Usage:
  plan-rpm-install.py [--principal NAME]... [--principal-dir DIR]...
                      [--assume-absent NAME]... RPM...
  plan-rpm-install.py --facts FILE

Prints one line per rpm, tab separated: "install" or "skip", the path, and why.

--assume-absent treats a name as not installed. The installer erases chrony,
podman and runc before it installs, so a plan made before that, to be checked
with rpm --test, has to see the node the way the install will.

--facts reads the packages and what the node holds of them from a yaml file
instead of from rpm, which is how the decision is tested where there is no rpm.
"""
import argparse
import os
import sys

INSTALL = "install"
SKIP = "skip"

SAME = "same"
NEWER = "newer"
OLDER = "older"


def decide(packages):
    """packages: dicts with file, name, evr, srpm, principal, and installed
    (None, SAME, NEWER or OLDER) with installed_evr. Returns (action, file,
    reason) in the order given, one per distinct file."""
    kept_srpms = {}
    for p in packages:
        if not p["principal"] and p["installed"] == NEWER:
            kept_srpms.setdefault(p["srpm"], p)

    plan = []
    seen = set()
    for p in packages:
        key = (p["name"], p["evr"], p.get("arch"))
        if key in seen:
            continue
        seen.add(key)

        if p["installed"] == SAME:
            plan.append((SKIP, p["file"], f"{p['name']} {p['evr']} is installed"))
        elif p["principal"]:
            reason = "declared by the release"
            if p["installed"] in (NEWER, OLDER):
                reason += f", replaces {p['installed_evr']}"
            plan.append((INSTALL, p["file"], reason))
        elif p["installed"] == NEWER:
            plan.append((SKIP, p["file"],
                         f"the node holds {p['name']} {p['installed_evr']}, newer than {p['evr']}"))
        elif p["srpm"] in kept_srpms:
            holder = kept_srpms[p["srpm"]]
            plan.append((SKIP, p["file"],
                         f"built from {p['srpm']}, of which the node holds {holder['name']} "
                         f"{holder['installed_evr']}, newer than the bundle"))
        else:
            reason = "not installed" if p["installed"] is None else f"upgrades {p['installed_evr']}"
            plan.append((INSTALL, p["file"], reason))
    return plan


def text(value):
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


def evr_tuple(header, rpm):
    epoch = header[rpm.RPMTAG_EPOCHNUM]
    return (str(epoch), text(header[rpm.RPMTAG_VERSION]), text(header[rpm.RPMTAG_RELEASE]))


def evr_string(evr):
    return f"{evr[0]}:{evr[1]}-{evr[2]}"


def packages_from_rpm(args):
    import rpm

    ts = rpm.TransactionSet()
    # The bundle is checked as a whole against release.yml before it is
    # unpacked, and the keys of the repositories it was filled from are not on
    # the node, so a header is read without asking for its signature
    ts.setVSFlags(getattr(rpm, "_RPMVSF_NOSIGNATURES", 0) | getattr(rpm, "_RPMVSF_NODIGESTS", 0))

    principal_dirs = [os.path.realpath(d) for d in args.principal_dir]
    absent = set(args.assume_absent)
    packages = []
    for path in args.rpms:
        with open(path, "rb") as f:
            header = ts.hdrFromFdno(f.fileno())
        name = text(header[rpm.RPMTAG_NAME])
        arch = text(header[rpm.RPMTAG_ARCH])
        evr = evr_tuple(header, rpm)
        real = os.path.realpath(path)
        principal = name in args.principal or any(
            os.path.commonpath([real, d]) == d for d in principal_dirs)

        installed = None
        installed_evr = None
        if name not in absent:
            held = [h for h in ts.dbMatch("name", name)]
            same_arch = [h for h in held if text(h[rpm.RPMTAG_ARCH]) in (arch, "noarch") or arch == "noarch"]
            held = same_arch or held
            if held:
                evrs = [evr_tuple(h, rpm) for h in held]
                if any(rpm.labelCompare(e, evr) == 0 for e in evrs):
                    installed = SAME
                    installed_evr = evr
                else:
                    newest = evrs[0]
                    for e in evrs[1:]:
                        if rpm.labelCompare(e, newest) > 0:
                            newest = e
                    installed = NEWER if rpm.labelCompare(newest, evr) > 0 else OLDER
                    installed_evr = newest

        packages.append({
            "file": path,
            "name": name,
            "arch": arch,
            "evr": evr_string(evr),
            "srpm": text(header[rpm.RPMTAG_SOURCERPM]) or name,
            "principal": principal,
            "installed": installed,
            "installed_evr": evr_string(installed_evr) if installed_evr else None,
        })
    return packages


def packages_from_facts(path):
    import yaml

    with open(path) as f:
        facts = yaml.safe_load(f) or []
    for p in facts:
        p.setdefault("principal", False)
        p.setdefault("installed", None)
        p.setdefault("installed_evr", None)
        p.setdefault("arch", "x86_64")
    return facts


def main():
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--principal", action="append", default=[])
    parser.add_argument("--principal-dir", action="append", default=[])
    parser.add_argument("--assume-absent", action="append", default=[])
    parser.add_argument("--facts")
    parser.add_argument("rpms", nargs="*")
    args = parser.parse_args()

    if args.facts:
        packages = packages_from_facts(args.facts)
    else:
        packages = packages_from_rpm(args)

    for action, path, reason in decide(packages):
        print(f"{action}\t{path}\t{reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
