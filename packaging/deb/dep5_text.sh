#!/usr/bin/env bash
# Print a text file as the body of a field in a Debian copyright file (DEP-5, deb822 syntax): every line indented
# by one space, and empty lines (or lines of only spaces and tabs, which would end the paragraph) as " .".
# build.sh uses it to put the full text of LICENSE in /usr/share/doc/porthole/copyright, so the two never drift.
#
#   bash packaging/deb/dep5_text.sh LICENSE
set -euo pipefail

if [[ $# -ne 1 || ! -f "$1" ]]; then
  echo "Usage: dep5_text.sh FILE (a text file to print as DEP-5 field text)" >&2
  exit 1
fi

# " ." means an empty line, so a line that starts with a full stop can't be written as it is.
first="$(LC_ALL=C grep -n -m 1 '^\.' "$1" | cut -d: -f1 || true)"
if [[ -n "$first" ]]; then
  echo "$1, line $first starts with a full stop, which DEP-5 would read as an empty line." >&2
  exit 1
fi

LC_ALL=C sed -e 's/^[[:space:]]*$/./' -e 's/^/ /' "$1"
