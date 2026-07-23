<table border="0">
  <tr>
    <td>
      <!-- VERSION -->v1.00.00<br>
      <!-- DATE -->23-Jul-2026<br>
      macOS &nbsp;|&nbsp; Windows &nbsp;|&nbsp; Linux<br>
      <a href="https://landenlabs.com">Home</a>
    </td>
    <td>
      <a href="https://landenlabs.com">
        <img src="screens/landenlabs_400.webp" width="300" alt="LanDen Labs">
      </a>
    </td>
  </tr>
</table>

<img src="icon.png" width="72" align="left" alt="git-compare-ui icon">

# git-compare-ui

A PyQt6 GUI for reviewing per-file diffs between the current working tree and
a compare-to branch before launching each one in Android Studio's diff viewer
(or any configured tool). Replaces piping `git difftool -x studioDiff <branch>`
straight to the terminal, which gives no visibility into what's about to run
and no way to skip files that can't be compared (pure adds/deletes).

## Usage

```
python git-compare.py <compare-branch> [--repo PATH]
```

If `<compare-branch>` is omitted, the branch picker opens first so you can
choose one.

## Comparison Table

This is the main window — a table of every file that differs from the
selected branch:

| Column | Meaning |
|---|---|
| checkbox | included when you click "Run Checked" |
| File | path (new path, for renames) |
| Status | Modified / Added / Deleted / Renamed / Copied / Type changed |
| + / - | added/deleted line counts from `git diff --numstat` |
| Result | blank until run, then a launch timestamp or error |

Files that only exist on one side (Added or Deleted relative to the branch)
can't be diffed — their checkbox is disabled and the row is greyed out.

- **Double-click** a row to launch (or re-launch) that file's diff.
- **Run Checked** launches every checked, comparable row.
- Rows turn green after a successful launch, red on error.
- **Expand/Collapse** toggles between showing every row and only checked ones.
- **Filter** (toolbar dropdown) narrows the table to specific file extensions
  — check any of `.java` / `.xml` / `.png` / `.gradle`, "Add extension…" for
  anything else, or "All" to clear the filter.
- The theme button (half-light/half-dark icon) switches the whole app between
  light and dark, persisted between runs.

## Branch Picker

Opens automatically when no branch is given, or any time you want to switch
comparison branches. It lists every local branch with:

| Column | Meaning |
|---|---|
| Branch | click the row to compare the working tree against it |
| Last Commit | tip commit date (or earliest reflog entry as a fallback) |
| Files Changed | file count vs. the working tree, from `git diff --name-only` |
| Changed Since Fork | click to load a diff of this branch against the commit it forked from, into the comparison table |
| Delete Local | deletes the local branch (`git branch -D`), disabled for the checked-out branch |
| Delete Remote | deletes the tracking branch (`git push <remote> --delete`), disabled if none exists |

Both delete actions show a confirmation dialog stating the exact `git`
command that will run before doing anything.

"Changed Since Fork" finds the fork point with
`git merge-base --fork-point <current-branch> <branch>` (falling back to a
plain `git merge-base` if the reflog doesn't cover it), then diffs that
commit against the branch tip — so it shows what the branch itself
introduced, independent of your working tree state.

The comparison table window and the branch picker are independent windows;
closing either one leaves the other running.

## Settings

"⚙" configures how a diff is launched:

- **Direct** (default) — extracts the branch's copy of the file to a temp
  file via `git show <branch>:<path>` and calls
  `"<studio binary>" diff <branch-copy> <working-tree-file>` directly.
- **git difftool -x \<command\>** — shells out to
  `git difftool -y -x "<command>" <branch> -- <path>` per file instead,
  matching the original `studioDiff` wrapper-script workflow.

Both the Studio binary path and the difftool `-x` command are editable and
persisted (via `QSettings`, org `LanDenLabs` / app `git-compare-ui`).

## Requirements

```
pip install -r requirements.txt
```

## Building a standalone app

```
./build.sh
```

Runs PyInstaller against `git-compare.spec`, producing `dist/git-compare.app`
(macOS) with `icon.icns` set as both the Dock icon and bundle icon, and the
version/copyright baked into `Info.plist`. Tagged pushes (`vX.Y.Z`) also
build and publish macOS/Windows artifacts automatically — see
`.github/workflows/build.yml`.

If `icon.png` changes, regenerate `icon.icns`/`icon.ico` with:

```
python3 make-icons.py
```

## License

[Apache License 2.0](LICENSE)
