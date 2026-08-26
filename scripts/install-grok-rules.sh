#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source_file="${script_dir}/../templates/grok/AGENTS.md"
grok_config_dir="${GROK_CONFIG_DIR:-${HOME}/.grok}"
target_file="${grok_config_dir}/AGENTS.md"

mkdir -p -- "${grok_config_dir}"
install -m 0644 -- "${source_file}" "${target_file}"
cmp --silent -- "${source_file}" "${target_file}"
printf '%s\n' "installed ${target_file} from ${source_file}"
