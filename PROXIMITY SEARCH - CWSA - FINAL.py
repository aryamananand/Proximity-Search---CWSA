from __future__ import annotations

import hashlib
import html
import os
import platform
import re
import sqlite3
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    import pymupdf  # Modern package name; the old "fitz" alias is deprecated.
except ImportError:
    print(
        "Missing dependency: PyMuPDF.\n"
        "Install it with:\n\n    pip install PyMuPDF\n"
    )
    raise SystemExit(1)

try:
    from PySide6.QtCore import QByteArray, Qt, QThread, Signal, QStandardPaths, QSettings
    from PySide6.QtGui import (
        QAction,
        QTextCursor,
        QColor,
        QFont,
        QIcon,
        QKeySequence,
        QPainter,
        QPixmap,
        QShortcut,
    )
    from PySide6.QtWidgets import (
        QApplication,
        QDialog,
        QFileDialog,
        QFrame,
        QGraphicsDropShadowEffect,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QMainWindow,
        QMessageBox,
        QProgressBar,
        QPushButton,
        QListWidget,
        QListWidgetItem,
        QScrollArea,
        QSpinBox,
        QSplitter,
        QToolButton,
        QMenu,
        QTextBrowser,
        QVBoxLayout,
        QWidget,
    )
except ImportError:
    print(
        "Missing dependency: PySide6.\n"
        "Install it with:\n\n    pip install PySide6\n"
    )
    raise SystemExit(1)

APP_NAME = "Sri Aurobindo — Proximity Search"
APP_VERSION = "9.0"
SETTINGS_ORG = "SriAurobindoAshram"
SETTINGS_APP = "ProximitySearch"
DB_VERSION = 8
DEFAULT_GAP = 15
MAX_RESULTS = 500

# Warm parchment palette — same feel on every platform.
BG = "#f5f1e8"
CARD = "#fffdf9"
CARD_ALT = "#fbf7ee"
TEXT = "#302d29"
MUTED = "#766f66"
FAINT = "#a09588"
BORDER = "#ddd3c5"
BORDER_STRONG = "#b89457"
YELLOW = "#fff0a6"
YELLOW_STRONG = "#ffd928"

# Cross-platform font stacks. Qt (and HTML) walk the list left to right and
# use the first family that is actually installed, so the app looks native
# and "regular" (no decorative/serif look) on Windows, macOS and Linux alike.
FONT_STACK = (
    '-apple-system, "Segoe UI", "Noto Sans", "Ubuntu", "Cantarell", '
    '"Helvetica Neue", Arial, sans-serif'
)
MONO_STACK = (
    '"Cascadia Mono", "SF Mono", Consolas, "Ubuntu Mono", Menlo, Monaco, monospace'
)

WORD_TOKEN_RE = re.compile(r"\S+", re.UNICODE)
STRUCTURAL_RE = re.compile(
    r"^(part|section|chapter|canto|book|letter|appendix|act|scene|volume)\b",
    re.I,
)


def norm_text(s: str) -> str:
    s = s.replace("\u00ad", "")
    s = s.replace("\u2019", "'").replace("\u2018", "'")
    s = s.replace("\u201c", '"').replace("\u201d", '"')
    s = s.replace("\u2013", "-").replace("\u2014", "-")
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def search_norm(s: str) -> str:
    return norm_text(s).casefold()


def join_word_text(words: list[str]) -> str:
    if not words:
        return ""
    out = words[0]
    no_space_before = set(",.;:!?)]}»”'\")")
    no_space_after = set("([{«“'\"")
    for w in words[1:]:
        if not w:
            continue
        if w[0] in no_space_before or (out and out[-1] in no_space_after):
            out += w
        elif out.endswith("-"):
            out += w
        else:
            out += " " + w
    return norm_text(out)


