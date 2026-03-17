"""Markdown / plain-text editor widget with Markup ⇄ Formatted toggle."""

import logging
import re
from pathlib import Path

import markdown2
from markdownify import markdownify as html_to_md
from platformdirs import user_data_dir
from spellchecker import SpellChecker

from PyQt6.QtCore import Qt, QEvent, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QAction,
    QFont,
    QKeySequence,
    QShortcut,
    QSyntaxHighlighter,
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
    QTextListFormat,
)
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

_APP_NAME = "VeritasReader"
_USER_DICT_PATH = Path(user_data_dir(_APP_NAME)) / "user_dictionary.txt"
_WORD_RE = re.compile(r"\b[A-Za-z]+(?:'[A-Za-z]+)*\b")

# Patterns stripped before word counting so markdown syntax isn't counted
_MD_STRIP = [
    (re.compile(r"```.*?```", re.DOTALL), " "),   # fenced code blocks
    (re.compile(r"`[^`\n]+`"), " "),               # inline code
    (re.compile(r"^#{1,6}\s+", re.MULTILINE), ""), # heading markers
    (re.compile(r"^[-*_]{3,}\s*$", re.MULTILINE), ""),  # horizontal rules
    (re.compile(r"!?\[([^\]]*)\]\([^\)]*\)"), r"\1"),   # links/images → label only
    (re.compile(r"\*{1,3}|_{1,3}|~~"), ""),        # bold/italic/strikethrough markers
    (re.compile(r"^>\s?", re.MULTILINE), ""),      # blockquote markers
    (re.compile(r"^\s*[-*+]\s+", re.MULTILINE), ""), # list bullets
    (re.compile(r"^\s*\d+\.\s+", re.MULTILINE), ""), # numbered list markers
]
_WCOUNT_RE = re.compile(r"[A-Za-z\u00C0-\u024F]+(?:[''\-][A-Za-z]+)*")

logger = logging.getLogger(__name__)

_STYLE_ITEMS = ["Normal text", "Heading 1", "Heading 2", "Heading 3"]

# ---------------------------------------------------------------------------
# Spell-checking helpers
# ---------------------------------------------------------------------------

def _load_spell_checker() -> SpellChecker:
    """Load US-English SpellChecker, merging the user's custom word list."""
    spell = SpellChecker(language="en")
    if _USER_DICT_PATH.exists():
        words = _USER_DICT_PATH.read_text(encoding="utf-8").split()
        if words:
            spell.word_frequency.load_words(words)
    return spell


def _add_to_user_dict(word: str, spell: SpellChecker) -> None:
    """Persist a word to the user dictionary and update the live checker."""
    word = word.lower()
    spell.word_frequency.load_words([word])
    _USER_DICT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _USER_DICT_PATH.open("a", encoding="utf-8") as f:
        f.write(word + "\n")
    logger.debug("Added '%s' to user dictionary", word)


class SpellCheckHighlighter(QSyntaxHighlighter):
    """Underlines misspelled words in red.  Skips short tokens and ALL-CAPS."""

    def __init__(self, document: QTextDocument, spell: SpellChecker) -> None:
        super().__init__(document)
        self._spell = spell
        self._fmt = QTextCharFormat()
        self._fmt.setUnderlineStyle(QTextCharFormat.UnderlineStyle.SpellCheckUnderline)
        self._fmt.setUnderlineColor(Qt.GlobalColor.red)

    def highlightBlock(self, text: str) -> None:
        for m in _WORD_RE.finditer(text):
            word = m.group()
            if len(word) < 3 or word.isupper():
                continue
            if self._spell.unknown([word]):
                self.setFormat(m.start(), len(word), self._fmt)

    def rehighlight_soon(self) -> None:
        """Re-run highlighting (e.g. after user dict update)."""
        self.rehighlight()


