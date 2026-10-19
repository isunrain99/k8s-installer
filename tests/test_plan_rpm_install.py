"""plan-rpm-install.py decides which rpms of the bundle a rhel node is handed.

Getting it wrong either way is what this exists to stop. Handing rpm a
dependency older than the node holds breaks the packages of the node pinned to
it, which is how openssl-devel and device-mapper-event stopped an install on rhel
9.7. Keeping the node's copy of something the release declares leaves a node
running a containerd or a kubelet nobody chose. So what is asserted is the
decision, read through --facts, without rpm: the version comparison is rpm's own
and is not what this module decides.
"""
import subprocess
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
PLANNER = REPO / "scripts" / "all-node" / "linux-package" / "plan-rpm-install.py"


def plan(tmp_path, packages):
    facts = tmp_path / "facts.yml"
    facts.write_text(yaml.safe_dump(packages))
    result = subprocess.run(
        [sys.executable, str(PLANNER), "--facts", str(facts)],
        capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    lines = [line.split("\t") for line in result.stdout.splitlines()]
    return {path: action for action, path, _ in lines}


def pkg(name, srpm, installed=None, principal=False, evr="0:1.0-1.el9"):
    return {
        "file": f"{name}.rpm",
        "name": name,
        "evr": evr,
        "srpm": srpm,
        "principal": principal,
        "installed": installed,
        "installed_evr": None if installed is None else "0:9.9-9.el9",
    }


def test_a_dependency_the_node_holds_newer_is_kept(tmp_path):
    actions = plan(tmp_path, [pkg("device-mapper", "lvm2.src.rpm", installed="newer")])

    assert actions == {"device-mapper.rpm": "skip"}


def test_the_rest_of_its_source_rpm_is_kept_with_it(tmp_path):
    # openssl-libs is newer on the node. openssl requires openssl-libs at its
    # own version, so handing rpm the bundled openssl alone is the same refusal
    actions = plan(tmp_path, [
        pkg("openssl-libs", "openssl.src.rpm", installed="newer"),
        pkg("openssl", "openssl.src.rpm", installed="older"),
        pkg("openssl-devel-not-on-node", "openssl.src.rpm"),
    ])

    assert actions == {
        "openssl-libs.rpm": "skip",
        "openssl.rpm": "skip",
        "openssl-devel-not-on-node.rpm": "skip",
    }


def test_a_dependency_the_node_holds_older_or_not_at_all_goes_in(tmp_path):
    actions = plan(tmp_path, [
        pkg("expat", "expat.src.rpm", installed="older"),
        pkg("fuse3", "fuse3.src.rpm"),
    ])

    assert actions == {"expat.rpm": "install", "fuse3.rpm": "install"}


def test_what_is_installed_at_the_bundled_version_is_left_alone(tmp_path):
    # Reinstalling containerd.io over itself trips its own conflict on
    # containerd, which is why the same version is never handed over
    actions = plan(tmp_path, [pkg("containerd.io", "containerd.io.src.rpm", installed="same", principal=True)])

    assert actions == {"containerd.io.rpm": "skip"}


def test_what_the_release_declares_goes_in_over_a_newer_one(tmp_path):
    actions = plan(tmp_path, [pkg("kubelet", "kubelet.src.rpm", installed="newer", principal=True)])

    assert actions == {"kubelet.rpm": "install"}


def test_a_declared_package_is_not_kept_back_by_its_source_rpm(tmp_path):
    actions = plan(tmp_path, [
        pkg("docker-ce-rootless-extras", "docker-ce.src.rpm", installed="newer"),
        pkg("docker-ce", "docker-ce.src.rpm", installed="older", principal=True),
    ])

    assert actions == {"docker-ce-rootless-extras.rpm": "skip", "docker-ce.rpm": "install"}


def test_one_package_in_two_directories_is_planned_once(tmp_path):
    actions = plan(tmp_path, [pkg("libseccomp", "libseccomp.src.rpm"), pkg("libseccomp", "libseccomp.src.rpm")])

    assert actions == {"libseccomp.rpm": "install"}
