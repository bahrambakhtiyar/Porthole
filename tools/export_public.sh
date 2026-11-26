#!/usr/bin/env bash
# Make the public repository's first commit from this repository's HEAD, with none of its history:
#   bash tools/export_public.sh <new or empty folder>
#
# It takes the files git tracks on HEAD (git archive: nothing untracked or ignored comes along) and leaves out the
# project's private notes, which NOTES in tests/private_docs.py lists. It refuses, and writes nothing, when what is
# left still names one of those notes (outside that list, in any case), has a file under private/, a symbolic link
# (git would publish its target as it is; the project has none), a machine path (a mount point, a temporary session
# folder, a Windows path into a Desktop, OneDrive or Claude folder, a home folder other than the tests' made-up ones
# and GitHub's CI machine, with / or \ like C:\Users\<name>) or an email address other than the GitHub no-reply ones.
# These checks read every file, binary ones too (a PNG's text or a GIF's comment can hold a path), and ignore case.
# Otherwise it makes a new git repository in the folder, on branch main, with one commit under the no-reply address,
# and prints the commands that publish it. It never adds a remote and never pushes.
set -euo pipefail
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES
unset -f git grep find tar 2>/dev/null || true  # the programs themselves, not shell functions of the same name

AUTHOR_NAME="Bahram Bakhtiyar"
AUTHOR_EMAIL="64256223+bahrambakhtiyar@users.noreply.github.com"
MESSAGE="Porthole 0.1.0: share your screen on your Wi-Fi"
PUBLIC_URL="https://github.com/bahrambakhtiyar/Porthole.git"
LIST="tests/private_docs.py"
# The home folders the tests make up (/home/ayla, /Users/ayla, C:\Users\Ayla and so on), and GitHub's CI machine
# (runner). Any other one is somebody's real folder.
MADE_UP_HOMES="ayla|me|someone|runner"
# What the content checks look for, read byte by byte (LC_ALL=C) and ignoring case. A folder name is ASCII letters,
# digits and ._- or the bytes of any other letter; any other ASCII character ends it ("C:\Users\Ayla's PC" is Ayla).
NAME='([a-z0-9._-]|[^[:print:][:cntrl:]])+'
SEP='(\\{1,2}|/)'  # / or \ (\\ where a string escapes it)
HOME_FOLDER='[\/](home|users)'"$SEP$NAME"
PC_PATH='/m[n]t/[[:graph:]]*|/t[m]p/claude[[:graph:]]*'
PC_PATH+="|[a-z]:$SEP([^\\/[:space:][:cntrl:]\"']+$SEP)*(desktop|onedrive|claude)[^[:space:][:cntrl:]\"']*"
EMAIL='[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}'
# The addresses that may be public: the GitHub no-reply ones of the commits and the packages.
ALLOWED_EMAILS='(64256223\+)?bahrambakhtiyar@users\.noreply\.github\.com'
# Third-party licence texts, copied as they are: they keep their authors' addresses.
THIRD_PARTY="packaging/appimage/THIRD-PARTY-NOTICES.txt"

die() {
  echo "export_public: $*" >&2
  exit 1
}