class SpellCheckEdit(QPlainTextEdit):
    """QPlainTextEdit with spell-check right-click context menu."""

    def __init__(self, spell: SpellChecker, parent=None) -> None:
        super().__init__(parent)
        self._spell = spell
        self._spell_highlighter: SpellCheckHighlighter | None = None

    def set_spell_highlighter(self, h: "SpellCheckHighlighter") -> None:
        self._spell_highlighter = h

    def contextMenuEvent(self, event) -> None:
        menu = self.createStandardContextMenu()
        cursor = self.cursorForPosition(event.pos())
        cursor.select(QTextCursor.SelectionType.WordUnderCursor)
        word = cursor.selectedText()

        if word and len(word) >= 3 and self._spell.unknown([word]):
            menu.addSeparator()
            candidates = sorted(self._spell.candidates(word) or [])[:6]
            if candidates:
                for suggestion in candidates:
                    act = QAction(f"→ {suggestion}", menu)
                    act.triggered.connect(
                        lambda _, s=suggestion, c=cursor: (
                            c.insertText(s),
                            self.setTextCursor(c),
                        )
                    )
                    menu.addAction(act)
            else:
                no_act = QAction("(No suggestions)", menu)
                no_act.setEnabled(False)
                menu.addAction(no_act)

            menu.addSeparator()
            add_act = QAction(f'Add "{word}" to dictionary', menu)
            add_act.triggered.connect(lambda _, w=word: self._add_word(w))
            menu.addAction(add_act)

        menu.exec(event.globalPos())

    def _add_word(self, word: str) -> None:
        _add_to_user_dict(word, self._spell)
        if self._spell_highlighter:
            self._spell_highlighter.rehighlight_soon()
# Font sizes for formatted-mode headings
_HEADING_SIZES = {0: 12, 1: 24, 2: 18, 3: 14}


