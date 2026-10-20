# shellcheck shell=bash
#
# Sourced by install-packages.sh and by setup-venv.sh, after the cli template of
# each, which is where msg and die come from. The ubuntu counterpart of
# rpm-common.sh, and it decides the same way plan-rpm-install.py does:
#
#   - a package the release declares, or one of kubernetes, goes in at its
#     version whatever the node holds, downgrade and all
#   - any other deb is a dependency, resolved against the GA image of the
#     ubuntu. A node of a later point release holds most of them newer, and the
#     node keeps those, together with every deb built from the same source
#     package, since those depend on each other at one exact version
#     (libssl-dev on libssl3, for one)
#   - what is installed at the version the bundle holds is not handed over
#
# Written in bash rather than reusing the python planner: an ubuntu node is not
# promised a python until setup-venv.sh has laid one down from these very debs.

# Package names the release declares. A deb under one of deb_principal_dirs is
# declared as well, which is how the packages of kubernetes are
deb_principals=()
deb_principal_dirs=()

# Names planned as if not installed, because the install removes them first
deb_assume_absent=()

# How many debs the last deb_install_files handed to dpkg
deb_installed_count=0

deb_is_principal() {
  local name=$1
  local path=$2

  local principal
  for principal in "${deb_principals[@]}"; do
    [[ $principal = "$name" ]] && return 0
  done

  local dir
  local real
  real=$(realpath "$path")
  for dir in "${deb_principal_dirs[@]}"; do
    [[ $real = "$(realpath "$dir")"/* ]] && return 0
  done

  return 1
}

deb_is_assumed_absent() {
  local name=$1

  local absent
  for absent in "${deb_assume_absent[@]}"; do
    [[ $absent = "$name" ]] && return 0
  done

  return 1
}

# The version of a package the node has installed, or nothing. A package that
# was removed and kept its configuration is not installed
deb_installed_version() {
  local name=$1
  local arch=$2

  local query="$name"
  [[ $arch != "all" ]] && query="$name:$arch"

  local status
  local version
  IFS=$'\t' read -r status version < <(dpkg-query -W -f='${db:Status-Abbrev}\t${Version}\n' "$query" 2>/dev/null || true)
  [[ $status = ii* ]] && echo "$version"

  return 0
}

# One line per deb, tab separated: "install" or "skip", the path, and why.
# Every deb is read first, so that a source package kept for one of its debs is
# kept for all of them whichever order they come in
deb_plan() {
  local paths=()
  local names=()
  local versions=()
  local sources=()
  local principals=()
  local states=()
  local installed_versions=()
  local -A kept_sources=()
  local -A seen=()

  local path
  local fields
  local name
  local version
  local arch
  local source
  local installed
  local state
  for path in "$@"; do
    fields=$(dpkg-deb -W --showformat='${Package}\t${Version}\t${Architecture}\t${source:Package}\n' "$path") ||
      die "[ERROR] Failed to read deb[\"$path\"]"
    IFS=$'\t' read -r name version arch source <<< "$fields"
    [[ -z $source ]] && source=$name

    [[ -n ${seen["$name|$version|$arch"]+x} ]] && continue
    seen["$name|$version|$arch"]=1

    installed=""
    deb_is_assumed_absent "$name" || installed=$(deb_installed_version "$name" "$arch")

    if [[ -z $installed ]]; then
      state="absent"
    elif dpkg --compare-versions "$installed" eq "$version"; then
      state="same"
    elif dpkg --compare-versions "$installed" gt "$version"; then
      state="newer"
    else
      state="older"
    fi

    paths+=("$path")
    names+=("$name")
    versions+=("$version")
    sources+=("$source")
    states+=("$state")
    installed_versions+=("$installed")
    if deb_is_principal "$name" "$path"; then
      principals+=("true")
    else
      principals+=("false")
      if [[ $state = "newer" && -z ${kept_sources["$source"]+x} ]]; then
        kept_sources["$source"]="the node holds $name $installed, newer than the bundle"
      fi
    fi
  done

  # The other way a source is kept: the bundle would upgrade one of its debs
  # while the node holds another one of the same source that the bundle does
  # not carry, which depends on the old version exactly and which nothing
  # would move along
  local -A in_bundle=()
  local i
  for i in "${!paths[@]}"; do
    in_bundle["${sources[$i]}|${names[$i]}"]=1
  done

  local -A node_by_source=()
  local node_source
  local node_name
  local node_status
  while IFS=$'\t' read -r node_source node_name node_status; do
    [[ $node_status = ii* ]] || continue
    [[ -z $node_source ]] && node_source=$node_name
    deb_is_assumed_absent "$node_name" && continue
    node_by_source["$node_source"]+=" $node_name"
  done < <(dpkg-query -W -f='${source:Package}\t${Package}\t${db:Status-Abbrev}\n' 2>/dev/null || true)

  local stranded
  for i in "${!paths[@]}"; do
    [[ ${principals[$i]} = "true" || ${states[$i]} != "older" ]] && continue
    [[ -n ${kept_sources["${sources[$i]}"]+x} ]] && continue
    stranded=""
    for node_name in ${node_by_source["${sources[$i]}"]-}; do
      [[ -z ${in_bundle["${sources[$i]}|$node_name"]+x} ]] && stranded+=" $node_name"
    done
    if [[ -n $stranded ]]; then
      kept_sources["${sources[$i]}"]="upgrading ${names[$i]} past ${installed_versions[$i]} would leave${stranded} of the node, which the bundle does not carry, pinned to the old version"
    fi
  done

  for i in "${!paths[@]}"; do
    name=${names[$i]}
    version=${versions[$i]}
    if [[ ${states[$i]} = "same" ]]; then
      printf 'skip\t%s\t%s\n' "${paths[$i]}" "$name $version is installed"
    elif [[ ${principals[$i]} = "true" ]]; then
      printf 'install\t%s\t%s\n' "${paths[$i]}" "declared by the release"
    elif [[ ${states[$i]} = "newer" ]]; then
      printf 'skip\t%s\t%s\n' "${paths[$i]}" "the node holds $name ${installed_versions[$i]}, newer than $version"
    elif [[ -n ${kept_sources["${sources[$i]}"]+x} ]]; then
      printf 'skip\t%s\t%s\n' "${paths[$i]}" "source ${sources[$i]} is kept: ${kept_sources["${sources[$i]}"]}"
    else
      if [[ ${states[$i]} = "absent" ]]; then
        printf 'install\t%s\t%s\n' "${paths[$i]}" "not installed"
      else
        printf 'install\t%s\t%s\n' "${paths[$i]}" "upgrades ${installed_versions[$i]}"
      fi
    fi
  done

  return 0
}

# Every deb under a directory, the way dpkg -R reads it
deb_files_of() {
  local dir=$1

  [[ -d $dir ]] || return 0
  find "$dir" -type f -name '*.deb' | sort
}

# Installs what the plan says goes in.
#
#   deb_install_files <label> [dpkg options] -- <deb files>
deb_install_files() {
  local label=$1
  shift

  local dpkg_opts=()
  while [[ $# -gt 0 && $1 != "--" ]]; do
    dpkg_opts+=("$1")
    shift
  done
  shift

  deb_installed_count=0
  [[ $# -eq 0 ]] && return 0

  local plan
  plan=$(deb_plan "$@")

  local to_install=()
  local action
  local path
  local reason
  while IFS=$'\t' read -r action path reason; do
    [[ -z $action ]] && continue
    if [[ $action = "install" ]]; then
      to_install+=("$path")
    elif [[ $reason != *" is installed" ]]; then
      msg "[INFO] Keeping what the node holds instead of $(basename "$path"): $reason"
    fi
  done <<< "$plan"

  deb_installed_count=${#to_install[@]}
  if [[ $deb_installed_count = 0 ]]; then
    msg "[INFO] Nothing of [\"$label\"] to install"
    return 0
  fi

  dpkg -i "${dpkg_opts[@]}" "${to_install[@]}"

  return 0
}

# Plans every directory of an install at once, as the node will be once the
# names given are removed, and has apt simulate it without changing anything.
#
#   deb_require_installable <removed name>... -- <deb files>
#
# apt rather than dpkg, because apt is what says what a transaction would take
# away: handed debs that break a package of the node, it plans to remove that
# package, where dpkg would stop half way. A removal the install did not ask
# for, or a dependency nothing here satisfies, refuses the node while it is
# still untouched
deb_require_installable() {
  local removed=()
  while [[ $# -gt 0 && $1 != "--" ]]; do
    removed+=("$1")
    shift
  done
  shift

  [[ $# -eq 0 ]] && return 0

  local saved_absent=("${deb_assume_absent[@]}")
  deb_assume_absent=("${removed[@]}")
  local plan
  plan=$(deb_plan "$@")
  deb_assume_absent=("${saved_absent[@]}")

  local to_install=()
  local action
  local path
  local reason
  while IFS=$'\t' read -r action path reason; do
    [[ $action = "install" ]] && to_install+=("$(realpath "$path")")
  done <<< "$plan"

  [[ ${#to_install[@]} = 0 ]] && return 0

  # Only what is installed is named for removal. apt fails on a name it can not
  # find, and an air gapped node has no package lists to find the others in
  local removals=()
  local name
  for name in "${removed[@]}"; do
    if [[ $(dpkg-query -W -f='${db:Status-Abbrev}' "$name" 2>/dev/null || true) = ii* ]]; then
      removals+=("$name-")
    fi
  done

  local output
  output=$(apt-get -s -q --allow-downgrades --allow-change-held-packages \
    -o Debug::NoLocking=1 install "${to_install[@]}" "${removals[@]}" 2>&1) ||
    die "[ERROR] apt refuses the packages of the bundle on this node. Nothing was changed:\n$output"

  local removed_pattern
  removed_pattern=$(IFS='|'; echo "${removed[*]}")
  local unexpected
  unexpected=$(grep -E '^Remv ' <<< "$output" | grep -Ev "^Remv (${removed_pattern})[ :]" || true)
  [[ -z $unexpected ]] && return 0

  die "[ERROR] Installing the bundle would remove packages of this node. Nothing was changed:\n$unexpected\nThose depend on a version of something the bundle replaces. Remove them if the node does not need them"
}
