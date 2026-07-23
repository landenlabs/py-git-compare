#!/usr/bin/env python3
# ----------------------------------------------------------------------
# Copyright (c) 2026 LanDen Labs - Dennis Lang
# https://landenlabs.com
# ----------------------------------------------------------------------
"""git-compare-ui — table-based UI for reviewing per-file diffs between the
current working tree and a compare-to branch, and launching each file's diff
in Android Studio (or any configured tool) individually.

Usage:
    python git_compare_ui.py <compare-branch> [--repo PATH]
"""

import argparse
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTableWidget, QTableWidgetItem, QCheckBox, QPushButton, QComboBox,
    QLabel, QLineEdit, QFileDialog, QHeaderView, QAbstractItemView,
    QDialog, QFormLayout, QDialogButtonBox, QRadioButton, QMessageBox,
    QStatusBar, QToolButton, QMenu, QInputDialog, QStyle,
)
from PyQt6.QtCore import Qt, QSettings, QProcess, QSize, pyqtSignal
from PyQt6.QtGui import QColor, QIcon, QPalette, QAction

from version import __version__

ORG = "LanDenLabs"
APP = "git-compare-ui"

DEFAULT_STUDIO = '/Applications/Android Studio.app/Contents/MacOS/studio'
DEFAULT_DIFFTOOL_CMD = f"'{DEFAULT_STUDIO}' diff"