class EditorWidget(QWidget):
    """Editor widget with Markup (raw markdown) and Formatted (rich text) modes.

    Toolbar buttons work in both modes.  Switching modes converts content
    bi-directionally using markdown2 and markdownify.

    Signals:
        text_changed: Emitted whenever the document content changes.
    """

    text_changed = pyqtSignal()
    grammar_check_requested = pyqtSignal(str)   # emits editor text

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._mode = "markup"  # "markup" | "formatted"
        self._last_markup_text: str = ""  # saved before switching to formatted
        self._formatted_dirty: bool = False  # True only if user edited in formatted mode
        self._spell = _load_spell_checker()
        self._build_ui()

    # ------------------------------------------------------------------
    # Build UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        # --- Formatting toolbar ---
        toolbar = QWidget()
        tl = QHBoxLayout(toolbar)
        tl.setContentsMargins(2, 2, 2, 2)
        tl.setSpacing(4)

        # Style dropdown
        self._style_combo = QComboBox()
        self._style_combo.addItems(_STYLE_ITEMS)
        self._style_combo.setFixedWidth(120)
        self._style_combo.setToolTip("Paragraph style")
        self._style_combo.currentIndexChanged.connect(self._on_style_changed)
        tl.addWidget(self._style_combo)

        tl.addSpacing(6)

        # Bold
        bold_btn = QPushButton("B")
        bold_btn.setFixedWidth(28)
        bold_btn.setToolTip("Bold")
        bold_btn.setStyleSheet("font-weight: bold;")
        bold_btn.clicked.connect(self._toggle_bold)
        tl.addWidget(bold_btn)

        # Italic
        italic_btn = QPushButton("I")
        italic_btn.setFixedWidth(28)
        italic_btn.setToolTip("Italic")
        italic_btn.setStyleSheet("font-style: italic;")
        italic_btn.clicked.connect(self._toggle_italic)
        tl.addWidget(italic_btn)

        # Strikethrough
        strike_btn = QPushButton("S")
        strike_btn.setFixedWidth(28)
        strike_btn.setToolTip("Strikethrough")
        strike_btn.setStyleSheet("text-decoration: line-through;")
        strike_btn.clicked.connect(self._toggle_strikethrough)
        tl.addWidget(strike_btn)

        # Separator
        sep1 = QFrame()
        sep1.setFrameShape(QFrame.Shape.VLine)
        sep1.setFrameShadow(QFrame.Shadow.Sunken)
        tl.addWidget(sep1)

        # Blockquote / indent block
        quote_btn = QPushButton('"')
        quote_btn.setFixedWidth(28)
        quote_btn.setToolTip("Blockquote")
        quote_btn.clicked.connect(self._toggle_quote)
        tl.addWidget(quote_btn)

        # Bullet list
        bullet_btn = QPushButton("•")
        bullet_btn.setFixedWidth(28)
        bullet_btn.setToolTip("Bullet list")
        bullet_btn.clicked.connect(self._toggle_bullet)
        tl.addWidget(bullet_btn)

        # Increase indent
        indent_btn = QPushButton("→|")
        indent_btn.setFixedWidth(32)
        indent_btn.setToolTip("Increase indent  (Tab)")
        indent_btn.clicked.connect(self._increase_indent)
        tl.addWidget(indent_btn)

        # Decrease indent
        outdent_btn = QPushButton("|←")
        outdent_btn.setFixedWidth(32)
        outdent_btn.setToolTip("Decrease indent  (Shift+Tab)")
        outdent_btn.clicked.connect(self._decrease_indent)
        tl.addWidget(outdent_btn)

        # Horizontal divider
        div_btn = QPushButton("—")
        div_btn.setFixedWidth(28)
        div_btn.setToolTip("Horizontal divider")
        div_btn.clicked.connect(self._insert_divider)
        tl.addWidget(div_btn)

        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.VLine)
        sep2.setFrameShadow(QFrame.Shadow.Sunken)
        tl.addWidget(sep2)

        grammar_btn = QPushButton("Grammar")
        grammar_btn.setToolTip("Check grammar via AI (opens AI panel)")
        grammar_btn.clicked.connect(self._on_check_grammar)
        tl.addWidget(grammar_btn)

        find_btn = QPushButton("Find")
        find_btn.setToolTip("Find / Replace  (Ctrl+F / Ctrl+H)")
        find_btn.clicked.connect(lambda: self._show_find(replace=False))
        tl.addWidget(find_btn)

        tl.addStretch()

        # Mode toggle
        self._mode_btn = QPushButton("Formatted")
        self._mode_btn.setToolTip("Switch to formatted (rich text) view")
        self._mode_btn.setCheckable(True)
        self._mode_btn.setChecked(False)
        self._mode_btn.clicked.connect(self._toggle_mode)
        tl.addWidget(self._mode_btn)

        layout.addWidget(toolbar)

        # --- Find / Replace bar (hidden by default) ---
        self._find_bar = self._build_find_bar()
        self._find_bar.hide()
        layout.addWidget(self._find_bar)

        # Keyboard shortcuts
        find_sc = QShortcut(QKeySequence.StandardKey.Find, self)
        find_sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        find_sc.activated.connect(lambda: self._show_find(replace=False))

        replace_sc = QShortcut(QKeySequence("Ctrl+H"), self)
        replace_sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        replace_sc.activated.connect(lambda: self._show_find(replace=True))

        # --- Stacked editor panes ---
        self._stack = QStackedWidget()

        # Page 0: Markup (SpellCheckEdit — QPlainTextEdit subclass)
        self._markup_editor = SpellCheckEdit(self._spell)
        self._markup_editor.setFont(QFont("Monospace", 11))
        self._markup_editor.setPlaceholderText(
            "Import a file, paste text, or generate content with AI to get started..."
        )
        self._markup_editor.textChanged.connect(self.text_changed)
        self._markup_editor.cursorPositionChanged.connect(self._update_style_combo)
        self._markup_editor.installEventFilter(self)
        markup_hl = SpellCheckHighlighter(self._markup_editor.document(), self._spell)
        self._markup_editor.set_spell_highlighter(markup_hl)

        # Page 1: Formatted (QTextEdit)
        self._rich_editor = QTextEdit()
        self._rich_editor.setFont(QFont("Georgia", 12))
        self._rich_editor.setPlaceholderText(
            "Formatted view — edit here or switch to Markup to see raw text."
        )
        self._rich_editor.textChanged.connect(self._on_rich_text_changed)
        self._rich_editor.cursorPositionChanged.connect(self._update_style_combo)
        SpellCheckHighlighter(self._rich_editor.document(), self._spell)

        self._stack.addWidget(self._markup_editor)   # index 0
        self._stack.addWidget(self._rich_editor)     # index 1
        layout.addWidget(self._stack, stretch=1)

        # --- Word-count status strip ---
        wc_bar = QWidget()
        wcl = QHBoxLayout(wc_bar)
        wcl.setContentsMargins(6, 1, 6, 1)
        self._word_count_label = QLabel("0 words")
        self._word_count_label.setStyleSheet("color: gray; font-size: 10px;")
        wcl.addWidget(self._word_count_label)
        wcl.addStretch()
        layout.addWidget(wc_bar)

        # Debounce timer so large pastes don't stall the UI
        self._wc_timer = QTimer()
        self._wc_timer.setSingleShot(True)
        self._wc_timer.setInterval(400)
        self._wc_timer.timeout.connect(self._refresh_word_count)
        self._markup_editor.textChanged.connect(self._wc_timer.start)
        self._rich_editor.textChanged.connect(self._wc_timer.start)

    # ------------------------------------------------------------------
    # Mode toggle
    # ------------------------------------------------------------------

    def _toggle_mode(self) -> None:
        if self._mode == "markup":
            self._switch_to_formatted()
        else:
            self._switch_to_markup()

    def _switch_to_formatted(self) -> None:
        md = self._markup_editor.toPlainText()
        self._last_markup_text = md
        self._formatted_dirty = False
        html = markdown2.markdown(
            md,
            extras=["tables", "fenced-code-blocks", "header-ids"],
        )
        self._rich_editor.blockSignals(True)
        self._rich_editor.setHtml(html)
        self._rich_editor.blockSignals(False)
        self._stack.setCurrentIndex(1)
        self._mode = "formatted"
        self._mode_btn.setText("Markup")
        self._mode_btn.setToolTip("Switch to markup (raw text) view")
        self._mode_btn.setChecked(True)

    def _switch_to_markup(self) -> None:
        if self._formatted_dirty:
            html = self._rich_editor.toHtml()
            md = html_to_md(
                html,
                heading_style="ATX",
                bullets="-",
                strip=["a"],
            ).strip()
        else:
            md = self._last_markup_text
        self._markup_editor.blockSignals(True)
        self._markup_editor.setPlainText(md)
        self._markup_editor.blockSignals(False)
        self._stack.setCurrentIndex(0)
        self._mode = "markup"
        self._mode_btn.setText("Formatted")
        self._mode_btn.setToolTip("Switch to formatted (rich text) view")
        self._mode_btn.setChecked(False)
        if self._formatted_dirty:
            self.text_changed.emit()

    def _on_rich_text_changed(self) -> None:
        self._formatted_dirty = True
        self.text_changed.emit()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_text(self, text: str) -> None:
        """Replace editor content.  Always accepts markdown source."""
        self._last_markup_text = text
        self._formatted_dirty = False
        self._markup_editor.setPlainText(text)
        if self._mode == "formatted":
            html = markdown2.markdown(
                text,
                extras=["tables", "fenced-code-blocks", "header-ids"],
            )
            self._rich_editor.blockSignals(True)
            self._rich_editor.setHtml(html)
            self._rich_editor.blockSignals(False)

    def get_text(self) -> str:
        """Return current content as markdown (from whichever pane is active)."""
        if self._mode == "markup":
            return self._markup_editor.toPlainText()
        else:
            html = self._rich_editor.toHtml()
            return html_to_md(
                html,
                heading_style="ATX",
                bullets="-",
                strip=["a"],
            ).strip()

    def append_text(self, text: str) -> None:
        """Append text at the end of the markup editor."""
        cursor = self._markup_editor.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(text)
        self._markup_editor.setTextCursor(cursor)

    def clear(self) -> None:
        self._markup_editor.clear()
        self._rich_editor.clear()

    def is_empty(self) -> bool:
        return not self.get_text().strip()

    def set_font_size(self, size: int) -> None:
        """Update the point size of both editor panes, keeping their typefaces."""
        self._markup_editor.setFont(QFont("Monospace", size))
        self._rich_editor.setFont(QFont("Georgia", size))

    # ------------------------------------------------------------------
    # Event filter — Tab / Shift+Tab for indent in markup mode
    # ------------------------------------------------------------------

    def eventFilter(self, obj, event) -> bool:
        if event.type() == QEvent.Type.KeyPress:
            key = event.key()
            mods = event.modifiers()

            # Find/replace input key handling
            if obj in (self._find_input, self._replace_input):
                if key == Qt.Key.Key_Escape:
                    self._hide_find()
                    return True
                if obj is self._find_input and key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                    if mods & Qt.KeyboardModifier.ShiftModifier:
                        self._find_prev()
                    else:
                        self._find_next()
                    return True

            # Markup editor Tab / Shift+Tab
            if obj is self._markup_editor:
                if key == Qt.Key.Key_Tab:
                    if mods & Qt.KeyboardModifier.ShiftModifier:
                        self._decrease_indent()
                    else:
                        self._increase_indent()
                    return True
                if key == Qt.Key.Key_Backtab:
                    self._decrease_indent()
                    return True

        return super().eventFilter(obj, event)

    # ------------------------------------------------------------------
    # Style dropdown
    # ------------------------------------------------------------------

    def _current_heading_level(self) -> int:
        """Return 0 for normal text, 1–3 for H1–H3 (markup mode)."""
        cursor = self._markup_editor.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.StartOfLine)
        cursor.movePosition(
            QTextCursor.MoveOperation.EndOfLine, QTextCursor.MoveMode.KeepAnchor
        )
        line = cursor.selectedText()
        for level in (3, 2, 1):
            if line.startswith("#" * level + " "):
                return level
        return 0

    def _set_heading_markup(self, level: int) -> None:
        self._remove_heading_markup()
        cursor = self._markup_editor.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.StartOfLine)
        cursor.insertText("#" * level + " ")
        self._markup_editor.setTextCursor(cursor)

    def _remove_heading_markup(self) -> None:
        cursor = self._markup_editor.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.StartOfLine)
        cursor.movePosition(
            QTextCursor.MoveOperation.EndOfLine, QTextCursor.MoveMode.KeepAnchor
        )
        line = cursor.selectedText()
        stripped = re.sub(r"^#{1,3} ", "", line)
        if stripped != line:
            cursor.insertText(stripped)
            self._markup_editor.setTextCursor(cursor)

    def _update_style_combo(self) -> None:
        if self._mode == "markup":
            idx = self._current_heading_level()
        else:
            level = self._rich_editor.textCursor().blockFormat().headingLevel()
            idx = min(level, 3)
        self._style_combo.blockSignals(True)
        self._style_combo.setCurrentIndex(idx)
        self._style_combo.blockSignals(False)

    def _on_style_changed(self, index: int) -> None:
        if self._mode == "markup":
            if index == 0:
                self._remove_heading_markup()
            else:
                self._set_heading_markup(index)
        else:
            cursor = self._rich_editor.textCursor()
            block_fmt = QTextBlockFormat()
            block_fmt.setHeadingLevel(index)
            char_fmt = QTextCharFormat()
            char_fmt.setFontPointSize(_HEADING_SIZES.get(index, 12))
            char_fmt.setFontWeight(
                QFont.Weight.Bold if index > 0 else QFont.Weight.Normal
            )
            cursor.mergeBlockFormat(block_fmt)
            cursor.mergeCharFormat(char_fmt)
            self._rich_editor.setTextCursor(cursor)

    # ------------------------------------------------------------------
    # Inline formatting
    # ------------------------------------------------------------------

    def _toggle_bold(self) -> None:
        if self._mode == "markup":
            cursor = self._markup_editor.textCursor()
            if cursor.hasSelection():
                sel = cursor.selectedText()
                cursor.insertText(sel[2:-2] if (sel.startswith("**") and sel.endswith("**")) else f"**{sel}**")
            else:
                cursor.insertText("****")
                cursor.movePosition(QTextCursor.MoveOperation.Left, n=2)
                self._markup_editor.setTextCursor(cursor)
        else:
            fmt = QTextCharFormat()
            is_bold = self._rich_editor.fontWeight() >= 700
            fmt.setFontWeight(QFont.Weight.Normal if is_bold else QFont.Weight.Bold)
            self._rich_editor.mergeCurrentCharFormat(fmt)

    def _toggle_italic(self) -> None:
        if self._mode == "markup":
            cursor = self._markup_editor.textCursor()
            if cursor.hasSelection():
                sel = cursor.selectedText()
                cursor.insertText(sel[1:-1] if (sel.startswith("_") and sel.endswith("_")) else f"_{sel}_")
            else:
                cursor.insertText("__")
                cursor.movePosition(QTextCursor.MoveOperation.Left, n=1)
                self._markup_editor.setTextCursor(cursor)
        else:
            fmt = QTextCharFormat()
            fmt.setFontItalic(not self._rich_editor.fontItalic())
            self._rich_editor.mergeCurrentCharFormat(fmt)

    def _toggle_strikethrough(self) -> None:
        if self._mode == "markup":
            cursor = self._markup_editor.textCursor()
            if cursor.hasSelection():
                sel = cursor.selectedText()
                cursor.insertText(sel[2:-2] if (sel.startswith("~~") and sel.endswith("~~")) else f"~~{sel}~~")
            else:
                cursor.insertText("~~~~")
                cursor.movePosition(QTextCursor.MoveOperation.Left, n=2)
                self._markup_editor.setTextCursor(cursor)
        else:
            fmt = QTextCharFormat()
            fmt.setFontStrikeOut(not self._rich_editor.currentCharFormat().fontStrikeOut())
            self._rich_editor.mergeCurrentCharFormat(fmt)

    # ------------------------------------------------------------------
    # Block / structural formatting
    # ------------------------------------------------------------------

    def _toggle_quote(self) -> None:
        if self._mode == "markup":
            self._toggle_markup_prefix("> ")
        else:
            cursor = self._rich_editor.textCursor()
            block_fmt = cursor.blockFormat()
            # Toggle between 40px indent and 0
            block_fmt.setLeftMargin(0.0 if block_fmt.leftMargin() > 0 else 40.0)
            cursor.setBlockFormat(block_fmt)

    def _toggle_bullet(self) -> None:
        if self._mode == "markup":
            self._toggle_markup_prefix("- ")
        else:
            cursor = self._rich_editor.textCursor()
            if cursor.currentList():
                # Remove from list
                lst = cursor.currentList()
                lst.remove(cursor.block())
                block_fmt = QTextBlockFormat()
                cursor.setBlockFormat(block_fmt)
            else:
                list_fmt = QTextListFormat()
                list_fmt.setStyle(QTextListFormat.Style.ListDisc)
                cursor.createList(list_fmt)

    def _increase_indent(self) -> None:
        if self._mode == "markup":
            cursor = self._markup_editor.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.StartOfLine)
            cursor.movePosition(
                QTextCursor.MoveOperation.EndOfLine, QTextCursor.MoveMode.KeepAnchor
            )
            line = cursor.selectedText()
            cursor.insertText("  " + line)
            self._markup_editor.setTextCursor(cursor)
        else:
            cursor = self._rich_editor.textCursor()
            if cursor.currentList():
                lst = cursor.currentList()
                new_fmt = QTextListFormat()
                new_fmt.setStyle(lst.format().style())
                new_fmt.setIndent(lst.format().indent() + 1)
                cursor.createList(new_fmt)
            else:
                block_fmt = cursor.blockFormat()
                block_fmt.setLeftMargin(block_fmt.leftMargin() + 20.0)
                cursor.setBlockFormat(block_fmt)

    def _decrease_indent(self) -> None:
        if self._mode == "markup":
            cursor = self._markup_editor.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.StartOfLine)
            cursor.movePosition(
                QTextCursor.MoveOperation.EndOfLine, QTextCursor.MoveMode.KeepAnchor
            )
            line = cursor.selectedText()
            if line.startswith("  "):
                cursor.insertText(line[2:])
                self._markup_editor.setTextCursor(cursor)
        else:
            cursor = self._rich_editor.textCursor()
            if cursor.currentList():
                lst = cursor.currentList()
                indent = max(1, lst.format().indent() - 1)
                new_fmt = QTextListFormat()
                new_fmt.setStyle(lst.format().style())
                new_fmt.setIndent(indent)
                cursor.createList(new_fmt)
            else:
                block_fmt = cursor.blockFormat()
                block_fmt.setLeftMargin(max(0.0, block_fmt.leftMargin() - 20.0))
                cursor.setBlockFormat(block_fmt)

    def _toggle_markup_prefix(self, prefix: str) -> None:
        cursor = self._markup_editor.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.StartOfLine)
        cursor.movePosition(
            QTextCursor.MoveOperation.EndOfLine, QTextCursor.MoveMode.KeepAnchor
        )
        line = cursor.selectedText()
        cursor.insertText(line[len(prefix):] if line.startswith(prefix) else prefix + line)
        self._markup_editor.setTextCursor(cursor)

    def _on_check_grammar(self) -> None:
        text = self.get_text().strip()
        if text:
            self.grammar_check_requested.emit(text)

    def _insert_divider(self) -> None:
        if self._mode == "markup":
            cursor = self._markup_editor.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.EndOfLine)
            cursor.insertText("\n\n---\n\n")
            self._markup_editor.setTextCursor(cursor)
        else:
            cursor = self._rich_editor.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock)
            cursor.insertHtml("<br/><hr/><br/>")
            self._rich_editor.setTextCursor(cursor)

    # ------------------------------------------------------------------
    # Word count
    # ------------------------------------------------------------------

    @staticmethod
    def _count_words(text: str) -> int:
        """Count words in *text* after stripping Markdown syntax."""
        for pattern, repl in _MD_STRIP:
            text = pattern.sub(repl, text)
        return len(_WCOUNT_RE.findall(text))

    def _refresh_word_count(self) -> None:
        count = self._count_words(self.get_text())
        self._word_count_label.setText(f"{count:,} words")

    # ------------------------------------------------------------------
    # Find / Replace
    # ------------------------------------------------------------------

    def _active_editor(self) -> QPlainTextEdit | QTextEdit:
        return self._markup_editor if self._mode == "markup" else self._rich_editor

    def _build_find_bar(self) -> QWidget:
        bar = QFrame()
        bar.setFrameShape(QFrame.Shape.StyledPanel)
        vl = QVBoxLayout(bar)
        vl.setContentsMargins(4, 2, 4, 2)
        vl.setSpacing(2)

        # --- Find row ---
        find_row = QWidget()
        fl = QHBoxLayout(find_row)
        fl.setContentsMargins(0, 0, 0, 0)
        fl.setSpacing(4)

        fl.addWidget(QLabel("Find:"))
        self._find_input = QLineEdit()
        self._find_input.setPlaceholderText("Search…")
        self._find_input.textChanged.connect(self._on_find_text_changed)
        self._find_input.installEventFilter(self)
        fl.addWidget(self._find_input)

        self._match_label = QLabel("")
        self._match_label.setMinimumWidth(90)
        fl.addWidget(self._match_label)

        prev_btn = QPushButton("▲")
        prev_btn.setFixedWidth(28)
        prev_btn.setToolTip("Previous match  (Shift+Enter)")
        prev_btn.clicked.connect(self._find_prev)
        fl.addWidget(prev_btn)

        next_btn = QPushButton("▼")
        next_btn.setFixedWidth(28)
        next_btn.setToolTip("Next match  (Enter)")
        next_btn.clicked.connect(self._find_next)
        fl.addWidget(next_btn)

        self._case_cb = QCheckBox("Case")
        self._case_cb.setToolTip("Match case")
        self._case_cb.toggled.connect(self._on_find_text_changed)
        fl.addWidget(self._case_cb)

        self._word_cb = QCheckBox("Whole word")
        self._word_cb.toggled.connect(self._on_find_text_changed)
        fl.addWidget(self._word_cb)

        close_btn = QPushButton("✕")
        close_btn.setFixedWidth(24)
        close_btn.setToolTip("Close  (Escape)")
        close_btn.clicked.connect(self._hide_find)
        fl.addWidget(close_btn)

        vl.addWidget(find_row)

        # --- Replace row (hidden when opened via Ctrl+F) ---
        self._replace_row = QWidget()
        rl = QHBoxLayout(self._replace_row)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(4)

        rl.addWidget(QLabel("Replace:"))
        self._replace_input = QLineEdit()
        self._replace_input.setPlaceholderText("Replace with…")
        self._replace_input.returnPressed.connect(self._replace_one)
        self._replace_input.installEventFilter(self)
        rl.addWidget(self._replace_input)

        replace_btn = QPushButton("Replace")
        replace_btn.setToolTip("Replace current match, then advance")
        replace_btn.clicked.connect(self._replace_one)
        rl.addWidget(replace_btn)

        replace_all_btn = QPushButton("Replace All")
        replace_all_btn.clicked.connect(self._replace_all)
        rl.addWidget(replace_all_btn)

        rl.addStretch()
        vl.addWidget(self._replace_row)

        return bar

    def _show_find(self, replace: bool = False) -> None:
        self._find_bar.show()
        self._replace_row.setVisible(replace)
        self._find_input.selectAll()
        self._find_input.setFocus()
        self._on_find_text_changed()

    def _hide_find(self) -> None:
        self._find_bar.hide()
        self._active_editor().setFocus()

    def _find_flags(self) -> QTextDocument.FindFlag:
        flags = QTextDocument.FindFlag(0)
        if self._case_cb.isChecked():
            flags |= QTextDocument.FindFlag.FindCaseSensitively
        if self._word_cb.isChecked():
            flags |= QTextDocument.FindFlag.FindWholeWords
        return flags

    def _count_matches(self, text: str) -> int:
        doc = self._active_editor().document()
        flags = self._find_flags()
        count = 0
        cursor = QTextCursor(doc)
        while True:
            cursor = doc.find(text, cursor, flags)
            if cursor.isNull():
                break
            count += 1
        return count

    def _on_find_text_changed(self) -> None:
        text = self._find_input.text()
        if not text:
            self._match_label.setText("")
            return
        count = self._count_matches(text)
        self._match_label.setText("No results" if count == 0 else f"{count} match{'es' if count != 1 else ''}")
        # Jump to first result from top
        editor = self._active_editor()
        cursor = editor.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.Start)
        editor.setTextCursor(cursor)
        editor.find(text, self._find_flags())

    def _find_next(self) -> None:
        text = self._find_input.text()
        if not text:
            return
        editor = self._active_editor()
        if not editor.find(text, self._find_flags()):
            # Wrap to top
            cursor = editor.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.Start)
            editor.setTextCursor(cursor)
            editor.find(text, self._find_flags())

    def _find_prev(self) -> None:
        text = self._find_input.text()
        if not text:
            return
        flags = self._find_flags() | QTextDocument.FindFlag.FindBackward
        editor = self._active_editor()
        if not editor.find(text, flags):
            # Wrap to bottom
            cursor = editor.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            editor.setTextCursor(cursor)
            editor.find(text, flags)

    def _replace_one(self) -> None:
        text = self._find_input.text()
        replacement = self._replace_input.text()
        if not text:
            return
        editor = self._active_editor()
        cursor = editor.textCursor()
        if cursor.hasSelection():
            selected = cursor.selectedText()
            match = selected == text if self._case_cb.isChecked() else selected.lower() == text.lower()
            if match:
                cursor.insertText(replacement)
        self._find_next()

    def _replace_all(self) -> None:
        text = self._find_input.text()
        replacement = self._replace_input.text()
        if not text:
            return
        editor = self._active_editor()
        flags = self._find_flags()
        cursor = editor.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.Start)
        editor.setTextCursor(cursor)
        count = 0
        while editor.find(text, flags):
            editor.textCursor().insertText(replacement)
            count += 1
        self._match_label.setText(f"Replaced {count}" if count else "No matches")
