#!/bin/sh
# Install KiSesh from a checkout or remote source, then enable its Kitty integration.

set -eu

default_package_url=https://github.com/TolgaOk/kisesh/archive/refs/tags/v0.1.1-beta.tar.gz
python_version=${KISESH_PYTHON:-3.11}
uv_installer_url=${KISESH_UV_INSTALLER_URL:-https://astral.sh/uv/0.11.32/install.sh}
data_home=${XDG_DATA_HOME:-"$HOME/.local/share"}
tool_root=${KISESH_TOOL_ROOT:-"$data_home/kisesh-tool"}
temporary=

cleanup() {
    if [ -n "$temporary" ] && [ -d "$temporary" ]; then
        rm -rf -- "$temporary"
    fi
}

checkout_directory() {
    script_path=$0
    case "$script_path" in
        */*) ;;
        *) script_path=$(command -v "$script_path" 2>/dev/null || :) ;;
    esac
    [ -n "$script_path" ] || return 1
    candidate=$(CDPATH= cd -- "$(dirname -- "$script_path")" 2>/dev/null && pwd) || return 1
    [ -f "$candidate/pyproject.toml" ] || return 1
    [ -f "$candidate/kisesh/__init__.py" ] || return 1
    printf '%s\n' "$candidate"
}

trap cleanup EXIT HUP INT TERM

package_source=${KISESH_PACKAGE_URL:-}
editable=no
if [ -z "$package_source" ]; then
    if checkout=$(checkout_directory); then
        package_source=$checkout
        editable=yes
    else
        package_source=$default_package_url
    fi
fi

uv_executable=${KISESH_UV:-}
if [ -z "$uv_executable" ]; then
    uv_executable=$(command -v uv 2>/dev/null || :)
fi
if [ -z "$uv_executable" ]; then
    curl_executable=${KISESH_CURL:-$(command -v curl 2>/dev/null || :)}
    if [ -z "$curl_executable" ] || [ ! -x "$curl_executable" ]; then
        printf '%s\n' 'kisesh installer: curl was not found' >&2
        exit 1
    fi
    temporary=$(mktemp -d "${TMPDIR:-/tmp}/kisesh-install.XXXXXX")
    uv_installer="$temporary/uv-install.sh"
    "$curl_executable" \
        --fail \
        --silent \
        --show-error \
        --location \
        "$uv_installer_url" \
        --output "$uv_installer"
    UV_UNMANAGED_INSTALL="$temporary/uv-bin" sh "$uv_installer"
    uv_executable="$temporary/uv-bin/uv"
fi
if [ ! -x "$uv_executable" ]; then
    printf '%s\n' "kisesh installer: uv is not executable: $uv_executable" >&2
    exit 1
fi

UV_TOOL_DIR="$tool_root/environments"
UV_TOOL_BIN_DIR="$tool_root/bin"
export UV_TOOL_DIR UV_TOOL_BIN_DIR

if [ "$editable" = yes ]; then
    "$uv_executable" tool install \
        --force \
        --editable \
        --python "$python_version" \
        "$package_source"
else
    "$uv_executable" tool install \
        --force \
        --python "$python_version" \
        "$package_source"
fi

kisesh_cli="$UV_TOOL_BIN_DIR/kisesh"
if [ ! -x "$kisesh_cli" ]; then
    printf '%s\n' "kisesh installer: installed command is missing: $kisesh_cli" >&2
    exit 1
fi
KISESH_CLI="$kisesh_cli" "$kisesh_cli" install "$@"

printf '%s\n' "KiSesh installed. Kitty was left running; reload its configuration when convenient."
