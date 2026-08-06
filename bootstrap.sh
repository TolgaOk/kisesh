#!/bin/sh
# Install KiSesh as an isolated uv tool, then enable its Kitty integration.

set -eu

package_url=${KISESH_PACKAGE_URL:-https://github.com/TolgaOk/kisesh/archive/refs/heads/main.tar.gz}
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

trap cleanup EXIT HUP INT TERM

uv_executable=${KISESH_UV:-}
if [ -z "$uv_executable" ]; then
    uv_executable=$(command -v uv 2>/dev/null || :)
fi
if [ -z "$uv_executable" ]; then
    curl_executable=${KISESH_CURL:-$(command -v curl 2>/dev/null || :)}
    if [ -z "$curl_executable" ] || [ ! -x "$curl_executable" ]; then
        printf '%s\n' 'kisesh bootstrap: curl was not found' >&2
        exit 1
    fi
    temporary=$(mktemp -d "${TMPDIR:-/tmp}/kisesh-bootstrap.XXXXXX")
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
    printf '%s\n' "kisesh bootstrap: uv is not executable: $uv_executable" >&2
    exit 1
fi

UV_TOOL_DIR="$tool_root/environments"
UV_TOOL_BIN_DIR="$tool_root/bin"
export UV_TOOL_DIR UV_TOOL_BIN_DIR

"$uv_executable" tool install \
    --force \
    --python "$python_version" \
    "$package_url"

kisesh_cli="$UV_TOOL_BIN_DIR/kisesh"
if [ ! -x "$kisesh_cli" ]; then
    printf '%s\n' "kisesh bootstrap: installed command is missing: $kisesh_cli" >&2
    exit 1
fi
KISESH_CLI="$kisesh_cli" "$kisesh_cli" install

printf '%s\n' "KiSesh installed. Restart Kitty once, then press Alt+S."