[[ $# -eq 1 && -n "$1" && "$1" != -* ]] || die "usage: bash tools/export_public.sh <new or empty folder>"
target="$1"
command -v git >/dev/null || die "git isn't installed"
command -v python3 >/dev/null || die "python3 isn't installed"

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
root="$(git -C "$here" rev-parse --show-toplevel 2>/dev/null)" || die "$here isn't in a git repository"
commit="$(git -C "$root" rev-parse --verify --quiet 'HEAD^{commit}')" || die "the repository has no commit yet"
short="$(git -C "$root" rev-parse --short "$commit")"

# The target: new or empty, and outside this repository.
realpath_of() { python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$1"; }
target_abs="$(realpath_of "$target")"
root_abs="$(realpath_of "$root")"
case "$target_abs/" in
  "$root_abs/"*) die "$target is inside the repository: give a folder outside it" ;;
esac
if [[ -e "$target" || -L "$target" ]]; then
  [[ -d "$target" && ! -L "$target" ]] || die "$target exists and isn't a folder"
  [[ -z "$(ls -A "$target")" ]] || die "$target isn't empty: give a new or empty folder"
fi

if [[ -n "$(git -C "$root" --no-optional-locks status --porcelain --untracked-files=no)" ]]; then
  echo "Note: the copy is HEAD ($short); the uncommitted changes in $root aren't in it." >&2
fi

# The private notes, read from HEAD's copy of the list without running it.
names="$(git -C "$root" show "$commit:$LIST" 2>/dev/null | python3 -c '
import ast, sys
for node in ast.parse(sys.stdin.read()).body:
    if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "NOTES" for t in node.targets):
        try:
            names = ast.literal_eval(node.value)
        except ValueError:
            break
        if isinstance(names, tuple) and names and all(isinstance(n, str) for n in names):
            print("\n".join(names))
            sys.exit(0)
sys.exit(1)
')" || die "HEAD has no list of the private notes (NOTES in $LIST)"
notes=()
while IFS= read -r name; do
  [[ "$name" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || die "$LIST names an odd file: $name"
  notes+=("$name")
done <<<"$names"

stage="$(mktemp -d "${TMPDIR:-/tmp}/porthole-export.XXXXXX")"
trap 'rm -rf "$stage"' EXIT
git -C "$root" archive --format=tar "$commit" | tar -x -C "$stage"
cd "$stage"

left_out=()
for name in "${notes[@]}"; do
  if [[ -e "docs/$name" ]]; then
    rm -f "docs/$name"
    left_out+=("docs/$name")
  fi
done

# The checks. Each problem is "file:line: what was found (why)".
# Every match of a pattern in any file, binary ones too, byte by byte and ignoring case: "./file:line:match".
matches() { LC_ALL=C grep -rnoai "$@" . | LC_ALL=C sort || true; }
problems=()
while IFS= read -r link; do
  problems+=("${link#./}: a symbolic link (git would publish its target as it is; the project has none)")
done < <(find . -type l | LC_ALL=C sort)
if [[ -e private || -L private ]]; then
  while IFS= read -r file; do
    problems+=("${file#./}: a file under private/, where the private notes live")
  done < <(find ./private \( -type f -o -type l \) | LC_ALL=C sort)
fi
patterns=()
for name in "${notes[@]}"; do
  patterns+=(-e "$name")
  stem="${name%.md}"
  if [[ "$stem" == *-* ]]; then patterns+=(-e "$stem"); fi  # "plan" alone is just a word; a stem with a dash isn't
  while IFS= read -r file; do
    problems+=("${file#./}: a copy of the private note $name")
  done < <(find . -iname "$name" \( -type f -o -type l \) | LC_ALL=C sort)
done
while IFS= read -r hit; do
  [[ "${hit%%:*}" == "./$LIST" ]] && continue
  problems+=("${hit#./} (names a private note)")
done < <(matches -F "${patterns[@]}")
while IFS= read -r hit; do
  problems+=("${hit#./} (a home folder)")
done < <(matches -E "$HOME_FOLDER" | LC_ALL=C grep -viE "^[^:]*:[0-9]+:[\\/](home|users)$SEP($MADE_UP_HOMES)\$" || true)
while IFS= read -r hit; do
  problems+=("${hit#./} (a path on this PC)")
done < <(matches -E "$PC_PATH")
allowed="^${ALLOWED_EMAILS}\$"
while IFS= read -r hit; do
  [[ "${hit%%:*}" == "./$THIRD_PARTY" ]] && continue
  [[ "${hit##*:}" =~ $allowed ]] && continue
  problems+=("${hit#./} (an email address)")
done < <(matches -E "$EMAIL")

if ((${#problems[@]})); then
  {
    echo "export_public: refused, and nothing was written to $target. HEAD ($short) still has:"
    printf '  %s\n' "${problems[@]}"
    echo "Fix them in a commit and run it again."
  } >&2
  exit 1
fi

files="$(find . \( -type f -o -type l \) | wc -l | tr -d ' ')"
mkdir -p "$target_abs"
cp -a "$stage/." "$target_abs/"
cd "$target_abs"
git init -q -b main
git config user.name "$AUTHOR_NAME"
git config user.email "$AUTHOR_EMAIL"
git add -A -f .
GIT_AUTHOR_NAME="$AUTHOR_NAME" GIT_AUTHOR_EMAIL="$AUTHOR_EMAIL" \
  GIT_COMMITTER_NAME="$AUTHOR_NAME" GIT_COMMITTER_EMAIL="$AUTHOR_EMAIL" \
  git commit -q --no-verify -m "$MESSAGE"

# Check what was made: one commit, by the no-reply address, with every file, and no remote.
[[ "$(git rev-list --count HEAD)" == 1 ]] || die "$target has more than one commit"
who="$AUTHOR_NAME <$AUTHOR_EMAIL>"
[[ "$(git log -1 --format='%an <%ae>|%cn <%ce>')" == "$who|$who" ]] || die "the commit in $target isn't by $who"
[[ "$(git ls-files | wc -l | tr -d ' ')" == "$files" ]] || die "the commit in $target doesn't have all $files files"
[[ -z "$(git status --porcelain)" ]] || die "$target has files the commit doesn't"
[[ -z "$(git remote)" ]] || die "$target has a remote"

echo "Made $target_abs: one commit, $(git rev-parse --short HEAD), on main, with $files files, by $who."
echo "From HEAD $short of $root_abs. Left out: ${left_out[*]:-nothing}."
echo
echo "Nothing was pushed. Look through it, then publish it yourself:"
printf '  cd %q\n' "$target_abs"
echo "  git log --stat"
echo "  git remote add origin $PUBLIC_URL"
echo "  git push -u origin main"