def resource_path(name):
    """Locate a bundled resource (e.g. icon.png) both when run from source
    and when frozen by PyInstaller, which unpacks --add-data into _MEIPASS."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / name


def app_icon():
    """QIcon for icon.png, or a null QIcon if the resource is missing."""
    path = resource_path("icon.png")
    return QIcon(str(path)) if path.is_file() else QIcon()


def _build_date():
    """Release/build date, derived from version.py's mtime -- set-version.bash
    rewrites version.py on every release, so this tracks the last publish."""
    target = Path(__file__).resolve().parent / "version.py"
    try:
        return datetime.fromtimestamp(target.stat().st_mtime).strftime("%Y-%m-%d")
    except OSError:
        return "unknown"


THEME_ICON_PATH = resource_path("darklight.png")

STATUS_NAMES = {
    'A': 'Added', 'D': 'Deleted', 'M': 'Modified',
    'R': 'Renamed', 'C': 'Copied', 'T': 'Type changed', 'U': 'Unmerged',
}

COL_CHECK, COL_FILE, COL_STATUS, COL_ADD, COL_DEL, COL_RESULT = range(6)
HEADERS = ["", "File", "Status", "+", "-", "Result"]

TOOLBAR_BTN_HEIGHT = 26
DEFAULT_FILTER_EXTENSIONS = [".java", ".xml", ".png", ".gradle"]

LIGHT_ROW_COLORS = {
    'skip': "#e6e6e6",
    'default': "#ffffff",
    'ran': "#c8f0c8",
    'error': "#f7c8c8",
}
DARK_ROW_COLORS = {
    'skip': "#3a3a3a",
    'default': "#2b2b2b",
    'ran': "#1f3d1f",
    'error': "#4a1f1f",
}

# Default style/palette, captured once at startup so light mode can restore
# whatever the platform normally looks like (rather than a hardcoded guess).
_DEFAULT_STYLE_NAME = None
_DEFAULT_PALETTE = None


def build_dark_palette():
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(53, 53, 53))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(220, 220, 220))
    palette.setColor(QPalette.ColorRole.Base, QColor(35, 35, 35))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(53, 53, 53))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(220, 220, 220))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(220, 220, 220))
    palette.setColor(QPalette.ColorRole.Text, QColor(220, 220, 220))
    palette.setColor(QPalette.ColorRole.Button, QColor(53, 53, 53))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(220, 220, 220))
    palette.setColor(QPalette.ColorRole.BrightText, QColor(255, 60, 60))
    palette.setColor(QPalette.ColorRole.Link, QColor(90, 160, 240))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(42, 130, 218))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(0, 0, 0))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(120, 120, 120))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(120, 120, 120))
    return palette


@dataclass
class FileRow:
    status: str
    path: str
    old_path: str = None
    added: str = "0"
    deleted: str = "0"
    comparable: bool = True
    reason: str = ""


def run_git(repo, args):
    result = subprocess.run(['git', '-C', str(repo)] + args, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout


def format_git_cmd(repo, args):
    return "git -C '" + str(repo) + "' " + " ".join(args)


def list_branches(repo):
    out = run_git(repo, ['branch', '--format=%(refname:short)'])
    return [b.strip() for b in out.splitlines() if b.strip()]


def current_branch(repo):
    try:
        return run_git(repo, ['rev-parse', '--abbrev-ref', 'HEAD']).strip()
    except RuntimeError:
        return None


def fork_point(repo, branch, base):
    """Commit `branch` diverged from `base` at.

    `git merge-base --fork-point <base> <branch>` walks `base`'s reflog to find
    where `branch` split off, which stays correct even if `base` has since been
    rebased/fast-forwarded past that point. Falls back to a plain merge-base
    (no reflog needed) when the reflog doesn't cover it — e.g. a freshly
    fetched remote branch, or an expired reflog.
    """
    try:
        out = run_git(repo, ['merge-base', '--fork-point', base, branch]).strip()
        if out:
            return out
    except RuntimeError:
        pass
    try:
        return run_git(repo, ['merge-base', base, branch]).strip() or None
    except RuntimeError:
        return None


def files_changed_since_fork(repo, branch, base):
    base_commit = fork_point(repo, branch, base)
    if not base_commit:
        return None
    try:
        out = run_git(repo, ['diff', '--name-only', base_commit, branch])
        return sum(1 for line in out.splitlines() if line.strip())
    except RuntimeError:
        return None


def list_remote_branches(repo):
    out = run_git(repo, ['branch', '-r', '--format=%(refname:short)'])
    return [b.strip() for b in out.splitlines() if b.strip()]


def remote_branch_map(repo):
    """Map local branch name -> remote name, based on existing '<remote>/<branch>' refs."""
    mapping = {}
    for ref in list_remote_branches(repo):
        if '/' in ref:
            remote, name = ref.split('/', 1)
            mapping[name] = remote
    return mapping


def delete_local_branch(repo, branch):
    run_git(repo, ['branch', '-D', branch])


def delete_remote_branch(repo, remote, branch):
    run_git(repo, ['push', remote, '--delete', branch])


def git_toplevel(path):
    """git diff/show/difftool paths are always relative to the repo's
    top-level directory, not to whatever subdirectory git was invoked from —
    resolve it once so file paths can be joined correctly."""
    out = run_git(path, ['rev-parse', '--show-toplevel'])
    return Path(out.strip())


def branch_last_commit_date(repo, branch):
    """Date of the branch tip's commit, or (if that ref has no commit yet)
    the earliest reflog entry as an approximation of when it was created."""
    try:
        out = run_git(repo, ['log', '-1', '--format=%cI', branch]).strip()
        if out:
            return _format_iso_date(out)
    except RuntimeError:
        pass
    try:
        out = run_git(repo, ['reflog', 'show', '--format=%cI', branch]).strip()
        lines = [line for line in out.splitlines() if line]
        if lines:
            return _format_iso_date(lines[-1])
    except RuntimeError:
        pass
    return "-"


def _format_iso_date(iso_str):
    try:
        return datetime.fromisoformat(iso_str).strftime('%Y-%m-%d %H:%M')
    except ValueError:
        return iso_str


def branch_files_changed(repo, branch):
    try:
        out = run_git(repo, ['diff', '--name-only', branch])
        return sum(1 for line in out.splitlines() if line.strip())
    except RuntimeError:
        return None


def parse_name_status_z(raw):
    tokens = raw.split('\0')
    rows = {}
    i = 0
    while i < len(tokens) and tokens[i] != '':
        code = tokens[i]
        status = code[0]
        if status in ('R', 'C'):
            old_path, new_path = tokens[i + 1], tokens[i + 2]
            rows[new_path] = FileRow(status=status, path=new_path, old_path=old_path)
            i += 3
        else:
            path = tokens[i + 1]
            rows[path] = FileRow(status=status, path=path)
            i += 2
    return rows


def parse_numstat_z(raw, rows):
    tokens = raw.split('\0')
    i = 0
    while i < len(tokens) and tokens[i] != '':
        added, deleted, rest = tokens[i].split('\t', 2)
        if rest:
            path = rest
            i += 1
        else:
            path = tokens[i + 2]
            i += 3
        if path in rows:
            rows[path].added = added
            rows[path].deleted = deleted


def _diff_rows(repo, args, added_reason, deleted_reason):
    name_status = run_git(repo, ['diff', '--find-renames', '--name-status', '-z'] + args)
    rows = parse_name_status_z(name_status)

    numstat = run_git(repo, ['diff', '--find-renames', '--numstat', '-z'] + args)
    parse_numstat_z(numstat, rows)

    for r in rows.values():
        if r.status == 'A':
            r.comparable = False
            r.reason = added_reason
        elif r.status == 'D':
            r.comparable = False
            r.reason = deleted_reason

    return sorted(rows.values(), key=lambda r: r.path)


def diff_rows(repo, branch):
    """Diff the working tree against `branch`."""
    return _diff_rows(repo, [branch], "Added (only in working tree)", "Deleted (only in branch)")


def diff_rows_refs(repo, base, target):
    """Diff two fixed refs/commits against each other — no working tree involved."""
    return _diff_rows(repo, [base, target],
                       "Added (only in branch, not at fork point)",
                       "Deleted (present at fork point, removed in branch)")


def extract_branch_file(repo, branch, path, cache_dir):
    dest = cache_dir / branch.replace('/', '_') / path
    dest.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(['git', '-C', str(repo), 'show', f'{branch}:{path}'],
                             capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode(errors='replace').strip())
    dest.write_bytes(result.stdout)
    return dest


class SettingsDialog(QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("Comparison Settings")

        self.studio_edit = QLineEdit(settings.value("studio_path", DEFAULT_STUDIO))
        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._browse_studio)
        studio_row = QHBoxLayout()
        studio_row.addWidget(self.studio_edit)
        studio_row.addWidget(browse_btn)

        self.mode_direct = QRadioButton("Direct — extract files and call studio diff")
        self.mode_difftool = QRadioButton("git difftool -x <command>")
        mode = settings.value("mode", "direct")
        self.mode_direct.setChecked(mode != "difftool")
        self.mode_difftool.setChecked(mode == "difftool")

        self.difftool_cmd_edit = QLineEdit(settings.value("difftool_cmd", DEFAULT_DIFFTOOL_CMD))

        form = QFormLayout()
        form.addRow("Studio binary:", studio_row)
        form.addRow(self.mode_direct)
        form.addRow(self.mode_difftool)
        form.addRow("difftool -x command:", self.difftool_cmd_edit)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def _browse_studio(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select Studio binary", "/Applications")
        if path:
            self.studio_edit.setText(path)

    def accept(self):
        self.settings.setValue("studio_path", self.studio_edit.text().strip() or DEFAULT_STUDIO)
        self.settings.setValue("mode", "difftool" if self.mode_difftool.isChecked() else "direct")
        self.settings.setValue("difftool_cmd", self.difftool_cmd_edit.text().strip() or DEFAULT_DIFFTOOL_CMD)
        super().accept()


BRANCH_TABLE_HEADERS = ["Branch", "Last Commit", "Files Changed", "Changed Since Fork", "Delete Local", "Delete Remote"]
BR_COL_BRANCH, BR_COL_DATE, BR_COL_CHANGED, BR_COL_FORK, BR_COL_DEL_LOCAL, BR_COL_DEL_REMOTE = range(6)


class BranchPickerDialog(QDialog):
    branchSelected = pyqtSignal(str)
    branchesChanged = pyqtSignal()
    forkDiffRequested = pyqtSignal(str)

    def __init__(self, repo, branches, parent=None):
        super().__init__(parent)
        self.repo = repo
        self.setWindowTitle("Select Branch to Compare")
        self.setWindowIcon(app_icon())
        self.setModal(False)
        self.resize(960, 420)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Click a branch to compare the working tree against it:"))

        self.table = QTableWidget(0, len(BRANCH_TABLE_HEADERS))
        self.table.setHorizontalHeaderLabels(BRANCH_TABLE_HEADERS)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.cellClicked.connect(self._on_cell_clicked)
        layout.addWidget(self.table)

        self._populate(branches)

    def _populate(self, branches):
        current = current_branch(self.repo)
        base = current or 'HEAD'
        remote_map = remote_branch_map(self.repo)

        self.table.setRowCount(len(branches))
        for r, branch in enumerate(branches):
            date_str = branch_last_commit_date(self.repo, branch)
            changed = branch_files_changed(self.repo, branch)
            since_fork = files_changed_since_fork(self.repo, branch, base) if branch != current else 0
            self.table.setItem(r, BR_COL_BRANCH, QTableWidgetItem(branch))
            self.table.setItem(r, BR_COL_DATE, QTableWidgetItem(date_str))
            self.table.setItem(r, BR_COL_CHANGED, QTableWidgetItem(str(changed) if changed is not None else "-"))

            fork_btn = QPushButton(str(since_fork) if since_fork is not None else "-")
            fork_btn.setEnabled(since_fork is not None)
            if since_fork is None:
                fork_btn.setToolTip("No fork point found")
            else:
                fork_btn.setToolTip("Show files changed since this branch's fork point in the comparison table")
            fork_btn.clicked.connect(lambda _checked, b=branch: self.forkDiffRequested.emit(b))
            self.table.setCellWidget(r, BR_COL_FORK, fork_btn)

            del_local_btn = QPushButton("Delete")
            del_local_btn.setEnabled(branch != current)
            if branch == current:
                del_local_btn.setToolTip("Cannot delete the currently checked-out branch")
            del_local_btn.clicked.connect(lambda _checked, b=branch: self._delete_local(b))
            self.table.setCellWidget(r, BR_COL_DEL_LOCAL, del_local_btn)

            remote = remote_map.get(branch)
            del_remote_btn = QPushButton("Delete")
            del_remote_btn.setEnabled(remote is not None)
            if remote is None:
                del_remote_btn.setToolTip("No remote branch found")
            del_remote_btn.clicked.connect(lambda _checked, b=branch, rmt=remote: self._delete_remote(b, rmt))
            self.table.setCellWidget(r, BR_COL_DEL_REMOTE, del_remote_btn)

        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setSectionResizeMode(BR_COL_BRANCH, QHeaderView.ResizeMode.Stretch)

    def _on_cell_clicked(self, r, c):
        if c in (BR_COL_FORK, BR_COL_DEL_LOCAL, BR_COL_DEL_REMOTE):
            return
        self.branchSelected.emit(self.table.item(r, BR_COL_BRANCH).text())

    def _confirm(self, title, branch, args):
        cmd = format_git_cmd(self.repo, args)
        box = QMessageBox(self)
        box.setWindowTitle(title)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText(f"Delete branch '{branch}'?")
        box.setInformativeText(f"This will run:\n\n{cmd}")
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        return box.exec() == QMessageBox.StandardButton.Yes

    def _delete_local(self, branch):
        if not self._confirm("Delete Local Branch", branch, ['branch', '-D', branch]):
            return
        try:
            delete_local_branch(self.repo, branch)
        except RuntimeError as e:
            QMessageBox.warning(self, "Delete failed", str(e))
            return
        self._refresh()

    def _delete_remote(self, branch, remote):
        if not self._confirm("Delete Remote Branch", branch, ['push', remote, '--delete', branch]):
            return
        try:
            delete_remote_branch(self.repo, remote, branch)
        except RuntimeError as e:
            QMessageBox.warning(self, "Delete failed", str(e))
            return
        self._refresh()

    def _refresh(self):
        self._populate(list_branches(self.repo))
        self.branchesChanged.emit()


class MainWindow(QMainWindow):
    def __init__(self, repo, branch):
        super().__init__()
        self.repo = self._resolve_repo(repo)
        self.settings = QSettings(ORG, APP)
        self.rows = []
        self._row_color_kinds = []
        self.compare_base = None
        self.compare_target = None
        self.dark_mode = self.settings.value("dark_mode", False, type=bool)
        self.row_colors = DARK_ROW_COLORS if self.dark_mode else LIGHT_ROW_COLORS
        self._tmpdir = tempfile.TemporaryDirectory(prefix="git-compare-ui-")
        self.cache_dir = Path(self._tmpdir.name)

        self.setWindowTitle(f"git-compare-ui v{__version__} — {self.repo.name}")
        self.setWindowIcon(app_icon())
        self.resize(1000, 600)

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        self.active_extensions = set()
        self.extension_actions = {}

        top = QHBoxLayout()
        top.addWidget(QLabel("Repo:"))
        self.repo_edit = QLineEdit(str(self.repo))
        self.repo_edit.setReadOnly(True)
        top.addWidget(self.repo_edit)
        browse_repo_btn = QPushButton("Browse…")
        browse_repo_btn.setFixedHeight(TOOLBAR_BTN_HEIGHT)
        browse_repo_btn.setToolTip("Browse for a different git repository")
        browse_repo_btn.clicked.connect(self._browse_repo)
        top.addWidget(browse_repo_btn)

        top.addWidget(QLabel("Compare to:"))
        self.branch_combo = QComboBox()
        self.branch_combo.setEditable(True)
        top.addWidget(self.branch_combo)

        refresh_btn = QPushButton()
        refresh_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload))
        refresh_btn.setIconSize(QSize(16, 16))
        refresh_btn.setFixedSize(TOOLBAR_BTN_HEIGHT, TOOLBAR_BTN_HEIGHT)
        refresh_btn.setToolTip("Refresh the comparison table")
        refresh_btn.clicked.connect(self.refresh)
        top.addWidget(refresh_btn)

        run_checked_btn = QPushButton("Run Checked")
        run_checked_btn.setFixedHeight(TOOLBAR_BTN_HEIGHT)
        run_checked_btn.setToolTip("Launch the diff tool for every checked row")
        run_checked_btn.clicked.connect(self.run_checked)
        top.addWidget(run_checked_btn)

        settings_btn = QPushButton("⚙")
        settings_btn.setFixedSize(TOOLBAR_BTN_HEIGHT, TOOLBAR_BTN_HEIGHT)
        settings_btn.setToolTip("Open comparison settings")
        settings_btn.clicked.connect(self.open_settings)
        top.addWidget(settings_btn)

        self.theme_btn = QPushButton()
        self.theme_btn.setIcon(QIcon(str(THEME_ICON_PATH)))
        self.theme_btn.setIconSize(QSize(16, 16))
        self.theme_btn.setFixedSize(TOOLBAR_BTN_HEIGHT, TOOLBAR_BTN_HEIGHT)
        self.theme_btn.setToolTip("Toggle dark/light theme")
        self.theme_btn.clicked.connect(self.toggle_theme)
        top.addWidget(self.theme_btn)

        self.show_all = False
        self.toggle_filter_btn = QPushButton()
        self.toggle_filter_btn.setFixedSize(TOOLBAR_BTN_HEIGHT, TOOLBAR_BTN_HEIGHT)
        self.toggle_filter_btn.clicked.connect(self._toggle_filter)
        self._update_toggle_filter_btn()
        top.addWidget(self.toggle_filter_btn)

        self.filter_btn = QToolButton()
        self.filter_btn.setText("Filter")
        self.filter_btn.setFixedHeight(TOOLBAR_BTN_HEIGHT)
        self.filter_btn.setToolTip("Filter rows by file extension")
        self.filter_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._build_filter_menu()
        top.addWidget(self.filter_btn)

        layout.addLayout(top)

        self.table = QTableWidget(0, len(HEADERS))
        self.table.setHorizontalHeaderLabels(HEADERS)
        self.table.horizontalHeader().setSectionResizeMode(COL_FILE, QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.cellDoubleClicked.connect(self._on_double_click)
        layout.addWidget(self.table)

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)

        self.apply_theme()

        self.branch_picker = None
        self._load_branches(branch)
        if branch:
            self.refresh()
        else:
            self.branch_combo.setCurrentText("")
            self.status_bar.showMessage("Select a branch to compare against…")
            self._show_branch_picker()

    def _resolve_repo(self, path):
        path = Path(path).resolve()
        try:
            return git_toplevel(path)
        except RuntimeError:
            return path

    def _load_branches(self, preselect):
        try:
            branches = list_branches(self.repo)
        except RuntimeError as e:
            QMessageBox.warning(self, "git branch failed", str(e))
            branches = []
        self.branch_combo.addItems(branches)
        if preselect:
            idx = self.branch_combo.findText(preselect)
            if idx >= 0:
                self.branch_combo.setCurrentIndex(idx)
            else:
                self.branch_combo.setCurrentText(preselect)

    def _browse_repo(self):
        path = QFileDialog.getExistingDirectory(self, "Select repo", str(self.repo))
        if path:
            self.repo = self._resolve_repo(path)
            self.repo_edit.setText(str(self.repo))
            self.branch_combo.clear()
            self._load_branches(None)
            self.refresh()

    def _show_branch_picker(self):
        branches = [self.branch_combo.itemText(i) for i in range(self.branch_combo.count())]
        if not branches:
            return
        # No Qt parent on purpose: on macOS a dialog with a widget parent becomes
        # a native *child window* of that parent, and Cocoa auto-closes child
        # windows when the parent window closes — which then leaves zero
        # top-level windows open and quits the app. Keeping this parentless
        # makes it a fully independent top-level window, so closing the main
        # window (or vice versa) only quits the app once both are closed.
        self.branch_picker = BranchPickerDialog(self.repo, branches, None)
        self.branch_picker.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, True)
        self.branch_picker.branchSelected.connect(self.apply_branch)
        self.branch_picker.branchesChanged.connect(self._on_branches_changed)
        self.branch_picker.forkDiffRequested.connect(self.show_fork_diff)

        # Being parentless (see above) means the window manager doesn't know to
        # stack this above the main window, and it can otherwise land exactly on
        # top of it — so show the main window first (for a real position to
        # offset from), spread the two side-by-side (picker left, main right)
        # with a slight vertical stagger, then explicitly bring the picker to front.
        self.show()
        base_x, base_y = self.x(), self.y()
        self.move(base_x + 60, base_y)
        self.branch_picker.move(max(base_x - 60, 0), base_y + 40)
        self.branch_picker.show()
        self.branch_picker.raise_()
        self.branch_picker.activateWindow()

    def _on_branches_changed(self):
        current_text = self.branch_combo.currentText()
        self.branch_combo.clear()
        self._load_branches(current_text)

    def apply_branch(self, branch):
        self.compare_base = None
        self.compare_target = None
        idx = self.branch_combo.findText(branch)
        if idx >= 0:
            self.branch_combo.setCurrentIndex(idx)
        else:
            self.branch_combo.setCurrentText(branch)
        self.refresh()
        self.show()
        self.raise_()
        self.activateWindow()

    def show_fork_diff(self, branch):
        base_branch = current_branch(self.repo) or 'HEAD'
        fp = fork_point(self.repo, branch, base_branch)
        if not fp:
            QMessageBox.warning(self, "No fork point found",
                                 f"Could not determine where '{branch}' forked from '{base_branch}'.")
            return

        self.compare_base = fp
        self.compare_target = branch
        idx = self.branch_combo.findText(branch)
        if idx >= 0:
            self.branch_combo.setCurrentIndex(idx)
        else:
            self.branch_combo.setCurrentText(branch)

        self.refresh()
        self.status_bar.showMessage(
            f"Comparing '{branch}' against its fork point {fp[:8]} from '{base_branch}' — "
            f"{len(self.rows)} file(s) changed since fork")
        self.show()
        self.raise_()
        self.activateWindow()

    def open_settings(self):
        dlg = SettingsDialog(self.settings, self)
        dlg.exec()

    def toggle_theme(self):
        self.dark_mode = not self.dark_mode
        self.settings.setValue("dark_mode", self.dark_mode)
        self.apply_theme()

    def apply_theme(self):
        self.row_colors = DARK_ROW_COLORS if self.dark_mode else LIGHT_ROW_COLORS
        app = QApplication.instance()
        if self.dark_mode:
            app.setStyle("Fusion")
            app.setPalette(build_dark_palette())
        else:
            app.setStyle(_DEFAULT_STYLE_NAME)
            app.setPalette(_DEFAULT_PALETTE)
        for r, kind in enumerate(self._row_color_kinds):
            if kind:
                self._set_row_color(r, kind)

    def refresh(self):
        if self.compare_base:
            try:
                self.rows = diff_rows_refs(self.repo, self.compare_base, self.compare_target)
            except RuntimeError as e:
                QMessageBox.warning(self, "git diff failed", str(e))
                self.rows = []
            self._populate_table()
            return

        branch = self.branch_combo.currentText().strip()
        if not branch:
            return
        try:
            self.rows = diff_rows(self.repo, branch)
        except RuntimeError as e:
            QMessageBox.warning(self, "git diff failed", str(e))
            self.rows = []
        self._populate_table()

    def _populate_table(self):
        self.table.setRowCount(len(self.rows))
        self._row_color_kinds = [None] * len(self.rows)
        for r, row in enumerate(self.rows):
            checkbox = QCheckBox()
            checkbox.setChecked(row.comparable)
            checkbox.setEnabled(row.comparable)
            checkbox.toggled.connect(lambda checked, rr=r: self._on_checkbox_toggled(rr, checked))
            cell_widget = QWidget()
            cell_layout = QHBoxLayout(cell_widget)
            cell_layout.addWidget(checkbox)
            cell_layout.setContentsMargins(0, 0, 0, 0)
            cell_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setCellWidget(r, COL_CHECK, cell_widget)

            self.table.setItem(r, COL_FILE, QTableWidgetItem(row.path))
            self.table.setItem(r, COL_STATUS, QTableWidgetItem(STATUS_NAMES.get(row.status, row.status)))
            self.table.setItem(r, COL_ADD, QTableWidgetItem(str(row.added)))
            self.table.setItem(r, COL_DEL, QTableWidgetItem(str(row.deleted)))
            result_text = row.reason if not row.comparable else ""
            self.table.setItem(r, COL_RESULT, QTableWidgetItem(result_text))

            self._set_row_color(r, 'skip' if not row.comparable else 'default')

        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setSectionResizeMode(COL_FILE, QHeaderView.ResizeMode.Stretch)
        comparable = sum(1 for r in self.rows if r.comparable)
        self.status_bar.showMessage(f"{len(self.rows)} files — {comparable} comparable, "
                                     f"{len(self.rows) - comparable} skipped")
        self._apply_row_filter()

    def _toggle_filter(self):
        self.show_all = not self.show_all
        self._update_toggle_filter_btn()
        self._apply_row_filter()

    def _update_toggle_filter_btn(self):
        if self.show_all:
            icon = QStyle.StandardPixmap.SP_TitleBarShadeButton
            tooltip = "Collapse to checked rows only"
        else:
            icon = QStyle.StandardPixmap.SP_TitleBarUnshadeButton
            tooltip = "Expand to show all rows"
        self.toggle_filter_btn.setIcon(self.style().standardIcon(icon))
        self.toggle_filter_btn.setIconSize(QSize(16, 16))
        self.toggle_filter_btn.setToolTip(tooltip)

    def _extension_visible(self, path):
        if not self.active_extensions:
            return True
        return Path(path).suffix.lower() in self.active_extensions

    def _apply_row_filter(self):
        for r in range(self.table.rowCount()):
            hide_by_state = (not self.show_all) and (not self._row_checked(r))
            hide_by_ext = not self._extension_visible(self.rows[r].path)
            self.table.setRowHidden(r, hide_by_state or hide_by_ext)

    def _on_checkbox_toggled(self, r, checked):
        if not self.show_all:
            hide_by_ext = not self._extension_visible(self.rows[r].path)
            self.table.setRowHidden(r, (not checked) or hide_by_ext)

    def _build_filter_menu(self):
        self.filter_menu = QMenu(self.filter_btn)

        self.all_ext_action = self.filter_menu.addAction("All")
        self.all_ext_action.setCheckable(True)
        self.all_ext_action.setChecked(True)
        self.all_ext_action.triggered.connect(self._select_all_extensions)
        self.filter_menu.addSeparator()

        for ext in DEFAULT_FILTER_EXTENSIONS:
            self._add_extension_action(ext)

        self.filter_menu.addSeparator()
        self.add_extension_action = self.filter_menu.addAction("Add extension…")
        self.add_extension_action.triggered.connect(self._prompt_add_extension)

        self.filter_btn.setMenu(self.filter_menu)

    @staticmethod
    def _normalize_extension(ext):
        ext = ext.strip().lower()
        if not ext:
            return ""
        return ext if ext.startswith('.') else '.' + ext

    def _add_extension_action(self, ext, checked=False):
        ext = self._normalize_extension(ext)
        if not ext:
            return None
        if ext in self.extension_actions:
            action = self.extension_actions[ext]
            if checked:
                action.setChecked(True)
            return action

        action = QAction(ext, self)
        action.setCheckable(True)
        action.setChecked(checked)
        action.triggered.connect(lambda _checked, e=ext: self._on_extension_toggled(e))
        if hasattr(self, 'add_extension_action'):
            self.filter_menu.insertAction(self.add_extension_action, action)
        else:
            self.filter_menu.addAction(action)
        self.extension_actions[ext] = action
        return action

    def _select_all_extensions(self):
        self.all_ext_action.setChecked(True)
        for action in self.extension_actions.values():
            action.setChecked(False)
        self.active_extensions = set()
        self._apply_row_filter()

    def _on_extension_toggled(self, ext):
        action = self.extension_actions[ext]
        if action.isChecked():
            self.active_extensions.add(ext)
            self.all_ext_action.setChecked(False)
        else:
            self.active_extensions.discard(ext)
            if not self.active_extensions:
                self.all_ext_action.setChecked(True)
        self._apply_row_filter()

    def _prompt_add_extension(self):
        text, ok = QInputDialog.getText(self, "Add Extension Filter", "File extension (e.g. .kt):")
        if not ok:
            return
        ext = self._normalize_extension(text)
        if not ext:
            return
        self._add_extension_action(ext, checked=True)
        self.all_ext_action.setChecked(False)
        self.active_extensions.add(ext)
        self._apply_row_filter()

    def _set_row_color(self, r, kind):
        self._row_color_kinds[r] = kind
        color = QColor(self.row_colors[kind])
        for c in range(len(HEADERS)):
            if c == COL_CHECK:
                continue
            item = self.table.item(r, c)
            if item is not None:
                item.setBackground(color)

    def _row_checked(self, r):
        widget = self.table.cellWidget(r, COL_CHECK)
        checkbox = widget.findChild(QCheckBox)
        return checkbox.isChecked() if checkbox else False

    def _on_double_click(self, r, _col):
        self.run_row(r)

    def run_checked(self):
        for r, row in enumerate(self.rows):
            if row.comparable and self._row_checked(r):
                self.run_row(r)

    def run_row(self, r):
        row = self.rows[r]
        if not row.comparable:
            self.status_bar.showMessage(f"Cannot compare: {row.path} — {row.reason}")
            return

        mode = self.settings.value("mode", "direct")
        studio_path = self.settings.value("studio_path", DEFAULT_STUDIO)
        difftool_cmd = self.settings.value("difftool_cmd", DEFAULT_DIFFTOOL_CMD)

        try:
            if self.compare_base:
                # Fork-point mode: both sides are fixed commits, neither is the
                # working tree, so both must be extracted via `git show`.
                base_ref, target_ref = self.compare_base, self.compare_target
                if mode == "difftool":
                    ok = QProcess.startDetached(
                        'git', ['-C', str(self.repo), 'difftool', '-y', '-x', difftool_cmd,
                                base_ref, target_ref, '--', row.path])
                else:
                    left_tmp = extract_branch_file(self.repo, base_ref, row.old_path or row.path, self.cache_dir)
                    right_tmp = extract_branch_file(self.repo, target_ref, row.path, self.cache_dir)
                    ok = QProcess.startDetached(studio_path, ['diff', str(left_tmp), str(right_tmp)])
            else:
                branch = self.branch_combo.currentText().strip()
                if mode == "difftool":
                    ok = QProcess.startDetached(
                        'git', ['-C', str(self.repo), 'difftool', '-y', '-x', difftool_cmd, branch, '--', row.path])
                else:
                    left_tmp = extract_branch_file(self.repo, branch, row.old_path or row.path, self.cache_dir)
                    right_path = self.repo / row.path
                    ok = QProcess.startDetached(studio_path, ['diff', str(left_tmp), str(right_path)])

            if ok:
                self.table.setItem(r, COL_RESULT,
                                    QTableWidgetItem(f"Launched {datetime.now().strftime('%H:%M:%S')}"))
                self._set_row_color(r, 'ran')
            else:
                self.table.setItem(r, COL_RESULT, QTableWidgetItem("Failed to launch"))
                self._set_row_color(r, 'error')
        except RuntimeError as e:
            self.table.setItem(r, COL_RESULT, QTableWidgetItem(f"Error: {e}"))
            self._set_row_color(r, 'error')

    def closeEvent(self, event):
        self._tmpdir.cleanup()
        super().closeEvent(event)


def main():
    parser = argparse.ArgumentParser(description="GUI table for reviewing and launching per-file git diffs")
    parser.add_argument('branch', nargs='?', default='', help='compare-to branch (e.g. main)')
    parser.add_argument('--repo', default='.', help='path to the git repo (default: current directory)')
    args = parser.parse_args()

    app = QApplication(sys.argv)
    QApplication.setOrganizationName(ORG)
    QApplication.setApplicationName(APP)
    QApplication.setApplicationVersion(__version__)
    app.setWindowIcon(app_icon())

    global _DEFAULT_STYLE_NAME, _DEFAULT_PALETTE
    _DEFAULT_STYLE_NAME = app.style().objectName()
    _DEFAULT_PALETTE = QPalette(app.palette())

    window = MainWindow(args.repo, args.branch)
    window.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