def line_similarity_y(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    ay = (a[1] + a[3]) / 2
    by = (b[1] + b[3]) / 2
    ah = max(1.0, a[3] - a[1])
    bh = max(1.0, b[3] - b[1])
    tolerance = max(1.5, min(ah, bh) * 0.55)
    return abs(ay - by) <= tolerance


@dataclass
class VisualWord:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    block: int
    line: int
    word: int


@dataclass
class VisualLine:
    page_index: int
    line_no: int
    block: int
    text: str
    words: list[VisualWord]
    x0: float
    y0: float
    x1: float
    y1: float
    unit: str = ""
    unit_type: str = ""


@dataclass
class Hit:
    file_id: int
    file_name: str
    book: str
    pdf_page: str
    page_index: int
    line_no: int
    end_pdf_page: str
    end_page_index: int
    end_line_no: int
    unit: str
    unit_type: str
    context: str
    line_text: str
    query_words: list[str]
    start_word: int
    end_word: int


# --------------------------------------------------------------------------
# Per-library settings & storage location
# --------------------------------------------------------------------------
# The database used to live inside the folder of PDFs itself. That breaks on
# read-only / synced / network folders, and it left a hidden file behind in
# the user's document collection. It now lives in the OS-appropriate app
# data folder (Application Support on macOS, %APPDATA% on Windows, XDG data
# dir on Linux), keyed by a short hash of the library path so several
# collections can be indexed side by side without colliding.

def get_settings() -> QSettings:
    return QSettings(QSettings.IniFormat, QSettings.UserScope, SETTINGS_ORG, SETTINGS_APP)


def load_last_root() -> Optional[Path]:
    value = get_settings().value("library/root_path", "")
    if value:
        p = Path(str(value)).expanduser()
        if p.is_dir():
            return p
    return None


def save_last_root(root: Path) -> None:
    s = get_settings()
    s.setValue("library/root_path", str(root.resolve()))
    s.sync()


def load_last_gap() -> int:
    try:
        return max(1, int(get_settings().value("search/gap", DEFAULT_GAP)))
    except (TypeError, ValueError):
        return DEFAULT_GAP


def save_last_gap(value: int) -> None:
    s = get_settings()
    s.setValue("search/gap", int(value))


def load_window_geometry() -> Optional[QByteArray]:
    value = get_settings().value("window/geometry")
    return value if isinstance(value, QByteArray) and not value.isEmpty() else None


def save_window_geometry(data: QByteArray) -> None:
    get_settings().setValue("window/geometry", data)


def load_splitter_state() -> Optional[QByteArray]:
    value = get_settings().value("window/splitter")
    return value if isinstance(value, QByteArray) and not value.isEmpty() else None


def save_splitter_state(data: QByteArray) -> None:
    get_settings().setValue("window/splitter", data)


def library_data_dir(root: Path) -> Path:
    base = QStandardPaths.writableLocation(QStandardPaths.AppDataLocation)
    base_path = Path(base) if base else Path.home() / \
        ".sri_aurobindo_proximity_search"
    key = hashlib.sha1(str(root.resolve()).encode("utf-8")).hexdigest()[:12]
    data_dir = base_path / "libraries" / key
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def library_db_path(root: Path) -> Path:
    return library_data_dir(root) / "index.sqlite3"


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA temp_store=MEMORY")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.ensure_schema()

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass

    def ensure_schema(self):
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version != DB_VERSION:
            self.conn.executescript(
                """
                DROP TABLE IF EXISTS words;
                DROP TABLE IF EXISTS lines;
                DROP TABLE IF EXISTS pages;
                DROP TABLE IF EXISTS files;
                DROP TABLE IF EXISTS lines_fts;
                DROP TABLE IF EXISTS pages_fts;
                """
            )
            self.conn.executescript(
                """
                CREATE TABLE files (
                    id INTEGER PRIMARY KEY,
                    path TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    mtime REAL NOT NULL
                );

                CREATE TABLE pages (
                    id INTEGER PRIMARY KEY,
                    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
                    page_index INTEGER NOT NULL,
                    page_label TEXT NOT NULL,
                    width REAL NOT NULL,
                    height REAL NOT NULL,
                    UNIQUE(file_id, page_index)
                );

                CREATE TABLE lines (
                    id INTEGER PRIMARY KEY,
                    page_id INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
                    line_no INTEGER NOT NULL,
                    block_no INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    x0 REAL NOT NULL,
                    y0 REAL NOT NULL,
                    x1 REAL NOT NULL,
                    y1 REAL NOT NULL,
                    unit TEXT NOT NULL DEFAULT '',
                    unit_type TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE words (
                    id INTEGER PRIMARY KEY,
                    line_id INTEGER NOT NULL REFERENCES lines(id) ON DELETE CASCADE,
                    word_no INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    x0 REAL NOT NULL,
                    y0 REAL NOT NULL,
                    x1 REAL NOT NULL,
                    y1 REAL NOT NULL
                );

                CREATE VIRTUAL TABLE lines_fts USING fts5(
                    text,
                    content='lines',
                    content_rowid='id',
                    tokenize='unicode61 remove_diacritics 2'
                );

                -- Explicit indexes: SQLite does not auto-index foreign keys,
                -- and both of these are on the hot path of every search.
                CREATE INDEX idx_lines_page_line ON lines(page_id, line_no);
                CREATE INDEX idx_words_line ON words(line_id, word_no);
                CREATE INDEX idx_pages_file ON pages(file_id, page_index);
                """
            )
            self.conn.execute(f"PRAGMA user_version={DB_VERSION}")
            self.conn.commit()

    def clear(self):
        self.conn.execute("DELETE FROM words")
        self.conn.execute("DELETE FROM lines")
        self.conn.execute("DELETE FROM pages")
        self.conn.execute("DELETE FROM files")
        self.conn.commit()

    def upsert_file(self, path: Path) -> int:
        st = path.stat()
        row = self.conn.execute(
            "SELECT id,size,mtime FROM files WHERE path=?", (str(path),)).fetchone()
        if row and row[1] == st.st_size and abs(row[2] - st.st_mtime) < 0.01:
            return int(row[0])
        if row:
            self.conn.execute("DELETE FROM files WHERE id=?", (row[0],))
        cur = self.conn.execute(
            "INSERT INTO files(path,name,size,mtime) VALUES(?,?,?,?)",
            (str(path), path.name, st.st_size, st.st_mtime),
        )
        return int(cur.lastrowid)

    def insert_page(self, file_id: int, page_index: int, page_label: str, width: float, height: float) -> int:
        cur = self.conn.execute(
            "INSERT INTO pages(file_id,page_index,page_label,width,height) VALUES(?,?,?,?,?)",
            (file_id, page_index, page_label, width, height),
        )
        return int(cur.lastrowid)

    def insert_line(self, page_id: int, line: VisualLine) -> int:
        cur = self.conn.execute(
            """INSERT INTO lines(page_id,line_no,block_no,text,x0,y0,x1,y1,unit,unit_type)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                page_id,
                line.line_no,
                line.block,
                line.text,
                line.x0,
                line.y0,
                line.x1,
                line.y1,
                line.unit,
                line.unit_type,
            ),
        )
        line_id = int(cur.lastrowid)
        self.conn.executemany(
            """INSERT INTO words(line_id,word_no,text,x0,y0,x1,y1)
               VALUES(?,?,?,?,?,?,?)""",
            [(line_id, i, w.text, w.x0, w.y0, w.x1, w.y1)
             for i, w in enumerate(line.words)],
        )
        return line_id

    def rebuild_fts(self):
        self.conn.execute("INSERT INTO lines_fts(lines_fts) VALUES('rebuild')")
        self.conn.commit()

    def commit(self):
        self.conn.commit()

    def search_candidates(
        self,
        terms: list[str],
        limit: int = MAX_RESULTS,
        file_ids: Optional[set[int]] = None,
    ) -> list[sqlite3.Row]:
        """Return only structural units containing *every distinct* query term.

        FTS is used as the indexed candidate generator.  Each term gets its own
        FTS probe and the probes are intersected at the structural-unit level.
        This is substantially faster than the previous OR-based query, which
        admitted every unit containing even one common word and forced Python to
        scan many irrelevant chapters/letters.

        The final phrase/order/range test is still done by the exact matcher, so
        this candidate stage never changes search semantics.
        """
        unique_terms: list[str] = []
        seen: set[str] = set()
        for term in terms:
            normalized = token_norm(term)
            if normalized and normalized not in seen:
                unique_terms.append(normalized)
                seen.add(normalized)
        if not unique_terms:
            return []

        ids_clause = ""
        id_params: list[object] = []
        if file_ids:
            ids = sorted(file_ids)
            ids_clause = " AND f.id IN (" + ",".join("?" for _ in ids) + ")"
            id_params.extend(ids)

        arms: list[str] = []
        params: list[object] = []
        for term_index, term in enumerate(unique_terms):
            arms.append(
                f"""
                SELECT
                    l.id AS candidate_line_id,
                    l.unit AS unit,
                    l.unit_type AS unit_type,
                    f.id AS file_id,
                    f.name AS file_name,
                    f.path AS path,
                    {term_index} AS term_index
                FROM lines_fts x
                JOIN lines l ON l.id = x.rowid
                JOIN pages p ON p.id = l.page_id
                JOIN files f ON f.id = p.file_id
                WHERE lines_fts MATCH ?{ids_clause}
                """
            )
            params.append('"' + term.replace('"', '""') + '"')
            params.extend(id_params)

        sql = f"""
            SELECT
                MIN(candidate_line_id) AS candidate_line_id,
                unit, unit_type, file_id, file_name, path
            FROM (
                {' UNION ALL '.join(arms)}
            ) AS candidates
            GROUP BY file_id, unit, unit_type, file_name, path
            HAVING COUNT(DISTINCT term_index) = ?
            ORDER BY file_id, unit, unit_type
            LIMIT ?
        """
        params.extend([len(unique_terms), limit])
        return self.conn.execute(sql, params).fetchall()

    def get_unit_word_stream(
        self,
        file_id: int,
        unit: str,
        unit_type: str,
    ) -> list[sqlite3.Row]:
        """Return all indexed words in one structural unit in reading order."""
        return self.conn.execute(
            """
            SELECT
                w.text AS word_text,
                w.word_no,
                l.id AS line_id,
                l.line_no,
                l.text AS line_text,
                p.page_index,
                p.page_label
            FROM words w
            JOIN lines l ON l.id=w.line_id
            JOIN pages p ON p.id=l.page_id
            WHERE p.file_id=? AND l.unit=? AND l.unit_type=?
            ORDER BY p.page_index, l.line_no, w.word_no
            """,
            (file_id, unit, unit_type),
        ).fetchall()

    def list_files(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT id, name, path FROM files ORDER BY name COLLATE NOCASE, path COLLATE NOCASE"
        ).fetchall()

    def get_hit_context_lines(
        self,
        file_id: int,
        unit: str,
        unit_type: str,
        start_page_index: int,
        start_line_no: int,
        end_page_index: int,
        end_line_no: int,
        pad: int = 8,
        page_margin: int = 3,
    ) -> tuple[list[tuple[int, str, int, str]], int, int]:
        """Return a padded window of lines around a match, plus the
        window-relative indices of the match's first and last line.

        The match itself may span several visual lines and even several PDF
        pages (a wide word range can do that), so the whole inclusive range
        from (start_page_index, start_line_no) to (end_page_index,
        end_line_no) is always highlighted — not just the line the match
        starts on. Every candidate line still comes from the same
        structural unit as the match, the same boundary the search itself
        enforces, so the preview never wanders into a neighbouring chapter
        or letter for context. Bounding the query to a small page range
        (rather than the whole unit) keeps this fast even for a unit that
        spans hundreds of pages.
        """
        lo_page = max(0, start_page_index - page_margin)
        hi_page = end_page_index + page_margin
        rows = self.conn.execute(
            """
            SELECT p.page_index, p.page_label, l.line_no, l.text
            FROM lines l
            JOIN pages p ON p.id = l.page_id
            WHERE p.file_id=? AND l.unit=? AND l.unit_type=?
                  AND p.page_index BETWEEN ? AND ?
            ORDER BY p.page_index, l.line_no
            """,
            (file_id, unit, unit_type, lo_page, hi_page),
        ).fetchall()
        lines = [(int(r[0]), str(r[1]), int(r[2]), str(r[3])) for r in rows]
        if not lines:
            return [], 0, 0

        index = {(pi, ln): i for i, (pi, _, ln, _) in enumerate(lines)}
        start_idx = index.get((start_page_index, start_line_no))
        end_idx = index.get((end_page_index, end_line_no))
        if start_idx is None and end_idx is None:
            return [], 0, 0
        if start_idx is None:
            start_idx = end_idx
        if end_idx is None:
            end_idx = start_idx

        window_start = max(0, start_idx - pad)
        window_end = min(len(lines), end_idx + 1 + pad)
        window = lines[window_start:window_end]
        return window, start_idx - window_start, end_idx - window_start


def find_pdfs(root: Path) -> list[Path]:
    """Find every PDF under *root*, regardless of extension case.

    ``Path.rglob("*.pdf")`` is case-sensitive on Linux, so a folder holding
    ``Letter.PDF`` alongside ``Letter.pdf`` would index inconsistently
    depending on the OS. Matching case-insensitively and de-duplicating by
    resolved path keeps the same library behaving identically on Windows,
    macOS, and Linux.
    """
    seen: dict[Path, Path] = {}
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() == ".pdf":
            seen[path.resolve()] = path
    return sorted(seen.values(), key=lambda p: str(p).lower())


class PDFIndexer:
    def __init__(self, db: Database, root: Path, progress=None):
        self.db = db
        self.root = root
        self.progress = progress

    def emit(self, msg: str):
        if self.progress:
            self.progress(msg)

    @staticmethod
    def pdf_page_label(page: pymupdf.Page, index: int) -> str:
        try:
            label = page.get_label()
        except Exception:
            label = ""
        label = str(label).strip() if label is not None else ""
        return label or str(index + 1)

    @staticmethod
    def toc_context(doc: pymupdf.Document) -> dict[int, tuple[str, str]]:
        """Map physical page indices to the most recent TOC title and type."""
        result: dict[int, tuple[str, str]] = {}
        try:
            toc = doc.get_toc(simple=True)
        except Exception:
            toc = []
        entries: list[tuple[int, str, int]] = []
        for item in toc:
            if len(item) < 3:
                continue
            level, title, page = item[:3]
            if not isinstance(page, int) or page <= 0:
                continue
            entries.append((page - 1, norm_text(str(title)), int(level)))
        entries.sort(key=lambda x: x[0])
        current = ""
        current_type = ""
        j = 0
        for p in range(len(doc)):
            while j < len(entries) and entries[j][0] <= p:
                current = entries[j][1]
                current_type = infer_unit_type(current)
                j += 1
            if current:
                result[p] = (current, current_type)
        return result

    @staticmethod
    def reconstruct_lines(page: pymupdf.Page) -> list[VisualLine]:
        raw = page.get_text("words", sort=False)
        words: list[VisualWord] = []
        for item in raw:
            if len(item) < 8:
                continue
            x0, y0, x1, y1, text, block_no, line_no, word_no = item[:8]
            text = norm_text(str(text))
            if not text:
                continue
            words.append(
                VisualWord(
                    text=text,
                    x0=float(x0), y0=float(y0), x1=float(x1), y1=float(y1),
                    block=int(block_no), line=int(line_no), word=int(word_no),
                )
            )

        if not words:
            return []

        # First use the PDF's block number to keep columns separate. Within each
        # block, cluster by baseline so fragmented PDF text objects become one
        # visual line rather than several artificial "lines".
        by_block: dict[int, list[VisualWord]] = {}
        for w in words:
            by_block.setdefault(w.block, []).append(w)

        block_order = sorted(
            by_block,
            key=lambda b: (
                min(w.y0 for w in by_block[b]),
                min(w.x0 for w in by_block[b]),
                b,
            ),
        )

        visual: list[VisualLine] = []
        for block in block_order:
            ws = sorted(by_block[block], key=lambda w: (w.y0, w.x0))
            groups: list[list[VisualWord]] = []
            boxes: list[tuple[float, float, float, float]] = []
            for w in ws:
                placed = False
                # Search the most recent few groups. This avoids accidentally
                # merging distant lines while allowing minor baseline drift.
                for gi in range(max(0, len(groups) - 4), len(groups)):
                    if line_similarity_y(boxes[gi], (w.x0, w.y0, w.x1, w.y1)):
                        groups[gi].append(w)
                        x0 = min(boxes[gi][0], w.x0)
                        y0 = min(boxes[gi][1], w.y0)
                        x1 = max(boxes[gi][2], w.x1)
                        y1 = max(boxes[gi][3], w.y1)
                        boxes[gi] = (x0, y0, x1, y1)
                        placed = True
                        break
                if not placed:
                    groups.append([w])
                    boxes.append((w.x0, w.y0, w.x1, w.y1))

            for group, box in zip(groups, boxes):
                group.sort(key=lambda w: (w.x0, w.y0, w.word))
                text = join_word_text([w.text for w in group])
                if text:
                    visual.append(
                        VisualLine(
                            page_index=page.number,
                            line_no=0,
                            block=block,
                            text=text,
                            words=group,
                            x0=box[0], y0=box[1], x1=box[2], y1=box[3],
                        )
                    )

        # Reading order: block/column position first, then vertical position.
        visual.sort(key=lambda ln: (ln.y0, ln.x0, ln.block))
        for i, ln in enumerate(visual, 1):
            ln.line_no = i
        return visual

    def index(self, force: bool = False) -> int:
        pdfs = find_pdfs(self.root)
        if not pdfs:
            raise RuntimeError(f"No PDF files were found in:\n{self.root}")

        if force:
            self.db.clear()

        total = len(pdfs)
        for n, path in enumerate(pdfs, 1):
            self.emit(f"Indexing {n}/{total}: {path.name}")
            try:
                self.index_pdf(path)
            except Exception as exc:
                self.emit(f"Skipped {path.name}: {exc}")
        self.db.rebuild_fts()
        self.db.commit()
        return total

    def index_pdf(self, path: Path):
        file_id = self.db.upsert_file(path)
        # If file was unchanged, upsert_file returns the existing row. We can
        # safely detect whether pages already exist and skip re-indexing.
        existing = self.db.conn.execute(
            "SELECT 1 FROM pages WHERE file_id=? LIMIT 1", (file_id,)).fetchone()
        if existing:
            return

        doc = pymupdf.open(str(path))
        toc_map = self.toc_context(doc)
        try:
            for page_index in range(len(doc)):
                page = doc[page_index]
                label = self.pdf_page_label(page, page_index)
                page_id = self.db.insert_page(
                    file_id, page_index, label, page.rect.width, page.rect.height)
                unit, unit_type = toc_map.get(page_index, ("", ""))
                lines = self.reconstruct_lines(page)
                for line in lines:
                    line.unit = unit
                    line.unit_type = unit_type
                    self.db.insert_line(page_id, line)
            self.db.commit()
        finally:
            doc.close()


def infer_unit_type(title: str) -> str:
    t = norm_text(title)
    if not t:
        return ""
    m = STRUCTURAL_RE.match(t)
    return m.group(1).upper() if m else "UNIT"


def token_norm(s: str) -> str:
    s = search_norm(s)
    s = re.sub(r"^[^\w]+|[^\w]+$", "", s, flags=re.UNICODE)
    return s


def split_query(query: str) -> list[list[str]]:
    """Parse comma-separated search clauses.

    Words inside one clause remain ordered. Separate clauses may occur in any
    order. For example, ``love, divine knowledge`` accepts either order of the
    two clauses, while ``divine knowledge`` preserves the phrase order.
    """
    clauses: list[list[str]] = []
    for part in query.split(","):
        part = norm_text(part)
        if not part:
            continue
        words = [x for x in WORD_TOKEN_RE.findall(part) if x]
        if words:
            clauses.append(words)
    return clauses


def flatten_query_clauses(clauses: list[list[str]]) -> list[str]:
    return [word for clause in clauses for word in clause]


def _ordered_clause_matches(
    positions: dict[str, list[int]], clause: list[str], gap: int
) -> list[tuple[int, int, tuple[int, ...]]]:
    """Find ordered occurrences of one clause against a precomputed
    token-position index.

    ``positions`` maps each normalized token value to the sorted list of
    indices where it occurs in the unit's token stream (built once by the
    caller and shared across every clause of a query, rather than
    re-normalizing the whole stream per clause). The sequence may span
    lines and PDF pages; only the structural-unit boundary is enforced by
    the caller. ``gap`` is an inclusive word-window size.
    """
    if not positions or not clause or gap < 1:
        return []

    q = [token_norm(x) for x in clause]
    q = [x for x in q if x]
    if not q:
        return []

    first_positions = positions.get(q[0], [])
    if not first_positions:
        return []

    matches: list[tuple[int, int, tuple[int, ...]]] = []
    for start in first_positions:
        current = start
        matched_positions = [start]
        ok = True
        for term in q[1:]:
            next_positions = positions.get(term, [])
            nxt = next((p for p in next_positions if p > current), None)
            if nxt is None:
                ok = False
                break
            matched_positions.append(nxt)
            current = nxt

        if not ok:
            continue
        end = current
        if end - start + 1 <= gap:
            matches.append((start, end, tuple(matched_positions)))

    return matches


def find_proximity_matches(
    tokens: list[str],
    query_clauses: list[list[str]],
    gap: int,
    max_matches: int = MAX_RESULTS,
) -> list[tuple[int, int]]:
    """Find matching word windows inside one structural unit.

    Comma-separated clauses may occur in any order; words inside each clause
    retain their typed order. Every matched occurrence must be distinct, and
    all matched words must fit inside one inclusive ``gap``-word window.
    """
    if not tokens or not query_clauses or gap < 1 or max_matches < 1:
        return []

    # Normalize the unit's token stream and index it by value exactly once,
    # then reuse that index for every clause. Comma-separated queries with
    # several clauses used to pay this O(unit length) cost again per clause;
    # for long structural units that repeated work dominated match time.
    positions: dict[str, list[int]] = {}
    for i, raw in enumerate(tokens):
        value = token_norm(raw)
        if value:
            positions.setdefault(value, []).append(i)

    clause_matches = [
        _ordered_clause_matches(positions, clause, gap)
        for clause in query_clauses
    ]
    if any(not matches for matches in clause_matches):
        return []

    order = sorted(
        range(len(clause_matches)),
        key=lambda i: (len(clause_matches[i]), -len(query_clauses[i])),
    )

    found: list[tuple[int, int]] = []
    found_set: set[tuple[int, int]] = set()

    def backtrack(
        depth: int,
        used_positions: set[int],
        low: Optional[int],
        high: Optional[int],
    ) -> None:
        if len(found) >= max_matches:
            return
        if depth == len(order):
            if low is None or high is None:
                return
            if high - low + 1 > gap:
                return
            candidate = (low, high)
            if candidate not in found_set:
                found_set.add(candidate)
                found.append(candidate)
            return

        clause_index = order[depth]
        for start, end, matched_positions in clause_matches[clause_index]:
            if len(found) >= max_matches:
                return

            matched_set = set(matched_positions)
            if used_positions.intersection(matched_set):
                continue

            new_low = start if low is None else min(low, start)
            new_high = end if high is None else max(high, end)
            if new_high - new_low + 1 > gap:
                continue

            backtrack(
                depth + 1,
                used_positions | matched_set,
                new_low,
                new_high,
            )

    backtrack(0, set(), None, None)
    found.sort(key=lambda pair: (pair[0], pair[1]))
    return found


def make_excerpt(text: str, query_words: list[str], width: int = 230) -> str:
    text = norm_text(text)
    if len(text) <= width:
        return text
    low = search_norm(text)
    needle = search_norm(query_words[0]) if query_words else ""
    idx = low.find(needle)
    if idx < 0:
        return text[:width].rstrip() + " …"
    start = max(0, idx - width // 3)
    end = min(len(text), start + width)
    prefix = "… " if start > 0 else ""
    suffix = " …" if end < len(text) else ""
    return prefix + text[start:end].strip() + suffix


class IndexWorker(QThread):
    progress = Signal(str)
    done = Signal(bool, str)

    def __init__(self, root: Path, force: bool):
        super().__init__()
        self.root = root
        self.force = force

    def run(self):
        db = None
        try:
            db = Database(library_db_path(self.root))
            indexer = PDFIndexer(db, self.root, self.progress.emit)
            count = indexer.index(self.force)
            self.done.emit(True, f"Indexed {count} PDF files.")
        except Exception as exc:
            self.done.emit(False, f"{exc}\n\n{traceback.format_exc()}")
        finally:
            if db:
                db.close()


def build_app_icon() -> QIcon:
    """A small generated glyph, so the app has a real icon with no extra
    asset files to ship or go missing on any platform."""
    size = 128
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(TEXT))
    painter.drawEllipse(4, 4, size - 8, size - 8)
    painter.setBrush(QColor(BORDER_STRONG))
    painter.drawEllipse(10, 10, size - 20, size - 20)
    painter.setPen(QColor("#fffdf9"))
    font = QFont("Georgia", int(size * 0.44), QFont.Bold)
    painter.setFont(font)
    painter.drawText(pixmap.rect(), Qt.AlignCenter, "A")
    painter.end()
    return QIcon(pixmap)


class ResultCard(QFrame):
    clicked = Signal(int)

    def __init__(self, index: int, hit: Hit, parent=None):
        super().__init__(parent)
        self.index = index
        self.hit = hit
        self.setObjectName("resultCard")
        self.setCursor(Qt.PointingHandCursor)
        self.setProperty("selected", False)
        self.setStyleSheet(self.card_style())

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(8)

        top = QHBoxLayout()
        top.setSpacing(10)
        badge = QLabel(hit.unit_type or "RESULT")
        badge.setObjectName("badge")
        top.addWidget(badge, 0)
        book = QLabel(hit.book)
        book.setObjectName("resultBook")
        top.addWidget(book, 0)
        if hit.unit:
            unit = QLabel(hit.unit)
            unit.setObjectName("resultUnit")
            top.addWidget(unit, 1)
        else:
            top.addStretch(1)
        outer.addLayout(top)

        meta = QFrame()
        meta.setObjectName("metaBox")
        ml = QHBoxLayout(meta)
        ml.setContentsMargins(12, 8, 12, 8)
        ml.setSpacing(22)
        page_value = (
            hit.pdf_page
            if hit.pdf_page == hit.end_pdf_page
            else f"{hit.pdf_page} → {hit.end_pdf_page}"
        )
        line_value = (
            str(hit.line_no)
            if hit.page_index == hit.end_page_index and hit.line_no == hit.end_line_no
            else f"{hit.line_no} → {hit.end_line_no}"
        )
        for title, value in (
            ("BOOK", hit.book),
            ("PDF PAGE", page_value),
            ("LINE", line_value),
            ("UNIT", hit.unit or "—"),
        ):
            col = QVBoxLayout()
            col.setSpacing(1)
            a = QLabel(title)
            a.setObjectName("metaLabel")
            b = QLabel(value)
            b.setObjectName("metaValue")
            b.setWordWrap(True)
            col.addWidget(a)
            col.addWidget(b)
            ml.addLayout(col, 1)
        outer.addWidget(meta)

        if hit.unit:
            context_label = QLabel(f"Context  ·  {hit.unit}")
        else:
            context_label = QLabel("Context")
        context_label.setObjectName("contextLabel")
        outer.addWidget(context_label)

        filename = QLabel(hit.file_name)
        filename.setObjectName("fileName")
        outer.addWidget(filename)

        excerpt = QLabel(make_excerpt(
            hit.context or hit.line_text, hit.query_words))
        excerpt.setObjectName("excerpt")
        excerpt.setWordWrap(True)
        excerpt.setMaximumHeight(54)
        outer.addWidget(excerpt)

    def card_style(self) -> str:
        return f"""
        QFrame#resultCard {{
            background: {CARD};
            border: 1px solid {BORDER};
            border-radius: 10px;
        }}
        QFrame#resultCard:hover {{ border: 1px solid {BORDER_STRONG}; }}
        QFrame#resultCard[selected="true"] {{ border: 2px solid {BORDER_STRONG}; background: #fbf3df; }}
        QLabel#badge {{
            background: #f1e7cf;
            color: #80622b;
            border-radius: 6px;
            padding: 4px 8px;
            font-size: 10px;
            font-weight: 700;
        }}
        QLabel#resultBook {{ font-size: 14px; font-weight: 700; color: #332f2b; }}
        QLabel#resultUnit {{ font-size: 13px; color: #6d665e; }}
        QFrame#metaBox {{ background: #faf7f0; border: 1px solid #e5dccf; border-radius: 7px; }}
        QLabel#metaLabel {{ color: #9b9083; font-size: 9px; font-weight: 800; letter-spacing: 1px; }}
        QLabel#metaValue {{ color: #3d3832; font-size: 12px; font-weight: 700; }}
        QLabel#contextLabel {{ color: #746b61; font-size: 11px; font-style: italic; }}
        QLabel#fileName {{ color: #9a9187; font-size: 10px; }}
        QLabel#excerpt {{ color: #403b35; font-size: 12px; font-weight: 600; }}
        """

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self.index)
        super().mousePressEvent(event)


class Preview(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("previewPanel")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.title = QLabel("Select a result")
        self.title.setObjectName("previewTitle")
        layout.addWidget(self.title)

        self.meta = QFrame()
        self.meta.setObjectName("previewMeta")
        ml = QHBoxLayout(self.meta)
        ml.setContentsMargins(14, 10, 14, 10)
        ml.setSpacing(26)
        self.meta_values: dict[str, QLabel] = {}
        for key in ("BOOK", "PAGE", "LINE", "UNIT"):
            box = QVBoxLayout()
            box.setSpacing(1)
            a = QLabel(key)
            a.setObjectName("previewMetaLabel")
            b = QLabel("—")
            b.setObjectName("previewMetaValue")
            box.addWidget(a)
            box.addWidget(b)
            ml.addLayout(box, 1)
            self.meta_values[key] = b
        layout.addWidget(self.meta)

        self.file_label = QLabel("")
        self.file_label.setObjectName("previewFile")
        layout.addWidget(self.file_label)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        layout.addWidget(self.scroll, 1)

        self.document = QTextBrowser()
        self.document.setOpenExternalLinks(False)
        self.document.setReadOnly(True)
        self.document.setFrameShape(QFrame.NoFrame)
        self.document.setStyleSheet(
            "QTextBrowser { background:#fffdf9; border:1px solid #e1d8cc; border-radius:10px; padding:20px; }"
        )
        self.scroll.setWidget(self.document)

        self.current: Optional[Hit] = None
        self.show_placeholder()

    def show_placeholder(self):
        self.document.setHtml(
            f"""
            <html><body style="margin:0;font-family:{FONT_STACK};color:{FAINT};
                font-size:14px;padding:6px 2px;">
            Choose a result on the left to read it here, in full context,
            with the matched words highlighted.
            </body></html>
            """
        )

    def clear(self):
        self.current = None
        self.title.setText("Select a result")
        for v in self.meta_values.values():
            v.setText("—")
        self.file_label.clear()
        self.show_placeholder()

    def show_hit(self, hit: Hit, db: Database):
        self.current = hit
        self.title.setText(
            f"{hit.book}  ·  {hit.unit or 'Result'}")
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        self.meta_values["BOOK"].setText(hit.book)
        page_value = (
            hit.pdf_page
            if hit.pdf_page == hit.end_pdf_page
            else f"{hit.pdf_page} → {hit.end_pdf_page}"
        )
        line_value = (
            str(hit.line_no)
            if hit.page_index == hit.end_page_index and hit.line_no == hit.end_line_no
            else f"{hit.line_no} → {hit.end_line_no}"
        )
        self.meta_values["PAGE"].setText(page_value)
        self.meta_values["LINE"].setText(line_value)
        self.meta_values["UNIT"].setText(hit.unit or "—")
        self.file_label.setText(hit.file_name)

        # Pull a padded window of lines around the match. Unlike a plain
        # page lookup, this always includes every line the match itself
        # spans — a wide word range can run across several visual lines and
        # even across a PDF page break — so the whole matched passage is
        # highlighted together, not just the line it happens to start on.
        window, start_idx, end_idx = db.get_hit_context_lines(
            hit.file_id, hit.unit, hit.unit_type,
            hit.page_index, hit.line_no,
            hit.end_page_index, hit.end_line_no,
        )
        if not window:
            self.document.setHtml(
                f"""
                <html><body style="margin:0;font-family:{FONT_STACK};color:{FAINT};
                    font-size:14px;padding:6px 2px;">
                This passage could not be re-located in the index — try
                re-indexing the library.
                </body></html>
                """
            )
            return

        # Build HTML: every line inside the matched range gets a shared
        # highlight band, the query terms within it are picked out more
        # strongly still, and a divider marks any PDF page change so a
        # match crossing pages stays easy to follow.
        qnorm = [token_norm(x) for x in hit.query_words]
        body: list[str] = []
        prev_page_index: Optional[int] = None
        for i, (page_index, page_label, line_no, text) in enumerate(window):
            if prev_page_index is not None and page_index != prev_page_index:
                body.append(
                    f'<div class="pagebreak">PDF page {html.escape(page_label, quote=False)}</div>'
                )
            prev_page_index = page_index
            escaped = highlight_terms(html.escape(text, quote=False), qnorm)
            css_class = "hitline" if start_idx <= i <= end_idx else "plainline"
            body.append(
                f'<div class="{css_class}"><span class="ln">{line_no}</span><span class="txt">{escaped}</span></div>'
            )

        unit_line = html.escape(hit.unit, quote=False) if hit.unit else ""
        unit_html = f'<div class="unitline">{unit_line}</div>' if unit_line else ""
        page_html = f"""
        <html><head><style>
        body {{ margin:0; background:#fffdf9; color:#292622; font-family:{FONT_STACK}; font-size:16px; font-weight:400; }}
        .head {{ background:#f5f0e7; padding:5px 8px; margin-bottom:10px; color:#8c8276; font-family:{FONT_STACK}; font-size:11px; font-weight:700; }}
        .unitline {{ margin:0 0 12px 0; color:#8c8276; font-family:{FONT_STACK}; font-size:11px; font-weight:700; }}
        .plainline,.hitline {{ display:flex; align-items:baseline; line-height:1.6; margin:0; padding:2px 0; }}
        .hitline {{ background:#fff0a6; border-radius:4px; }}
        .pagebreak {{ margin:10px 0 6px 44px; color:#a89a7f; font-family:{FONT_STACK}; font-size:10px; font-weight:700; letter-spacing:0.5px; text-transform:uppercase; border-top:1px dashed {BORDER}; padding-top:6px; }}
        .ln {{ width:34px; flex:0 0 34px; color:#b0a59a; font-family:{MONO_STACK}; font-size:9px; text-align:right; margin-right:10px; user-select:none; }}
        .txt {{ flex:1; }}
        .term {{ background:#ffd928; font-weight:700; border-radius:3px; padding:0 1px; }}
        </style></head><body>
        <div class="head">{html.escape(hit.book, quote=False)}  ·  PDF page {html.escape(page_value, quote=False)}</div>
        {unit_html}
        {''.join(body)}
        </body></html>
        """
        self.document.setHtml(page_html)
        self.document.moveCursor(QTextCursor.MoveOperation.Start)


def highlight_terms(escaped_text: str, qnorm: list[str]) -> str:
    # escaped_text is HTML-escaped, so terms containing & etc. need an escaped
    # comparison too. We use a regex over text-like word spans.
    if not qnorm:
        return escaped_text
    pattern = re.compile(r"(?<![\w])(" + "|".join(re.escape(html.escape(q, quote=False))
                         for q in sorted(qnorm, key=len, reverse=True)) + r")(?![\w])", re.I)
    return pattern.sub(r'<span class="term">\1</span>', escaped_text)


def open_in_default_app(path: str) -> None:
    system = platform.system()
    try:
        if system == "Darwin":
            subprocess.run(["open", path], check=False)
        elif system == "Windows":
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            subprocess.run(["xdg-open", path], check=False)
    except Exception as exc:
        raise RuntimeError(
            f"Could not open the file with the system viewer: {exc}") from exc


class SearchWorker(QThread):
    """Run the potentially expensive search completely off the GUI thread."""

    finished = Signal(bool, object, str, float)
    progress = Signal(str)

    def __init__(
        self,
        db_path: Path,
        clauses: list[list[str]],
        terms: list[str],
        gap: int,
        file_ids: set[int],
        file_label: str,
        parent=None,
    ):
        super().__init__(parent)
        self.db_path = db_path
        self.clauses = clauses
        self.terms = terms
        self.gap = gap
        self.file_ids = set(file_ids)
        self.file_label = file_label

    def run(self):
        db = None
        started = time.perf_counter()
        try:
            db = Database(self.db_path)
            selected_file_ids = self.file_ids
            # FTS now intersects all distinct query terms at the structural-unit
            # level. This sharply reduces the number of units that require exact
            # Python verification, especially for common words.
            candidate_units = db.search_candidates(
                self.terms,
                max(MAX_RESULTS * 2, 1000),
                file_ids=selected_file_ids,
            )

            self.progress.emit(
                f"Searching · verifying {len(candidate_units):,} candidate sections…"
            )

            def verify_units(unit_rows: list[sqlite3.Row]) -> list[Hit]:
                hits: list[Hit] = []
                seen: set[tuple[int, str, str, int, int]] = set()

                for unit_number, unit_row in enumerate(unit_rows, 1):
                    if len(hits) >= MAX_RESULTS:
                        break
                    if unit_number == 1 or unit_number % 25 == 0:
                        self.progress.emit(
                            f"Searching · section {unit_number:,}/{len(unit_rows):,}"
                        )

                    file_id = int(unit_row["file_id"])
                    unit = str(unit_row["unit"] or "")
                    unit_type = str(unit_row["unit_type"] or "")
                    stream = db.get_unit_word_stream(file_id, unit, unit_type)
                    if not stream:
                        continue

                    token_texts = [str(r["word_text"]) for r in stream]
                    matches = find_proximity_matches(
                        token_texts, self.clauses, self.gap,
                        MAX_RESULTS - len(hits),
                    )
                    if not matches:
                        continue

                    line_rows: list[tuple[int, str, int, str]] = []
                    line_seen: set[tuple[int, int]] = set()
                    for row in stream:
                        key = (int(row["page_index"]), int(row["line_no"]))
                        if key not in line_seen:
                            line_seen.add(key)
                            line_rows.append((
                                int(row["page_index"]),
                                str(row["page_label"]),
                                int(row["line_no"]),
                                str(row["line_text"]),
                            ))

                    line_index = {
                        (page_index, line_no): i
                        for i, (page_index, _, line_no, _) in enumerate(line_rows)
                    }
                    word_meta = [
                        (int(r["page_index"]), str(r["page_label"]),
                         int(r["line_no"]), str(r["line_text"]))
                        for r in stream
                    ]

                    for start_word, end_word in matches:
                        start_meta = word_meta[start_word]
                        end_meta = word_meta[end_word]
                        dedupe_key = (file_id, unit, unit_type,
                                      start_word, end_word)
                        if dedupe_key in seen:
                            continue
                        seen.add(dedupe_key)

                        start_line_idx = line_index[(
                            start_meta[0], start_meta[2])]
                        end_line_idx = line_index[(end_meta[0], end_meta[2])]
                        context_start = max(0, start_line_idx - 5)
                        context_end = min(len(line_rows), end_line_idx + 6)
                        context_parts: list[str] = []
                        for idx in range(context_start, context_end):
                            page_index, page_label, _, line_text = line_rows[idx]
                            if idx > context_start and page_index != line_rows[idx - 1][0]:
                                context_parts.append(
                                    f"[PDF page {page_label}]")
                            context_parts.append(line_text)

                        hits.append(Hit(
                            file_id=file_id,
                            file_name=str(unit_row["file_name"]),
                            book=Path(str(unit_row["path"])).stem,
                            pdf_page=start_meta[1],
                            page_index=start_meta[0],
                            line_no=start_meta[2],
                            end_pdf_page=end_meta[1],
                            end_page_index=end_meta[0],
                            end_line_no=end_meta[2],
                            unit=unit,
                            unit_type=unit_type,
                            context=" ".join(context_parts),
                            line_text=start_meta[3],
                            query_words=self.terms,
                            start_word=start_word,
                            end_word=end_word,
                        ))
                        if len(hits) >= MAX_RESULTS:
                            break
                return hits

            hits = verify_units(candidate_units)

            elapsed = time.perf_counter() - started
            self.finished.emit(True, hits, self.file_label, elapsed)
        except Exception as exc:
            elapsed = time.perf_counter() - started
            self.finished.emit(
                False, [], f"{exc}\n\n{traceback.format_exc()}", elapsed)
        finally:
            if db is not None:
                db.close()


class MainWindow(QMainWindow):
    def __init__(self, root: Path):
        super().__init__()
        self.root = root
        self.db = Database(library_db_path(root))
        self.results: list[Hit] = []
        self.selected_index: int = -1
        self.worker: Optional[IndexWorker] = None
        self.search_worker: Optional[SearchWorker] = None
        self._pending_search_after_index = False

        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(960, 620)
        self.apply_initial_geometry()
        self.build_menu()
        self.build_ui()
        self.apply_style()
        self.refresh_index_state()

        QShortcut(QKeySequence("Meta+L"), self, activated=self.query.setFocus)
        QShortcut(QKeySequence("Ctrl+L"), self, activated=self.query.setFocus)
        QShortcut(QKeySequence("Return"), self.query, activated=self.search)
        QShortcut(QKeySequence(Qt.Key.Key_Down), self,
                  activated=lambda: self.navigate_results(1))
        QShortcut(QKeySequence(Qt.Key.Key_Up), self,
                  activated=lambda: self.navigate_results(-1))
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self.query,
                  activated=self.query.clear)

    def apply_initial_geometry(self):
        """Restore the window to where the user last left it on this
        machine, or fall back to a size that comfortably fits the current
        screen — including smaller laptop displays — instead of a fixed
        1500x920 that can open partly off-screen on a smaller panel."""
        saved = load_window_geometry()
        if saved is not None and self.restoreGeometry(saved):
            screen = self.screen()
            if screen is not None and screen.availableGeometry().intersects(self.frameGeometry()):
                return
        screen = self.screen() or QApplication.primaryScreen()
        available = screen.availableGeometry() if screen else None
        if available is not None:
            width = min(1500, int(available.width() * 0.92))
            height = min(920, int(available.height() * 0.92))
            width = max(self.minimumWidth(), width)
            height = max(self.minimumHeight(), height)
            self.resize(width, height)
            self.move(
                available.x() + (available.width() - width) // 2,
                available.y() + (available.height() - height) // 2,
            )
        else:
            self.resize(1500, 920)

    # ------------------------------------------------------------------
    # Menu
    # ------------------------------------------------------------------
    def build_menu(self):
        menu_bar = self.menuBar()

        library_menu = menu_bar.addMenu("&Library")

        change_action = QAction("Change Library Folder…", self)
        change_action.setShortcut(QKeySequence("Ctrl+O"))
        change_action.triggered.connect(self.change_folder)
        library_menu.addAction(change_action)

        reindex_action = QAction("Re-index PDFs", self)
        reindex_action.setShortcut(QKeySequence("Ctrl+R"))
        reindex_action.triggered.connect(self.reindex)
        library_menu.addAction(reindex_action)

        open_action = QAction("Open Selected PDF", self)
        open_action.setShortcut(QKeySequence("Ctrl+Return"))
        open_action.triggered.connect(self.open_selected_pdf)
        library_menu.addAction(open_action)

        library_menu.addSeparator()
        quit_action = QAction("Quit", self)
        quit_action.setShortcut(QKeySequence("Ctrl+Q"))
        quit_action.triggered.connect(self.close)
        library_menu.addAction(quit_action)

        help_menu = menu_bar.addMenu("&Help")
        about_action = QAction("About", self)
        about_action.triggered.connect(self.show_about)
        help_menu.addAction(about_action)

    def show_about(self):
        QMessageBox.information(
            self,
            "About",
            f"{APP_NAME}\nVersion {APP_VERSION}\n\n"
            "Finds words or phrases within a chosen word range. The range is "
            "the total number of words in the allowed window. Separate words "
            "or phrases with commas to allow them in any order; words inside "
            "a phrase remain in the typed order. Matches may cross visual lines "
            "and PDF pages, but never cross the structural unit containing the match.",
        )

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(24, 18, 24, 8)
        root_layout.setSpacing(10)

        header = QHBoxLayout()
        header.setSpacing(10)
        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        title = QLabel("Sri Aurobindo")
        title.setObjectName("appTitle")
        title_box.addWidget(title)
        subtitle = QLabel("Collected Works  ·  Proximity Search")
        subtitle.setObjectName("subtitle")
        title_box.addWidget(subtitle)
        self.path_label = QLabel(str(self.root))
        self.path_label.setObjectName("pathLabel")
        title_box.addWidget(self.path_label)
        header.addLayout(title_box, 1)

        self.change_folder_button = QPushButton("Change Folder…")
        self.change_folder_button.setObjectName("secondaryButton")
        self.change_folder_button.clicked.connect(self.change_folder)
        header.addWidget(self.change_folder_button, 0, Qt.AlignTop)
        root_layout.addLayout(header)

        search_card = QFrame()
        search_card.setObjectName("searchCard")
        search_shadow = QGraphicsDropShadowEffect()
        search_shadow.setBlurRadius(24)
        search_shadow.setOffset(0, 3)
        search_shadow.setColor(QColor(40, 32, 20, 26))
        search_card.setGraphicsEffect(search_shadow)
        sl = QVBoxLayout(search_card)
        sl.setContentsMargins(16, 14, 16, 14)
        sl.setSpacing(8)
        hint = QLabel(
            "Use commas between distinct words/phrases. Example: love, divine knowledge. "
            "Comma-separated clauses may appear in any order; words inside a phrase stay in the order typed. "
            "Word range is the total inclusive window containing all matched words. "
            "You can also select one or more PDFs. Results may cross lines and PDF pages, but never a structural-unit boundary.")
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        sl.addWidget(hint)

        controls = QHBoxLayout()
        controls.setSpacing(8)
        self.query = QLineEdit()
        self.query.setPlaceholderText("e.g. love, divine knowledge")
        self.query.setClearButtonEnabled(True)
        self.query.setObjectName("query")
        controls.addWidget(self.query, 1)

        gap_label = QLabel("Word range")
        gap_label.setObjectName("gapLabel")
        controls.addWidget(gap_label)

        self.minus = QPushButton("−")
        self.minus.setObjectName("stepButton")
        self.minus.setToolTip("Decrease the allowed word range")
        self.minus.clicked.connect(
            lambda: self.gap.setValue(max(1, self.gap.value() - 1)))
        controls.addWidget(self.minus)

        self.gap = QSpinBox()
        self.gap.setRange(1, 500)
        self.gap.setValue(load_last_gap())
        self.gap.setSuffix(" words")
        self.gap.setButtonSymbols(QSpinBox.NoButtons)
        self.gap.setObjectName("gapValue")
        self.gap.valueChanged.connect(save_last_gap)
        controls.addWidget(self.gap)

        self.plus = QPushButton("+")
        self.plus.setObjectName("stepButton")
        self.plus.setToolTip("Increase the allowed word range")
        self.plus.clicked.connect(
            lambda: self.gap.setValue(min(500, self.gap.value() + 1)))
        controls.addWidget(self.plus)

        file_label = QLabel("Files")
        file_label.setObjectName("fileFilterLabel")
        controls.addWidget(file_label)

        self.selected_file_ids: set[int] = set()
        self.selected_file_paths: set[str] = set()
        self.file_filter = QToolButton()
        self.file_filter.setObjectName("fileFilter")
        self.file_filter.setMinimumWidth(220)
        self.file_filter.setMaximumWidth(330)
        self.file_filter.setText("All indexed PDFs")
        self.file_filter.setToolTip("Select one or more PDFs to search")
        self.file_filter.clicked.connect(self.open_file_filter_dialog)
        controls.addWidget(self.file_filter)

        self.search_button = QPushButton("Search")
        self.search_button.setObjectName("searchButton")
        self.search_button.clicked.connect(self.search)
        controls.addWidget(self.search_button)
        sl.addLayout(controls)
        root_layout.addWidget(search_card)

        status_row = QHBoxLayout()
        status_row.setSpacing(10)
        self.status = QLabel("")
        self.status.setObjectName("status")
        status_row.addWidget(self.status, 1)
        self.progress = QProgressBar()
        self.progress.setObjectName("progress")
        self.progress.setRange(0, 0)  # indeterminate "busy" style
        self.progress.setFixedWidth(160)
        self.progress.setFixedHeight(6)
        self.progress.setTextVisible(False)
        self.progress.hide()
        status_row.addWidget(self.progress, 0)
        root_layout.addLayout(status_row)

        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(5)
        root_layout.addWidget(self.splitter, 1)

        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(6, 0, 6, 0)
        ll.setSpacing(8)
        self.result_count = QLabel("0 results")
        self.result_count.setObjectName("resultCount")
        ll.addWidget(self.result_count)

        self.results_scroll = QScrollArea()
        self.results_scroll.setWidgetResizable(True)
        self.results_scroll.setFrameShape(QFrame.NoFrame)
        self.results_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.results_container = QWidget()
        self.results_layout = QVBoxLayout(self.results_container)
        self.results_layout.setContentsMargins(0, 0, 8, 0)
        self.results_layout.setSpacing(9)
        self.results_layout.addStretch()
        self.results_scroll.setWidget(self.results_container)
        ll.addWidget(self.results_scroll, 1)
        self.splitter.addWidget(left)

        self.preview = Preview()
        self.splitter.addWidget(self.preview)
        saved_splitter = load_splitter_state()
        if saved_splitter is None or not self.splitter.restoreState(saved_splitter):
            self.splitter.setSizes([560, 900])

        footer = QHBoxLayout()
        self.footer = QLabel("Ready")
        self.footer.setObjectName("footer")
        footer.addWidget(self.footer, 1)
        self.reindex_button = QPushButton("Re-index PDFs")
        self.reindex_button.setObjectName("secondaryButton")
        self.reindex_button.clicked.connect(self.reindex)
        footer.addWidget(self.reindex_button)
        self.open_button = QPushButton("Open PDF")
        self.open_button.setObjectName("secondaryButton")
        self.open_button.clicked.connect(self.open_selected_pdf)
        footer.addWidget(self.open_button)
        root_layout.addLayout(footer)

        self.show_empty_state("Type a search above and press Enter.")

    def apply_style(self):
        self.setStyleSheet(
            f"""
            QMainWindow, QWidget {{ background:{BG}; color:{TEXT}; font-family:{FONT_STACK}; }}
            QMenuBar {{ background:{BG}; color:{TEXT}; font-family:{FONT_STACK}; font-size:12px; }}
            QMenuBar::item:selected {{ background:{CARD_ALT}; }}
            QMenu {{ background:{CARD}; color:{TEXT}; border:1px solid {BORDER}; }}
            QMenu::item:selected {{ background:#f1e7cf; }}
            QLabel#appTitle {{ font-size:30px; font-weight:800; color:#272421; }}
            QLabel#subtitle {{ font-size:16px; font-weight:700; color:#776f66; }}
            QLabel#pathLabel {{ font-family:{MONO_STACK}; font-size:10px; color:#8f857a; }}
            QFrame#searchCard {{ background:{CARD}; border:1px solid {BORDER}; border-radius:12px; }}
            QLabel#hint {{ font-size:13px; color:#655e56; font-weight:600; }}
            QLineEdit#query {{ background:#fff; border:1px solid #cfc4b5; border-radius:9px; padding:9px 12px; font-size:15px; color:#25221f; selection-background-color:#d7b35d; }}
            QLineEdit#query:focus {{ border:2px solid {BORDER_STRONG}; padding:8px 11px; }}
            QLabel#gapLabel, QLabel#fileFilterLabel {{ color:#7b7269; font-size:12px; font-weight:700; }}
            QPushButton#stepButton {{ background:#fffdf9; border:1px solid #d5cabd; border-radius:8px; min-width:34px; min-height:34px; max-width:34px; font-size:18px; font-weight:700; color:#4a443e; }}
            QPushButton#stepButton:hover {{ background:#f4ede2; border-color:{BORDER_STRONG}; }}
            QSpinBox#gapValue {{ background:#fffdf9; border:1px solid #d5cabd; border-radius:8px; min-height:34px; padding:0 10px; min-width:88px; font-size:12px; font-weight:700; color:#403a34; }}
            QToolButton#fileFilter {{ background:#fffdf9; border:1px solid #d5cabd; border-radius:8px; min-height:34px; padding:0 12px; min-width:220px; max-width:330px; font-size:12px; font-weight:700; color:#403a34; text-align:left; }}
            QToolButton#fileFilter:hover {{ border-color:{BORDER_STRONG}; background:#f8f1e6; }}
            QToolButton#fileFilter:pressed {{ background:#f1e7cf; }}
            QListWidget {{ background:#fffdf9; border:1px solid #d5cabd; border-radius:8px; color:#302d29; padding:4px; }}
            QListWidget::item {{ padding:6px 4px; }}
            QListWidget::item:hover {{ background:#f8f1e6; }}
            QPushButton#searchButton {{ background:#3d3935; color:white; border:none; border-radius:8px; min-height:36px; min-width:110px; font-size:13px; font-weight:800; }}
            QPushButton#searchButton:hover {{ background:#282521; }}
            QPushButton#secondaryButton {{ background:#fffdf9; color:#3f3932; border:1px solid #d5cabd; border-radius:8px; padding:7px 14px; font-size:12px; font-weight:700; }}
            QPushButton#secondaryButton:hover {{ border-color:{BORDER_STRONG}; background:#f8f1e6; }}
            QLabel#status {{ color:#71685e; font-size:12px; font-weight:600; padding:0 4px; }}
            QProgressBar#progress {{ background:#e9e0d1; border:none; border-radius:3px; }}
            QProgressBar#progress::chunk {{ background:{BORDER_STRONG}; border-radius:3px; }}
            QLabel#resultCount {{ color:#4d4741; font-size:13px; font-weight:800; }}
            QLabel#footer {{ color:#81776d; font-size:11px; }}
            QLabel#emptyState {{ color:{FAINT}; font-size:13px; font-weight:600; padding:36px 18px; background:{CARD_ALT}; border:1px dashed {BORDER}; border-radius:12px; }}
            QScrollBar:vertical {{ width:9px; background:transparent; margin:2px; }}
            QScrollBar::handle:vertical {{ background:#cfc5b9; border-radius:4px; min-height:30px; }}
            QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical {{ height:0; }}
            QSplitter::handle {{ background:#c9beb0; }}
            QFrame#previewPanel {{ background:transparent; }}
            QLabel#previewTitle {{ font-size:20px; font-weight:800; color:#302c28; padding:0 0 2px 0; }}
            QFrame#previewMeta {{ background:#fffdf9; border:1px solid {BORDER}; border-radius:10px; }}
            QLabel#previewMetaLabel {{ color:#9b9083; font-size:9px; font-weight:800; letter-spacing:1px; }}
            QLabel#previewMetaValue {{ color:#3d3832; font-size:13px; font-weight:800; }}
            QLabel#previewFile {{ color:#8e857b; font-family:{MONO_STACK}; font-size:10px; padding-left:2px; }}
            """
        )

    # ------------------------------------------------------------------
    # Results list helpers
    # ------------------------------------------------------------------
    def refresh_index_state(self):
        count = self.db.conn.execute(
            "SELECT COUNT(*) FROM files").fetchone()[0]
        if count:
            self.footer.setText(f"Index ready · {count} PDF files")
        else:
            self.footer.setText("No index yet")
        self.refresh_file_filter()

    def refresh_file_filter(self):
        """Refresh the displayed selection after indexing or a folder change."""
        if not hasattr(self, "file_filter"):
            return
        valid = {str(r["path"]): int(r["id"]) for r in self.db.list_files()}
        self.selected_file_paths &= set(valid)
        self.selected_file_ids = {valid[path]
                                  for path in self.selected_file_paths}
        self.update_file_filter_button()

    def update_file_filter_button(self):
        count = len(self.selected_file_ids)
        if count == 0:
            self.file_filter.setText("All indexed PDFs")
        elif count == 1:
            row = self.db.conn.execute(
                "SELECT name FROM files WHERE id=?", (next(
                    iter(self.selected_file_ids)),)
            ).fetchone()
            self.file_filter.setText(str(row[0]) if row else "1 PDF selected")
        else:
            self.file_filter.setText(f"{count} PDFs selected")

    def open_file_filter_dialog(self):
        """Let the user check any number of PDFs, then apply the selection."""
        dialog = QDialog(self)
        dialog.setWindowTitle("Choose PDFs")
        dialog.setModal(True)
        dialog.setMinimumSize(520, 520)

        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        heading = QLabel("Search these PDFs")
        heading.setStyleSheet("font-size:16px;font-weight:800;color:#302d29;")
        layout.addWidget(heading)

        hint = QLabel(
            "Select one, several, or none. Leaving all unchecked searches the entire library.")
        hint.setWordWrap(True)
        hint.setStyleSheet("font-size:12px;color:#766f66;")
        layout.addWidget(hint)

        select_row = QHBoxLayout()
        select_all_btn = QPushButton("Select all")
        clear_all_btn = QPushButton("Clear all")
        select_row.addWidget(select_all_btn)
        select_row.addWidget(clear_all_btn)
        select_row.addStretch(1)
        layout.addLayout(select_row)

        list_widget = QListWidget()
        list_widget.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        layout.addWidget(list_widget, 1)

        rows = self.db.list_files()
        selected_paths = set(self.selected_file_paths)
        for row in rows:
            item = QListWidgetItem(str(row["name"]))
            item.setToolTip(str(row["path"]))
            item.setData(Qt.ItemDataRole.UserRole,
                         (int(row["id"]), str(row["path"])))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            state = (Qt.CheckState.Checked if str(row["path"]) in selected_paths
                     else Qt.CheckState.Unchecked)
            item.setCheckState(state)
            list_widget.addItem(item)

        def set_all(checked: bool):
            state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
            for i in range(list_widget.count()):
                list_widget.item(i).setCheckState(state)

        select_all_btn.clicked.connect(lambda: set_all(True))
        clear_all_btn.clicked.connect(lambda: set_all(False))

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel_btn = QPushButton("Cancel")
        apply_btn = QPushButton("Apply")
        apply_btn.setDefault(True)
        buttons.addWidget(cancel_btn)
        buttons.addWidget(apply_btn)
        layout.addLayout(buttons)

        cancel_btn.clicked.connect(dialog.reject)

        def apply_selection():
            ids: set[int] = set()
            paths: set[str] = set()
            for i in range(list_widget.count()):
                item = list_widget.item(i)
                if item.checkState() == Qt.CheckState.Checked:
                    file_id, path = item.data(Qt.ItemDataRole.UserRole)
                    ids.add(int(file_id))
                    paths.add(str(path))
            self.selected_file_ids = ids
            self.selected_file_paths = paths
            self.update_file_filter_button()
            dialog.accept()

        apply_btn.clicked.connect(apply_selection)
        dialog.exec()

    def clear_results(self):
        while self.results_layout.count() > 1:
            item = self.results_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        self.results = []
        self.selected_index = -1
        self.result_count.setText("0 results")
        self.preview.clear()

    def show_empty_state(self, message: str):
        label = QLabel(message)
        label.setObjectName("emptyState")
        label.setAlignment(Qt.AlignCenter)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        self.results_layout.insertWidget(0, label)

    # ------------------------------------------------------------------
    # Library folder management
    # ------------------------------------------------------------------
    def change_folder(self):
        if self.search_worker and self.search_worker.isRunning():
            QMessageBox.information(
                self, "Please wait", "A search is still in progress.")
            return
        if self.worker and self.worker.isRunning():
            QMessageBox.information(
                self, "Please wait", "Indexing is still in progress.")
            return
        new_root = choose_folder(self.root)
        if new_root is None:
            return
        if new_root.resolve() == self.root.resolve():
            return
        self.db.close()
        self.root = new_root
        save_last_root(self.root)
        self.db = Database(library_db_path(self.root))
        self.path_label.setText(str(self.root))
        self.clear_results()
        self.show_empty_state("Type a search above and press Enter.")
        self.refresh_index_state()
        if not self.db.conn.execute("SELECT 1 FROM files LIMIT 1").fetchone():
            self.reindex()

    # ------------------------------------------------------------------
    # Indexing
    # ------------------------------------------------------------------
    def reindex(self):
        if self.search_worker and self.search_worker.isRunning():
            return
        if self.worker and self.worker.isRunning():
            return
        self.clear_results()
        self.reindex_button.setEnabled(False)
        self.search_button.setEnabled(False)
        self.change_folder_button.setEnabled(False)
        self.status.setText("Indexing PDFs…")
        self.progress.show()
        self.worker = IndexWorker(self.root, True)
        self.worker.progress.connect(self.status.setText)
        self.worker.done.connect(self.index_finished)
        self.worker.start()

    def index_finished(self, ok: bool, message: str):
        self.reindex_button.setEnabled(True)
        self.search_button.setEnabled(True)
        self.change_folder_button.setEnabled(True)
        self.progress.hide()
        if ok:
            self.status.setText(message)
            self.db.close()
            self.db = Database(library_db_path(self.root))
            self.refresh_index_state()
            if self.query.text().strip():
                self.search()
        else:
            self.status.setText("Indexing failed")
            QMessageBox.critical(self, "Indexing error", message)

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------
    def search(self):
        if self.search_worker and self.search_worker.isRunning():
            return

        query = norm_text(self.query.text())
        if not query:
            return

        clauses = split_query(query)
        if not clauses:
            return
        terms = flatten_query_clauses(clauses)

        if not self.db.conn.execute("SELECT 1 FROM files LIMIT 1").fetchone():
            self.reindex()
            return

        selected_file_ids = set(self.selected_file_ids)
        selected_file_name = (
            "All indexed PDFs" if not selected_file_ids else self.file_filter.text()
        )
        gap = self.gap.value()

        self.clear_results()
        self.search_button.setEnabled(False)
        self.reindex_button.setEnabled(False)
        self.change_folder_button.setEnabled(False)
        self.file_filter.setEnabled(False)
        self.minus.setEnabled(False)
        self.plus.setEnabled(False)
        self.gap.setEnabled(False)
        self.status.setText(f"Searching · {selected_file_name}…")
        self.progress.show()

        self.search_worker = SearchWorker(
            library_db_path(self.root),
            clauses,
            terms,
            gap,
            selected_file_ids,
            selected_file_name,
            self,
        )
        self.search_worker.progress.connect(self.status.setText)
        self.search_worker.finished.connect(self.search_finished)
        self.search_worker.start()

    def search_finished(self, ok: bool, hits_obj: object, label: str, elapsed: float):
        self.search_button.setEnabled(True)
        self.reindex_button.setEnabled(True)
        self.change_folder_button.setEnabled(True)
        self.file_filter.setEnabled(True)
        self.minus.setEnabled(True)
        self.plus.setEnabled(True)
        self.gap.setEnabled(True)
        self.progress.hide()

        hits = list(hits_obj) if isinstance(hits_obj, list) else []
        if not ok:
            self.status.setText("Search failed")
            QMessageBox.critical(self, "Search error", label)
            return

        self.results = hits
        query = norm_text(self.query.text())
        if hits:
            for i, hit in enumerate(hits):
                card = ResultCard(i, hit)
                card.clicked.connect(self.select_result)
                self.results_layout.insertWidget(
                    self.results_layout.count() - 1, card)
        else:
            self.show_empty_state(
                f'No matches for "{query}". Try a larger word range or check the spelling.'
            )

        self.result_count.setText(
            f"{len(hits)} result" + ("s" if len(hits) != 1 else "")
        )
        self.status.setText(
            f"Search complete · {len(hits)} matching locations · {elapsed:.2f}s · {label}"
        )
        if hits:
            self.select_result(0)

        self.search_worker = None

    def select_result(self, index: int):
        if not (0 <= index < len(self.results)):
            return
        self.selected_index = index
        selected_widget: Optional[QWidget] = None
        for i in range(self.results_layout.count() - 1):
            item = self.results_layout.itemAt(i)
            widget = item.widget()
            if isinstance(widget, ResultCard):
                widget.setProperty("selected", widget.index == index)
                widget.style().unpolish(widget)
                widget.style().polish(widget)
                if widget.index == index:
                    selected_widget = widget
        self.preview.show_hit(self.results[index], self.db)
        if selected_widget is not None:
            self.results_scroll.ensureWidgetVisible(selected_widget, 0, 12)

    def navigate_results(self, delta: int):
        """Move the selection up or down one result (bound to Up/Down)."""
        if not self.results:
            return
        current = self.selected_index if self.selected_index >= 0 else -1
        new_index = min(len(self.results) - 1, max(0, current + delta))
        self.select_result(new_index)

    def open_selected_pdf(self):
        if not self.results:
            return
        hit = self.preview.current or self.results[0]
        row = self.db.conn.execute(
            "SELECT path FROM files WHERE id=?", (hit.file_id,)).fetchone()
        if not row:
            return
        path = str(row[0])
        try:
            open_in_default_app(path)
        except RuntimeError as exc:
            QMessageBox.warning(self, "Could not open PDF", str(exc))

    def closeEvent(self, event):
        save_window_geometry(self.saveGeometry())
        save_splitter_state(self.splitter.saveState())
        if self.search_worker and self.search_worker.isRunning():
            self.search_worker.quit()
            self.search_worker.wait(1500)
        if self.worker and self.worker.isRunning():
            self.worker.quit()
            self.worker.wait(1500)
        self.db.close()
        super().closeEvent(event)


def choose_folder(start_dir: Optional[Path] = None) -> Optional[Path]:
    selected = QFileDialog.getExistingDirectory(
        None,
        "Choose the folder containing the collected works",
        str(start_dir) if start_dir else str(Path.home()),
        QFileDialog.Option.ShowDirsOnly,
    )
    return Path(selected).expanduser() if selected else None


def ensure_windows_taskbar_identity():
    """On Windows, a Python script normally inherits the generic Python
    launcher's taskbar grouping and icon. Registering a distinct App User
    Model ID makes Windows treat this as its own application instead —
    correct taskbar icon, grouping, and pinning behaviour. No-op, and safe
    to call, on every other platform."""
    if platform.system() != "Windows":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            f"{SETTINGS_ORG}.{SETTINGS_APP}.{APP_VERSION}"
        )
    except Exception:
        pass  # Cosmetic only — never let this stop the app from starting.


def main():
    ensure_windows_taskbar_identity()
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(SETTINGS_ORG)
    app.setStyle("Fusion")
    app.setWindowIcon(build_app_icon())

    # Remember the last folder the user picked (per machine, via QSettings)
    # so the app opens straight into the library on every platform without
    # re-prompting. A first run — or a one-time legacy default location from
    # older versions of this script — falls back to the folder picker.
    root = load_last_root()
    if root is None:
        legacy_default = Path.home() / "Desktop" / "MISC" / \
            "Collected Works of Sri Aurobindo"
        root = legacy_default if legacy_default.is_dir() else None
    if root is None:
        root = choose_folder()
    if root is None:
        return 0
    save_last_root(root)

    window = MainWindow(root)
    window.show()
    if not window.db.conn.execute("SELECT 1 FROM files LIMIT 1").fetchone():
        window.reindex()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
